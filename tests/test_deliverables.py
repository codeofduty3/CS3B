"""Regression tests for the non-heartbeat team deliverables."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from creditwatch import policy, skills  # noqa: E402
from services.chat.session import load_world, others  # noqa: E402
from services.evals.runner import run_suite  # noqa: E402
from services.evals.scenario import build_economics, run_release_morning  # noqa: E402


def test_six_skills_are_locked_and_two_have_scripts():
    root = ROOT / "agent" / "skills"
    loaded = skills.load_skills(root)
    assert len(loaded) >= 6
    assert {
        "watchlist-entry", "release-brief", "ir-question", "signal-explainer"
    } <= set(loaded)
    scripted = [name for name in loaded if any((root / name / "scripts").glob("*"))]
    assert len(scripted) >= 2
    skills.verify(root, json.loads((root / "skills.lock").read_text()))


def test_release_digest_is_deterministic_and_keeps_citations():
    path = ROOT / "agent" / "skills" / "release-brief" / "scripts" / "signal_digest.py"
    spec = importlib.util.spec_from_file_location("signal_digest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    signals = [
        {
            "name": "FIXTURE HOTEL SENIOR LENDING",
            "rule": "MARKDOWN",
            "before": 99.0,
            "after": 93.5,
            "adsh": "0009000012-26-000121",
            "row": 101,
            "prior_row": 501,
        }
    ]
    first = module.digest(signals)
    assert first == module.digest(list(reversed(signals)))
    assert "0009000012-26-000121" in first and "row 101" in first


def test_dev_suite_runs_through_the_tool_server_and_writes_appendix_g_records():
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "dev.jsonl"
        rows, summary = run_suite(ROOT, "dev", "replay", output)
        assert len(rows) == 8 and summary["passed"] == 8 and not summary["failed"]
        assert all(row["passed"] for row in rows)
        assert all(
            {"task_id", "mandate", "final_response", "actions", "run_id"} <= set(row)
            for row in rows)
        assert all(
            {"tool", "action_class", "args_hash", "decision"} <= set(action)
            for row in rows for action in row["actions"])


def test_release_morning_uses_mock_outbox_and_approval_for_watchlist():
    world = load_world()
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "release_morning.json"
        result = run_release_morning(ROOT, output)
        assert result["outbox_message_id"].startswith("mock-")
        assert result["watchlist_entry_id"].startswith("wl-")
        assert len(result["steps"]) >= 6
        protected = others(world["mandates"], world["canaries"], result["mandate"])
        serialized = json.dumps(result)
        assert not policy.mentions(serialized, protected["names"])
        assert not policy.leaks(serialized, protected["terms"])


def test_unit_economics_has_cost_per_mandate_month():
    with tempfile.TemporaryDirectory() as temp:
        result = build_economics(ROOT, temp)
        assert {row["mandate"] for row in result["mandates"]} == {"mandate_a", "mandate_b"}
        assert all(row["estimated_cost_per_mandate_month_usd"] > 0 for row in result["mandates"])
        assert (Path(temp) / "unit_economics.md").exists()
