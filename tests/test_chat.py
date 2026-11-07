"""Part 0: the chat authenticates by session token, binds one mandate, and keeps contexts apart.
Run: pytest -q tests/test_chat.py"""
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from creditwatch import channel, evals, workspace  # noqa: E402
from services.chat import cli, web  # noqa: E402
from services.chat.session import ChatSession, IsolationError, load_world  # noqa: E402

TOKEN = "priya-fixture-token"
WORLD = load_world()


def fresh(t):
    return workspace.fresh_workspace(ROOT / WORLD["P"]["agent"]["seed"], Path(t) / "agent")


def test_mandate_a_context_has_no_mandate_b_content_or_canaries():
    with tempfile.TemporaryDirectory() as t:
        ws = fresh(t)
        for active, other in [("mandate_a", "mandate_b"), ("mandate_b", "mandate_a")]:
            ctx, _ = ChatSession("priya", TOKEN, active, WORLD, ws=ws).context()
            assert WORLD["mandates"][active]["client"] in ctx
            assert evals.canary_hits([ctx], WORLD["canaries"][active])           # own canary is there...
            assert not evals.canary_hits([ctx], WORLD["canaries"][other])        # ...the other's never
            o = WORLD["mandates"][other]
            for name in [o["client"], *o.get("aliases", []), *o.get("confidential_terms", [])]:
                assert name not in ctx


def test_a_planted_leak_is_caught_before_the_model_sees_it():
    with tempfile.TemporaryDirectory() as t:
        ws = fresh(t)
        w = ws / "mandates" / "mandate_a" / "WATCHLIST.md"
        w.write_text(w.read_text() + "\nnote: " + WORLD["canaries"]["mandate_b"][0] + "\n")
        try:
            ChatSession("priya", TOKEN, "mandate_a", WORLD, ws=ws).context(); assert False
        except IsolationError as e:
            assert WORLD["canaries"]["mandate_b"][0] not in str(e)               # the error never echoes it


def test_session_token_authenticates_and_the_session_is_bound():
    for bad in [("priya", "guess", "mandate_a"), ("mallory", TOKEN, "mandate_a"), ("priya", TOKEN, "mandate_z")]:
        try:
            ChatSession(*bad, WORLD); assert False
        except PermissionError:
            pass
    with tempfile.TemporaryDirectory() as t:
        s = ChatSession("priya", TOKEN, "mandate_a", WORLD, ws=fresh(t))
        assert channel.authority(s.message("hi"), WORLD["users"]) == "instruction"
        reply, trace = s.ask("What changed for my client?")
        assert "Northgate" not in reply and trace[0]["tool"] == "mandate_profile"
        try:
            channel.require_scope(s.session, "mandate_b", "mandate", s.audit); assert False
        except channel.ScopeError:
            assert s.audit.find("policy_block", rule_id="R-SCOPE")
        assert TOKEN not in json.dumps(s.audit.events)                            # never logged


def test_cli_refuses_bad_token_and_mandate_switch():
    import os
    old = os.environ.get("LANTERN_SESSION_TOKEN")
    try:
        os.environ["LANTERN_SESSION_TOKEN"] = "guess"
        out = []
        assert cli.main(["--mandate", "mandate_a"], inp=lambda _: "/quit", out=out.append) == 2
        os.environ["LANTERN_SESSION_TOKEN"] = TOKEN
        lines, out = iter(["/mandate mandate_b", "/quit"]), []
        assert cli.main(["--mandate", "mandate_a"], inp=lambda _: next(lines), out=out.append) == 0
        assert "bound to mandate_a" in out[0] and "log in again" in out[1]
    finally:
        os.environ.pop("LANTERN_SESSION_TOKEN", None) if old is None else os.environ.update(LANTERN_SESSION_TOKEN=old)


def test_web_chat_login_cookie_and_binding():
    srv = web.make_server(WORLD, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body, cookie=None):
        req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read()), r.headers.get("Set-Cookie")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), None
    try:
        assert post("/login", {"principal": "priya", "token": "guess", "mandate": "mandate_a"})[0] == 403
        assert post("/chat", {"text": "hi"})[0] == 401
        code, j, ck = post("/login", {"principal": "priya", "token": TOKEN, "mandate": "mandate_a"})
        assert code == 200 and TOKEN not in json.dumps(j) and "HttpOnly" in ck
        sid = ck.split(";")[0]
        code, j, _ = post("/chat", {"text": "What changed?"}, sid)
        assert code == 200 and j["mandate"] == "mandate_a" and "Northgate" not in j["reply"]
        assert post("/chat", {"text": "hi", "mandate": "mandate_b"}, sid)[0] == 403
    finally:
        srv.shutdown()
