"""Record/replay tests for the model-backed eval suite."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from creditwatch import llm  # noqa: E402
from services.agent.model import client_for  # noqa: E402
from services.agent.run import load_task  # noqa: E402
from services.chat.session import load_world  # noqa: E402
from services.evals.runner import run_agent_task  # noqa: E402

WORLD = load_world()


def _observations(req):
    out = []
    for message in req["messages"]:
        content = message["content"]
        if message["role"] == "user" and isinstance(content, list):
            out.extend(
                item for item in content
                if item.get("type") == "tool_result")
    return out


def _response(name, args, number, **extra):
    return {
        "content": [{
            "type": "tool_use",
            "id": f"toolu_{number:02d}",
            "name": name,
            "input": args,
        }],
        "stop_reason": "tool_use",
        "model": WORLD["P"]["models"]["main"]["id"],
        "tokens_in": 100,
        "tokens_out": 20,
        "usd": 0.0005,
        **extra,
    }


def _final(text):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "model": WORLD["P"]["models"]["main"]["id"],
        "tokens_in": 100,
        "tokens_out": 20,
        "usd": 0.0005,
    }


class FakeNoteAgent:
    def __init__(self):
        self.calls = 0
        self.requests = []

    def __call__(self, req):
        self.calls += 1
        self.requests.append(req)
        observed = _observations(req)
        if not observed:
            return _response(
                "mandate_profile", {"scope": "mandate_a"}, 0)
        if len(observed) == 1:
            return _response(
                "bdc_signals", {"scope": "public"}, 1)
        return _final("The note is based only on this mandate's cited filing rows.")


class FakeApprovedSend:
    def __init__(self):
        self.calls = 0
        self.draft_id = None

    def __call__(self, req):
        self.calls += 1
        observed = _observations(req)
        if not observed:
            return _response("draft_email", {
                "scope": "mandate_a",
                "to": ["cio@harborstate.example"],
                "subject": "Credit note",
                "body": "Credit update based only on this mandate's holdings.",
                "attachments": [],
            }, 0)
        draft = json.loads(observed[-1]["content"])
        if len(observed) == 1:
            self.draft_id = draft["draft_id"]
            return _response(
                "request_approval", {"item_id": self.draft_id}, 1)
        approval = json.loads(observed[-1]["content"])
        if len(observed) == 2:
            assert approval["status"] == "approved"
            return _response("send_email", {
                "scope": "mandate_a",
                "draft_id": self.draft_id,
                "approval_token": approval["approval_token"],
            }, 2)
        return _final("The approved note reached the mock outbox.")


def test_model_task_records_once_and_replays_identically_without_calls():
    task = load_task("dev-01")
    with tempfile.TemporaryDirectory() as temp:
        cache = Path(temp) / "cassettes"
        provider = FakeNoteAgent()
        record = run_agent_task(
            task, client_for(WORLD["P"]["models"], "record", cache, call=provider),
            world=WORLD)
        calls = provider.calls
        assert record["passed"] is True
        assert "Cite the accession number for every signal." in provider.requests[0]["messages"][0]["content"]
        assert calls == 3

        replay_client = client_for(WORLD["P"]["models"], "replay", cache)
        first = run_agent_task(task, replay_client, world=WORLD)
        second = run_agent_task(task, replay_client, world=WORLD)
        assert provider.calls == calls
        assert first == second == record


def test_approval_is_injected_by_harness_and_replay_is_exact():
    task = load_task("dev-04")
    with tempfile.TemporaryDirectory() as temp:
        cache = Path(temp) / "cassettes"
        provider = FakeApprovedSend()
        record = run_agent_task(
            task, client_for(WORLD["P"]["models"], "record", cache, call=provider),
            world=WORLD)
        assert provider.calls == 4
        assert [
            (action["tool"], action["decision"])
            for action in record["actions"]
        ] == [
            ("draft_email", "executed"),
            ("request_approval", "requested"),
            ("send_email", "executed"),
        ]
        replay = run_agent_task(
            task, client_for(WORLD["P"]["models"], "replay", cache), world=WORLD)
        assert provider.calls == 4
        assert replay["actions"] == record["actions"]


def test_replay_cache_miss_is_explicit():
    task = load_task("dev-02")
    with tempfile.TemporaryDirectory() as temp:
        try:
            run_agent_task(
                task, client_for(WORLD["P"]["models"], "replay", temp), world=WORLD)
            assert False
        except llm.CacheMiss:
            pass
