"""The twelve catalog tools over one authenticated, mandate-bound session.

Every call goes through Toolbox.call: input schema, server-side scope check,
the tool, then one audit event and one action record with its decision.
Approval tokens are issued only by Toolbox.approve, which the harness calls
when Priya approves: no tool can mint one."""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError

from creditwatch import approvals, channel, clock, memory, policy
from creditwatch.loop import h12

from . import schemas as S
from .errors import ApprovalInvalid, BadInput, NotFound, PolicyBlocked, ScopeDenied, ToolFailure

WL_LINE = re.compile(r"^- (?P<bdc>.+?): (?P<reason>.+) \(added (?P<added>\d{4}-\d{2}-\d{2})\)$")


@dataclass
class Spec:
    action_class: str
    scope_kind: str | None            # "public", "mandate" or None (session-level)
    inp: type[BaseModel]
    fn: Callable
    doc: str


def _num(x):
    return None if x != x or x is None else float(x)     # NaN -> None


class Toolbox:
    def __init__(self, session, world, ws, out_dir, ledger, now=clock.clock):
        self.session, self.world, self.ledger, self.now = session, world, ledger, now
        self.ws, self.out = Path(ws), Path(out_dir)
        self.actions = []                 # (tool, action_class, args_hash, decision): Appendix G
        self.authenticated = {}           # message_id -> content, set by the channel, never by the model
        self.requests = {}                # watchlist request_id -> payload

    # ------------------------------------------------------------ harness-only (not tools)
    def authenticate(self, msg):
        """Register a channel message; only an authenticated one can become a learned rule."""
        a = channel.authority(msg, self.world["users"])
        if a == "instruction":
            self.authenticated[msg["message_id"]] = msg["content"]
        return a

    def approve(self, item_id):
        """Priya approves the exact payload: returns an HMAC token bound to it."""
        payload = self._item(item_id)
        ttl = self.world["P"]["approvals"]["ttl_minutes"]
        tok = approvals.issue_token(payload, self.session.principal, self.now().timestamp(), ttl)
        self.ledger.record("approval_granted", item_id=item_id, payload_hash=approvals.payload_hash(payload))
        return tok

    # ------------------------------------------------------------ the one entry point
    def call(self, tool, **kwargs):
        spec = SPEC[tool]
        h = h12(kwargs)
        try:
            try:
                args = spec.inp.model_validate(kwargs)
            except ValidationError as e:
                raise BadInput(f"{tool}: {e.error_count()} invalid argument(s): "
                               + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
            if spec.scope_kind:
                try:
                    channel.require_scope(self.session, args.scope, spec.scope_kind, self.ledger)
                except channel.ScopeError as e:
                    raise ScopeDenied(str(e), "R-SCOPE")
            out, decision = spec.fn(self, args)
        except ToolFailure as e:
            self._record(tool, spec, h, e.decision, kwargs.get("scope"), error=e.code, rule_id=e.rule_id)
            raise
        self._record(tool, spec, h, decision, kwargs.get("scope"))
        return out

    def as_tools(self):
        """name -> callable returning plain JSON, for creditwatch.loop. A result
        that is a single list (holdings, signals) is unwrapped to that list."""
        def make(name):
            def f(**kw):
                d = self.call(name, **kw).model_dump(mode="json")
                return next(iter(d.values())) if len(d) == 1 and isinstance(next(iter(d.values())), list) else d
            return f
        return {name: make(name) for name in SPEC}

    def _record(self, tool, spec, args_hash, decision, scope, **extra):
        self.actions.append({"tool": tool, "action_class": spec.action_class,
                             "args_hash": args_hash, "decision": decision})
        self.ledger.record("tool_call", tool=tool, scope=scope, args_hash=args_hash,
                           decision=decision, **{k: v for k, v in extra.items() if v})

    # ------------------------------------------------------------ helpers
    def _others(self):
        o = policy.confidential_terms(self.world["mandates"], self.session.mandate)
        o["terms"] += [c for m, cs in self.world["canaries"].items() if m != self.session.mandate for c in cs]
        return o

    def _block_if_policy(self, payload):
        rule = policy.policy_check(payload, self.world["mandates"][self.session.mandate],
                                   self.world["registry"], self._others())
        if rule:
            self.ledger.record("policy_block", rule_id=rule)
            raise PolicyBlocked(f"standing rule {rule} fired", rule)

    def _next_id(self, d, prefix):
        d.mkdir(parents=True, exist_ok=True)
        return f"{prefix}-{len(list(d.glob(f'{prefix}-*.json'))) + 1:04d}"

    def _item(self, item_id):
        if item_id in self.requests:
            return self.requests[item_id]
        f = self.out / "drafts" / f"{item_id}.json"
        if not f.exists():
            raise NotFound(f"no draft or request {item_id!r}")
        return json.loads(f.read_text())

    def _watchlist_file(self, scope):
        return self.ws / "mandates" / scope / "WATCHLIST.md"

    # ------------------------------------------------------------ READ
    def list_mandates(self, a):
        mine = self.world["users"][self.session.principal]["mandates"]
        active = self.session.mandate
        return S.ListMandatesOut(mandates=[
            S.MandateRef(mandate_id=m, active=m == active,
                         client=self.world["mandates"][m]["client"] if m == active else None)
            for m in mine]), "executed"

    def bdc_holdings(self, a):
        p = self.world["panel"]
        cur = p[p["period_role"] == "current"]
        if a.bdc:
            if a.bdc not in set(p["name"]):
                raise NotFound(f"no BDC named {a.bdc!r} in the panel")
            cur = cur[cur["name"] == a.bdc]
        return S.HoldingsOut(holdings=[
            S.Holding(adsh=r.adsh, name=r.name, identifier=r.identifier, fair_value=_num(r.fair_value),
                      principal=_num(r.principal), pik_rate=_num(r.pik_rate), source_rows=str(r.source_rows))
            for r in cur.itertuples()]), "executed"

    def bdc_signals(self, a):
        from creditwatch import signals
        found = signals.find_signals(self.world["panel"], self.world["P"]["heartbeat"]["materiality"])
        if a.bdc:
            found = found[found["name"] == a.bdc]
        return S.SignalsOut(signals=[S.Signal(**{k: (_num(v) if isinstance(v, float) else v)
                                                for k, v in r.items()})
                                     for r in found.to_dict("records")]), "executed"

    def get_filing(self, a):
        p = self.world["panel"]
        rows = p[p["adsh"] == a.adsh]
        if rows.empty:
            raise NotFound(f"no filing {a.adsh} in the panel")
        r = rows.iloc[0]
        return S.FilingOut(adsh=a.adsh, cik=str(r["cik"]), name=r["name"], form=r["form"],
                           period=str(r["period"]), filed=str(r["filed"]), url=str(r["inlineurl"]),
                           rows=len(rows)), "executed"

    def mandate_profile(self, a):
        m = self.world["mandates"][a.scope]
        return S.MandateProfileOut(mandate_id=a.scope, client=m["client"], positions=m["positions"]), "executed"

    def watchlist_read(self, a):
        f = self._watchlist_file(a.scope)
        lines = f.read_text().splitlines() if f.exists() else []
        return S.WatchlistOut(entries=[S.WatchlistEntry(**m.groupdict())
                                       for m in map(WL_LINE.match, lines) if m]), "executed"

    def memory_read(self, a):
        store = memory.MandateStore(self.ws / "mandates", a.scope)       # one SQLite file per mandate
        return S.MemoryReadOut(hits=[S.MemoryHit(text=t, source=s) for t, s in store.search(a.query, a.k)]), \
            "executed"

    # ------------------------------------------------------------ INTERNAL_WRITE
    def memory_propose_write(self, a):
        """Origin comes from the channel: only an authenticated message id writes
        PRINCIPAL.md, and then with the server's copy of its text, not the model's."""
        auth = a.message_id in self.authenticated
        status = memory.propose_write(
            a.scope, self.authenticated[a.message_id] if auth else a.content,
            "principal" if auth else "untrusted", a.message_id if auth else None, self.ledger,
            self.ws / "PRINCIPAL.md", self.ws / "proposals", self.now().date().isoformat())
        return S.MemoryWriteOut(status=status), "executed"

    def watchlist_write(self, a):
        """Policy 'approve' (clients rely on it): without a valid token it only requests."""
        if a.bdc not in self.world["mandates"][a.scope]["positions"]:
            raise NotFound(f"{a.bdc!r} is not a position of {a.scope}")
        payload = {"kind": "watchlist", "mandate": a.scope, "bdc": a.bdc, "reason": a.reason}
        if not a.approval_token:
            rid = f"wl-req-{len(self.requests) + 1:04d}"
            self.requests[rid] = payload
            return S.WatchlistWriteOut(status="requested", request_id=rid), "requested"
        v = approvals.verify_token(payload, a.approval_token, self.session.principal, self.now().timestamp())
        if v != "ok":
            raise ApprovalInvalid(f"approval token {v}", "R-APPROVAL")
        f = self._watchlist_file(a.scope)
        n = sum(1 for x in f.read_text().splitlines() if WL_LINE.match(x)) if f.exists() else 0
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"- {a.bdc}: {a.reason} (added {self.now().date().isoformat()})\n")
        eid = f"wl-{n + 1:04d}"
        self.ledger.record("watchlist_write", mandate=a.scope, entry_id=eid)
        return S.WatchlistWriteOut(status="written", entry_id=eid), "executed"

    # ------------------------------------------------------------ DRAFT
    def draft_email(self, a):
        payload = {"mandate": a.scope, "to": a.to, "subject": a.subject, "body": a.body,
                   "attachments": a.attachments}
        self._block_if_policy(payload)
        did = self._next_id(self.out / "drafts", "draft")
        (self.out / "drafts" / f"{did}.json").write_text(json.dumps({"draft_id": did, **payload}, indent=1))
        return S.DraftOut(draft_id=did, payload_hash=approvals.payload_hash(payload)), "executed"

    # ------------------------------------------------------------ CONTROL
    def request_approval(self, a):
        payload = self._item(a.item_id)
        if payload.get("mandate") != self.session.mandate:
            raise ScopeDenied(f"{a.item_id} belongs to another mandate", "R-SCOPE")
        h = approvals.payload_hash(payload)
        self.ledger.record("approval_requested", item_id=a.item_id, payload_hash=h)
        return S.ApprovalRequestOut(item_id=a.item_id, status="pending", payload_hash=h), "requested"

    # ------------------------------------------------------------ EXTERNAL_SEND
    def send_email(self, a):
        """Mock outbox only. The token must match the stored draft byte for byte."""
        payload = self._item(a.draft_id)
        if payload.get("mandate") != a.scope:
            raise ScopeDenied(f"{a.draft_id} belongs to another mandate", "R-SCOPE")
        v = approvals.verify_token(payload, a.approval_token, self.session.principal, self.now().timestamp())
        if v != "ok":
            raise ApprovalInvalid(f"approval token {v}", "R-APPROVAL")
        self._block_if_policy(payload)                     # rules re-checked at send time
        mid = self._next_id(self.out / "outbox", "mock")
        (self.out / "outbox" / f"{mid}.json").write_text(json.dumps({"outbox_message_id": mid, **payload}, indent=1))
        return S.SendOut(outbox_message_id=mid, status="sent_to_mock_outbox"), "executed"


T = Toolbox
SPEC = {   # the server's own declaration; conformance compares it with Appendix C
    "list_mandates": Spec("READ", None, S.ListMandatesIn, T.list_mandates,
                          "Mandates this principal may open; only the active one is named."),
    "bdc_holdings": Spec("READ", "public", S.BdcHoldingsIn, T.bdc_holdings,
                         "Current-period holdings from the panel, with source rows. "
                         "Omit bdc to get every BDC in one call."),
    "bdc_signals": Spec("READ", "public", S.BdcSignalsIn, T.bdc_signals,
                        "Within-filing credit signals (markdown, PIK up, extension) with evidence rows. "
                        "Omit bdc to get every BDC in one call."),
    "get_filing": Spec("READ", None, S.GetFilingIn, T.get_filing,
                       "One filing's metadata by accession number."),
    "mandate_profile": Spec("READ", "mandate", S.MandateProfileIn, T.mandate_profile,
                            "The active client's name and positions."),
    "watchlist_read": Spec("READ", "mandate", S.WatchlistReadIn, T.watchlist_read,
                           "The active client's watchlist entries."),
    "memory_read": Spec("READ", "mandate", S.MemoryReadIn, T.memory_read,
                        "Search the active mandate's memory store."),
    "memory_propose_write": Spec("INTERNAL_WRITE", "mandate", S.MemoryProposeWriteIn, T.memory_propose_write,
                                 "Propose a memory; only an authenticated message becomes a rule."),
    "watchlist_write": Spec("INTERNAL_WRITE", "mandate", S.WatchlistWriteIn, T.watchlist_write,
                            "Add a watchlist entry: requests approval first, writes only with a valid token."),
    "draft_email": Spec("DRAFT", "mandate", S.DraftEmailIn, T.draft_email,
                        "Store a draft email after the standing rules pass."),
    "request_approval": Spec("CONTROL", None, S.RequestApprovalIn, T.request_approval,
                             "Ask Priya to approve a draft or watchlist request."),
    "send_email": Spec("EXTERNAL_SEND", "mandate", S.SendEmailIn, T.send_email,
                       "Send an approved draft to the mock outbox (never real email)."),
}
