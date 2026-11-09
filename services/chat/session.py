"""Part 0: the chat core shared by the CLI and the web chat.

One ChatSession per login: the session token authenticates the principal,
the session is bound to ONE mandate for its whole life (switching means a
new login), and only that mandate's files enter the model's context."""
import json
from pathlib import Path

import yaml

from creditwatch import (audit, channel, clock, evals, loop, panel, policy,
                         skills, standin, workspace)

ROOT = Path(__file__).resolve().parents[2]


class IsolationError(Exception):
    """Another mandate's name, term or canary reached this session's context."""


def load_world(root=ROOT):
    P = yaml.safe_load((root / "params.yaml").read_text())
    FX = root / P["data"]["fixture_dir"]
    return {"P": P,
            "users": json.loads((FX / "users.json").read_text()),
            "mandates": json.loads((root / P["data"]["mandates"]).read_text()),
            "registry": json.loads((root / P["data"]["registry"]).read_text()),
            "canaries": json.loads((FX / "canaries.json").read_text()),
            "panel": panel.load_panel(root / P["data"]["panel"]),
            "ws": root / P["agent"]["workspace"],
            "audit_dir": root / "logs" / "audit"}


def others(mandates, canaries, active):
    """What must never appear in the active mandate's context."""
    o = policy.confidential_terms(mandates, active)
    o["terms"] += [c for m, cs in canaries.items() if m != active for c in cs]
    return o


def check_isolation(ctx, mandates, canaries, active):
    o = others(mandates, canaries, active)
    hits = policy.mentions(ctx, o["names"]) + [c for _, c in evals.canary_hits([ctx], o["terms"])]
    if hits:
        raise IsolationError(f"{len(hits)} other-mandate item(s) in context")   # never echo the terms
    return ctx


class ChatSession:
    def __init__(self, principal, token, mandate, world, ws=None, ledger=None):
        self.session = channel.login(principal, token, mandate, world["users"])   # PermissionError if bad
        self._token = token                      # kept in memory only: never logged, never in context
        self.world, self.ws = world, Path(ws or world["ws"])
        self.audit = ledger or audit.Ledger(clock=clock.clock)
        self.audit.record("login", principal=principal, mandate=mandate)
        self.turns = 0

    @property
    def mandate(self):
        return self.session.mandate

    def context(self):
        """Fixed-order, budgeted context for the ACTIVE mandate, checked before use."""
        idx = skills.skill_index(skills.load_skills(self.ws / "skills"))
        parts = workspace.load_parts(self.ws, self.mandate, skills_index=idx)
        ctx, report = workspace.assemble(parts, self.world["P"]["context"]["budgets"])
        check_isolation(ctx, self.world["mandates"], self.world["canaries"], self.mandate)
        return ctx, report

    def message(self, text):
        """A chat line from the logged-in principal, stamped with the session token."""
        return {"principal": self.session.principal, "session_token": self._token, "content": text}

    def ask(self, text, model=None):
        """One turn: authority check, context, the loop over the session's READ tools."""
        msg = self.message(text)
        if channel.authority(msg, self.world["users"]) != "instruction":
            raise PermissionError("message is not from the authenticated principal")
        self.turns += 1
        ctx, _ = self.context()
        from services.mcp.tools import Toolbox                   # the Part 1 server's tools
        tools = Toolbox(self.session, self.world, self.ws, ROOT / "outputs", self.audit).as_tools()
        model = model or standin.FixtureModel(          # offline stand-in until Part 2's real model
            [{"tool": "mandate_profile", "args": {"scope": self.mandate}, "rationale": "client positions"},
             {"tool": "bdc_signals", "args": {"scope": "public"}, "rationale": "what changed"}],
            standin.note_writer)
        B = self.world["P"]["budget"]
        task = f"{ctx}\n\n## task (from {self.session.principal})\n{text}"
        reply, trace = loop.run_agent(model, tools, task, clock.clock, max_steps=B["max_steps_per_task"],
                                      usd_cap=B["usd_cap_per_task"], token_cap=B["token_cap_per_task"],
                                      run_id=f"chat-{self.mandate}-{self.turns}")
        self.audit.record("chat_turn", mandate=self.mandate, run_id=trace[-1]["run_id"],
                          steps=len(trace), stop=trace[-1].get("stop"))
        return reply if reply is not None else f"(stopped: {trace[-1].get('stop')})", trace
