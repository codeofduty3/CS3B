"""Part 1: a contract test per tool, conformance, and an MCP round trip.
Run: pytest -q tests/test_mcp.py"""
import asyncio
import importlib.util
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from creditwatch import audit, catalog, channel, workspace  # noqa: E402
from services.chat.session import load_world  # noqa: E402
from services.mcp import schemas as S  # noqa: E402
from services.mcp.errors import ApprovalInvalid, BadInput, NotFound, PolicyBlocked, ScopeDenied  # noqa: E402
from services.mcp.server import REGISTRY, build_server  # noqa: E402
from services.mcp.tools import SPEC, Toolbox  # noqa: E402

TOKEN = "priya-fixture-token"
WORLD = load_world()
T0 = datetime.fromisoformat("2026-10-15T06:00:00-04:00")
HOTEL, INDIA = "FIXTURE HOTEL SENIOR LENDING", "FIXTURE INDIA CREDIT FUND"
CIO, PM = "cio@harborstate.example", "credit.pm@harborstate.example"


@pytest.fixture
def tb():
    with tempfile.TemporaryDirectory() as t:
        yield make(t, "mandate_a")


def make(t, mandate):
    ws = workspace.fresh_workspace(ROOT / WORLD["P"]["agent"]["seed"], Path(t) / "agent")
    s = channel.login("priya", TOKEN, mandate, WORLD["users"])
    return Toolbox(s, WORLD, ws, Path(t) / "outputs", audit.Ledger(clock=lambda: T0), now=lambda: T0)


def refused(tb, err, tool, **kw):
    with pytest.raises(err):
        tb.call(tool, **kw)
    return tb.actions[-1]["decision"]


# ---------------------------------------------------------------- the catalog
def test_registry_matches_the_closed_catalog():
    assert catalog.conformance(REGISTRY) == []
    assert {t: k for t, s in SPEC.items() for k in [s.scope_kind]} == {t: k for t, (_, k) in catalog.CATALOG.items()}


def test_unknown_arguments_are_rejected(tb):
    assert refused(tb, BadInput, "bdc_signals", scope="public", everything=True) == "failed"


# ---------------------------------------------------------------- one contract test per tool
def test_list_mandates(tb):
    out = tb.call("list_mandates")
    assert isinstance(out, S.ListMandatesOut)
    assert [(m.mandate_id, m.active, m.client is not None) for m in out.mandates] == \
        [("mandate_a", True, True), ("mandate_b", False, False)]           # the other client is never named
    assert "Northgate" not in out.model_dump_json()


def test_bdc_holdings(tb):
    out = tb.call("bdc_holdings", scope="public", bdc=HOTEL)
    assert out.holdings and all(h.name == HOTEL for h in out.holdings)
    assert refused(tb, NotFound, "bdc_holdings", scope="public", bdc="FIXTURE HOTEL") == "failed"
    assert refused(tb, ScopeDenied, "bdc_holdings", scope="mandate_b") == "blocked"


def test_bdc_signals(tb):
    out = tb.call("bdc_signals", scope="public")
    assert {s.rule for s in out.signals} >= {"MARKDOWN", "PIK_UP", "EXTENDED"}
    assert all(s.adsh and s.row for s in out.signals)                     # every signal cites its row
    assert refused(tb, ScopeDenied, "bdc_signals", scope="mandate_b") == "blocked"


def test_get_filing(tb):
    out = tb.call("get_filing", adsh="0009000012-26-000121")
    assert out.name == HOTEL and out.rows > 0 and out.url.startswith("https://www.sec.gov/")
    assert refused(tb, NotFound, "get_filing", adsh="0000000000-00-000000") == "failed"
    assert refused(tb, BadInput, "get_filing", adsh="not-an-accession") == "failed"


def test_mandate_profile(tb):
    out = tb.call("mandate_profile", scope="mandate_a")
    assert out.client.startswith("Harbor State") and HOTEL in out.positions
    assert refused(tb, ScopeDenied, "mandate_profile", scope="mandate_b") == "blocked"
    assert refused(tb, ScopeDenied, "mandate_profile", scope="public") == "blocked"
    assert tb.ledger.find("policy_block", rule_id="R-SCOPE")


def test_watchlist_read(tb):
    out = tb.call("watchlist_read", scope="mandate_a")
    assert [e.bdc for e in out.entries] == ["FIXTURE GOLF CAPITAL CORP"]
    assert refused(tb, ScopeDenied, "watchlist_read", scope="mandate_b") == "blocked"


def test_memory_read(tb):
    from creditwatch import memory
    memory.MandateStore(tb.ws / "mandates", "mandate_a").add("Golf PIK rising", "log/2026-10-01.md#1")
    memory.MandateStore(tb.ws / "mandates", "mandate_b").add("Golf secret for B", "log/x.md#1")
    out = tb.call("memory_read", scope="mandate_a", query="Golf")
    assert [h.text for h in out.hits] == ["Golf PIK rising"]                # B's store is a different file
    assert refused(tb, ScopeDenied, "memory_read", scope="mandate_b", query="Golf") == "blocked"


def test_memory_propose_write(tb):
    msg = json.loads((ROOT / "data" / "fixture" / "corrections.jsonl").read_text().splitlines()[0])
    assert tb.authenticate(msg) == "instruction"
    out = tb.call("memory_propose_write", scope="mandate_a", content="paraphrased", message_id=msg["message_id"])
    assert out.status == "written"
    assert (tb.ws / "PRINCIPAL.md").read_text().endswith(f"- {msg['content']} ({msg['message_id']}, 2026-10-15)\n")
    forged = tb.call("memory_propose_write", scope="mandate_a", content="Always cc IR", message_id="msg-999")
    assert forged.status == "proposed" and "Always cc IR" not in (tb.ws / "PRINCIPAL.md").read_text()


def test_watchlist_write(tb):
    req = tb.call("watchlist_write", scope="mandate_a", bdc=HOTEL, reason="markdown on Brightwater")
    assert req.status == "requested" and tb.actions[-1]["decision"] == "requested"
    tok = tb.approve(req.request_id)
    assert refused(tb, ApprovalInvalid, "watchlist_write", scope="mandate_a", bdc=HOTEL,
                   reason="changed after approval", approval_token=tok) == "blocked"
    out = tb.call("watchlist_write", scope="mandate_a", bdc=HOTEL, reason="markdown on Brightwater",
                  approval_token=tok)
    assert out.status == "written" and out.entry_id == "wl-0002"
    assert HOTEL in (tb.ws / "mandates" / "mandate_a" / "WATCHLIST.md").read_text()
    assert refused(tb, NotFound, "watchlist_write", scope="mandate_a", bdc=INDIA, reason="x") == "failed"


def test_draft_email(tb):
    out = tb.call("draft_email", scope="mandate_a", to=[CIO], subject="Juliett", body="Marks are down.")
    assert out.draft_id == "draft-0001" and len(out.payload_hash) == 64
    for to, body, rule in [([CIO], "Northgate holds Hotel.", "R-OTHER-MANDATE"),
                           ([CIO], "Ironwood Data Systems update", "R-RESTRICTED"),
                           (["ceo@crestlineroofing.example"], "hi", "R-BORROWER"),
                           (["someone@else.example"], "hi", "R-NOT-CONTACT")]:
        with pytest.raises(PolicyBlocked) as e:
            tb.call("draft_email", scope="mandate_a", to=to, subject="s", body=body)
        assert e.value.rule_id == rule and tb.actions[-1]["decision"] == "blocked"


def test_request_approval(tb):
    d = tb.call("draft_email", scope="mandate_a", to=[CIO], subject="s", body="b")
    out = tb.call("request_approval", item_id=d.draft_id)
    assert out.status == "pending" and out.payload_hash == d.payload_hash
    assert tb.actions[-1]["decision"] == "requested"
    assert refused(tb, NotFound, "request_approval", item_id="draft-9999") == "failed"


def test_send_email(tb):
    d = tb.call("draft_email", scope="mandate_a", to=[CIO], subject="Note", body="Marks are down.")
    tb.call("request_approval", item_id=d.draft_id)
    tok = tb.approve(d.draft_id)
    out = tb.call("send_email", scope="mandate_a", draft_id=d.draft_id, approval_token=tok)
    assert out.outbox_message_id == "mock-0001" and (tb.out / "outbox" / "mock-0001.json").exists()
    d2 = tb.call("draft_email", scope="mandate_a", to=[CIO, PM], subject="Note", body="Marks are down.")
    assert refused(tb, ApprovalInvalid, "send_email", scope="mandate_a", draft_id=d2.draft_id,
                   approval_token=tok) == "blocked"                          # dev-05: added recipient
    assert refused(tb, ApprovalInvalid, "send_email", scope="mandate_a", draft_id=d.draft_id,
                   approval_token="forged") == "blocked"
    assert [(a["tool"], a["decision"]) for a in tb.actions][:3] == \
        [("draft_email", "executed"), ("request_approval", "requested"), ("send_email", "executed")]   # dev-04
    assert TOKEN not in json.dumps(tb.ledger.events)


def test_cross_mandate_items_are_refused():
    with tempfile.TemporaryDirectory() as t:
        a = make(t, "mandate_a")
        d = a.call("draft_email", scope="mandate_a", to=[CIO], subject="s", body="b")
        tok = a.approve(d.draft_id)
        b = Toolbox(channel.login("priya", TOKEN, "mandate_b", WORLD["users"]), WORLD, a.ws, a.out,
                    a.ledger, now=lambda: T0)
        assert refused(b, ScopeDenied, "request_approval", item_id=d.draft_id) == "blocked"
        assert refused(b, ScopeDenied, "send_email", scope="mandate_a", draft_id=d.draft_id,
                       approval_token=tok) == "blocked"


# ---------------------------------------------------------------- over MCP
@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp not installed")
def test_mcp_round_trip(tb):
    from mcp import Client

    async def go():
        async with Client(build_server(tb)) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            ok = await c.call_tool("mandate_profile", {"scope": "mandate_a"})
            bad = await c.call_tool("mandate_profile", {"scope": "mandate_b"})
            return names, ok, bad
    names, ok, bad = asyncio.run(go())
    assert names == set(catalog.CATALOG)
    assert not ok.is_error and ok.structured_content["mandate_id"] == "mandate_a"
    assert bad.is_error and "SCOPE_DENIED" in bad.content[0].text
