"""Part 2: the real model behind ReplayClient, routing, and identical replays.
Run: pytest -q tests/test_agent.py (no API key needed)"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from creditwatch import catalog, llm  # noqa: E402
from services.agent import router  # noqa: E402
from services.agent.model import ClaudeModel, client_for, tool_defs  # noqa: E402
from services.agent.run import load_task, run_task  # noqa: E402
from services.chat.session import load_world  # noqa: E402

WORLD = load_world()
MODELS = WORLD["P"]["models"]
TOKEN = "priya-fixture-token"
PLAN = [("mandate_profile", {"scope": "mandate_a"}), ("bdc_signals", {"scope": "public"})]


class FakeClaude:
    """Scripted provider in the Messages API's response shape. Counts its calls."""

    def __init__(self):
        self.calls, self.requests = 0, []

    def __call__(self, req):
        self.calls += 1
        self.requests.append(req)
        base = {"model": req["model"], "tokens_in": 2000, "tokens_out": 300, "usd": 0.007}
        if "tools" not in req:                                   # a router call: structured JSON
            props = req["output_config"]["format"]["schema"]["properties"]
            ans = {"label": "other_client_request"} if "label" in props else {"severity": "high"}
            return {**base, "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": json.dumps({**ans, "reason": "scripted"})}]}
        done = sum(1 for m in req["messages"] if m["role"] == "user" and isinstance(m["content"], list))
        if done < len(PLAN):
            name, args = PLAN[done]
            return {**base, "stop_reason": "tool_use", "content": [
                {"type": "thinking", "thinking": "", "signature": f"sig-{done}"},
                {"type": "tool_use", "id": f"toolu_{done:02d}", "name": name, "input": args}]}
        held = json.loads(req["messages"][2]["content"][0]["content"])["positions"]
        return {**base, "stop_reason": "end_turn",
                "content": [{"type": "text", "text": f"Note covering {len(held)} positions."}]}


def test_tools_offered_to_the_model_are_the_closed_catalog():
    defs = tool_defs()
    assert {d["name"] for d in defs} == set(catalog.CATALOG)
    assert all(d["input_schema"]["type"] == "object" for d in defs)


def test_request_shape_rebuilds_history_and_flags_refusals():
    fake = FakeClaude()
    m = ClaudeModel(fake, MODELS["main"])
    step = m([{"role": "user", "content": "task"}])
    assert step["tool"] == "mandate_profile"
    err = json.dumps({"error": "ScopeDenied", "detail": "no"})
    req = m.request([{"role": "user", "content": "task"}, {"role": "tool", "name": "mandate_profile", "content": err}])
    assert req["model"] == MODELS["main"]["id"] and req["fallbacks"] == "default"
    asst, res = req["messages"][1], req["messages"][2]["content"][0]
    assert asst["content"][0]["type"] == "thinking"                  # passed back unchanged
    assert res["tool_use_id"] == "toolu_00" and res["is_error"] is True


def test_record_once_then_replay_twice_identical():
    task = load_task("dev-01")
    with tempfile.TemporaryDirectory() as cache:
        fake = FakeClaude()
        rec, tr = run_task(task, client_for(MODELS, "record", cache, call=fake), WORLD, TOKEN)
        recorded = fake.calls
        assert recorded == 3 and rec["final_response"] == "Note covering 3 positions."
        runs = [run_task(task, client_for(MODELS, "replay", cache), WORLD, TOKEN) for _ in range(2)]
        assert fake.calls == recorded                                   # replay made no provider calls
        a, b = (json.dumps(r[1], sort_keys=True) for r in runs)
        assert a == b == json.dumps(tr, sort_keys=True)
        assert [(x["tool"], x["decision"]) for x in rec["actions"]] == \
            [("mandate_profile", "executed"), ("bdc_signals", "executed")]   # dev-01's expected sequence
        with pytest.raises(llm.CacheMiss):                               # replay never calls out
            run_task({**task, "prompt": "a prompt never recorded"}, client_for(MODELS, "replay", cache),
                     WORLD, TOKEN)


def test_budget_guard_stops_the_real_model_cleanly():
    from creditwatch import clock, loop
    tools = {"mandate_profile": lambda **k: {}, "bdc_signals": lambda **k: []}
    for caps, why in [({"usd_cap": 0.01}, "budget_usd"), ({"token_cap": 3000}, "budget_tokens"),
                      ({"max_steps": 1}, "max_steps")]:
        text, tr = loop.run_agent(ClaudeModel(FakeClaude(), MODELS["main"]), tools, "task", clock.clock, **caps)
        assert text is None and tr[-1]["stop"] == why


def test_small_model_takes_triage_and_materiality():
    assert router.model_for(MODELS, "agent")["id"] == MODELS["main"]["id"]
    with tempfile.TemporaryDirectory() as cache:
        fake = FakeClaude()
        c = client_for(MODELS, "record", cache, call=fake)
        inbox = json.loads((ROOT / "data" / "fixture" / "inbox.jsonl").read_text().splitlines()[0])
        label, _ = router.triage(c, MODELS, inbox)
        sig = {"name": "FIXTURE JULIETT LENDING BDC", "rule": "MARKDOWN", "identifier": "x",
               "before": 97.5, "after": 90.0}
        sev, _ = router.materiality(c, MODELS, sig, 35.5)
        assert label["label"] == "other_client_request" and sev["severity"] == "high"
        assert [r["model"] for r in fake.requests] == [MODELS["small"]["id"]] * 2
        assert all(r["output_config"]["format"]["type"] == "json_schema" for r in fake.requests)
        assert "<message>" in fake.requests[0]["messages"][0]["content"]          # body quoted as data


def test_recorded_dev_task_replays_identically():
    """Uses the real recording in evals/cassettes. Record it once:
    PYTHONPATH=src:. python -m services.agent.run --task dev-01 --mode record"""
    task, cache = load_task("dev-01"), ROOT / MODELS["cache_dir"]
    try:
        runs = [run_task(task, client_for(MODELS, "replay", cache), WORLD, TOKEN) for _ in range(2)]
    except llm.CacheMiss:
        pytest.skip("dev-01 not recorded yet")
    assert json.dumps(runs[0][1], sort_keys=True) == json.dumps(runs[1][1], sort_keys=True)
    assert runs[0][0] == runs[1][0]
