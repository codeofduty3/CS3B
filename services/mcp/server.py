"""Part 1: the credit-watch MCP server (MCP Python SDK v2). From the repository root:

    export LANTERN_SESSION_TOKEN=priya-fixture-token LANTERN_MANDATE=mandate_a
    PYTHONPATH=src:. mcp dev services/mcp/server.py      # the Inspector
    PYTHONPATH=src:. python -m services.mcp.server       # stdio

One server process = one authenticated session bound to one mandate.
REGISTRY is what the server declares; catalog.conformance(REGISTRY) must be []."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [p for p in (str(ROOT), str(ROOT / "src")) if p not in sys.path]

from services.mcp import schemas as S  # noqa: E402
from services.mcp.errors import ToolFailure  # noqa: E402
from services.mcp.tools import SPEC, Toolbox  # noqa: E402

REGISTRY = {name: spec.action_class for name, spec in SPEC.items()}


def build_server(tb: Toolbox):
    """Wrap each tool for MCP. Type hints become the input schema, the
    docstring the description; the Toolbox still validates and checks scope."""
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    srv = MCPServer("creditwatch-tools", instructions=f"Bound to {tb.session.mandate}.")

    def run(name, **kw):
        try:
            return tb.call(name, **{k: v for k, v in kw.items() if v is not None})
        except ToolFailure as e:                      # typed error -> MCP isError result
            raise ToolError(f"{e.code} [{e.rule_id or '-'}]: {e.detail}") from None

    def reg(fn):
        fn.__doc__ = f"{SPEC[fn.__name__].doc} ({REGISTRY[fn.__name__]})"
        srv.tool()(fn)

    @reg
    def list_mandates() -> S.ListMandatesOut:
        return run("list_mandates")

    @reg
    def bdc_holdings(scope: str, bdc: str | None = None) -> S.HoldingsOut:
        return run("bdc_holdings", scope=scope, bdc=bdc)

    @reg
    def bdc_signals(scope: str, bdc: str | None = None) -> S.SignalsOut:
        return run("bdc_signals", scope=scope, bdc=bdc)

    @reg
    def get_filing(adsh: str) -> S.FilingOut:
        return run("get_filing", adsh=adsh)

    @reg
    def mandate_profile(scope: str) -> S.MandateProfileOut:
        return run("mandate_profile", scope=scope)

    @reg
    def watchlist_read(scope: str) -> S.WatchlistOut:
        return run("watchlist_read", scope=scope)

    @reg
    def memory_read(scope: str, query: str, k: int = 5) -> S.MemoryReadOut:
        return run("memory_read", scope=scope, query=query, k=k)

    @reg
    def memory_propose_write(scope: str, content: str, message_id: str | None = None) -> S.MemoryWriteOut:
        return run("memory_propose_write", scope=scope, content=content, message_id=message_id)

    @reg
    def watchlist_write(scope: str, bdc: str, reason: str,
                        approval_token: str | None = None) -> S.WatchlistWriteOut:
        return run("watchlist_write", scope=scope, bdc=bdc, reason=reason, approval_token=approval_token)

    @reg
    def draft_email(scope: str, to: list[str], subject: str, body: str,
                    attachments: list[str] | None = None) -> S.DraftOut:
        return run("draft_email", scope=scope, to=to, subject=subject, body=body, attachments=attachments)

    @reg
    def request_approval(item_id: str) -> S.ApprovalRequestOut:
        return run("request_approval", item_id=item_id)

    @reg
    def send_email(scope: str, draft_id: str, approval_token: str) -> S.SendOut:
        return run("send_email", scope=scope, draft_id=draft_id, approval_token=approval_token)

    return srv


def toolbox_from_env(root=ROOT):
    from creditwatch import audit, channel, clock
    from services.chat.session import load_world
    world = load_world(root)
    s = channel.login(os.environ.get("LANTERN_PRINCIPAL", world["P"]["agent"]["principal"]),
                      os.environ["LANTERN_SESSION_TOKEN"], os.environ["LANTERN_MANDATE"], world["users"])
    world["audit_dir"].mkdir(parents=True, exist_ok=True)
    led = audit.Ledger(path=world["audit_dir"] / "mcp.jsonl", clock=clock.clock)
    return Toolbox(s, world, world["ws"], root / "outputs", led)


# The object `mcp dev` and `mcp run` look for. Built only when a session is configured,
# so the self-check and the tests can import REGISTRY without logging in.
mcp = (build_server(toolbox_from_env())
       if os.environ.get("LANTERN_SESSION_TOKEN") and os.environ.get("LANTERN_MANDATE") else None)

if __name__ == "__main__":
    if mcp is None:
        sys.exit("set LANTERN_SESSION_TOKEN and LANTERN_MANDATE first")
    mcp.run()
