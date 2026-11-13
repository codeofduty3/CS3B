"""Part 28: pairing, heartbeat routing, outputs and Airflow wrappers."""
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from creditwatch import evals, panel, policy, signals  # noqa: E402
from services.chat.session import load_world, others  # noqa: E402
from services.heartbeat import edgar_job, hygiene_job, monthly_brief_job, release_job  # noqa: E402
from services.signals.pairing import pair  # noqa: E402


def _fixture_builder():
    path = ROOT / "tools" / "make_fixture.py"
    spec = importlib.util.spec_from_file_location("part28_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pairing_reaches_full_recall_without_false_positives():
    params = yaml.safe_load((ROOT / "params.yaml").read_text())
    materiality = params["heartbeat"]["materiality"]
    fixture = panel.load_panel(ROOT / "data" / "fixture" / "panel_2p.csv")
    truth = pd.read_csv(ROOT / "data" / "fixture" / "ground_truth.csv", dtype=str)
    assert evals.signal_scores(
        signals.find_signals(fixture, materiality, pair=pair), truth
    )["precision"] == 1.0

    builder = _fixture_builder()
    for seed in (1, 2, 3):
        rows, expected = builder.build(seed)
        frame = pd.DataFrame(rows)
        for column in panel.NUMERIC:
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        score = evals.signal_scores(
            signals.find_signals(frame, materiality, pair=pair),
            pd.DataFrame(expected).astype(str))
        assert (score["precision"], score["recall"], score["fp"], score["fn"]) == (1.0, 1.0, 0, 0)


def test_heartbeat_jobs_route_only_held_bdcs():
    release = release_job(ROOT)
    assert release["tick"]["published"] is True
    assert release["route_counts"] == {"mandate_a": 8, "mandate_b": 4}
    assert all(item["action"] == "hold" for items in release["decisions"].values() for item in items)

    filings = edgar_job(ROOT)["filings"]
    assert {item["mandate"] for item in filings} == {"mandate_a", "mandate_b"}
    assert all(item["action"] in {"alert", "hold", "not_material"} for item in filings)

    briefs = monthly_brief_job(ROOT)["briefs"]
    assert set(briefs) == {"mandate_a", "mandate_b"}
    assert "0009000011-26-000111" in briefs["mandate_a"]
    assert "0009000013-26-000131" not in briefs["mandate_a"]

    stale = hygiene_job(ROOT)["stale_watchlist"]
    assert stale and stale[0]["bdc"] == "FIXTURE GOLF CAPITAL CORP"


def test_generated_alerts_are_complete_routed_and_isolated():
    from tools.run_heartbeat import generate

    world = load_world()
    with tempfile.TemporaryDirectory() as temp:
        result = generate(ROOT, temp)
        alert_path = Path(temp) / "alerts.jsonl"
        alerts = [json.loads(line) for line in alert_path.read_text().splitlines()]
        required = {"alert_id", "ts", "mandate", "bdc", "trigger", "rule", "text", "citations"}
        assert len(alerts) == 12
        assert all(required <= set(alert) for alert in alerts)
        for alert in alerts:
            assert alert["bdc"] in world["mandates"][alert["mandate"]]["positions"]
            protected = others(world["mandates"], world["canaries"], alert["mandate"])
            serialized = json.dumps(alert)
            assert not policy.mentions(serialized, protected["names"])
            assert not policy.leaks(serialized, protected["terms"])
        assert result["metrics"]["precision"] == result["metrics"]["recall"] == 1.0
        assert all(row["precision"] == row["recall"] == 1.0 for row in result["metrics"]["variants"])


def test_four_dags_are_thin_airflow_three_wrappers():
    ids = {}
    sdk = types.ModuleType("airflow.sdk")

    def dag(**kwargs):
        def wrapper(fn):
            ids[kwargs["dag_id"]] = kwargs
            fn()
            return fn
        return wrapper

    sdk.dag = dag
    sdk.task = lambda fn: (lambda: None)
    airflow = types.ModuleType("airflow")
    airflow.sdk = sdk
    pendulum = types.ModuleType("pendulum")
    pendulum.datetime = lambda *args, **kwargs: ("datetime", args, kwargs)
    saved = {name: sys.modules.get(name) for name in ("airflow", "airflow.sdk", "pendulum")}
    sys.modules.update({"airflow": airflow, "airflow.sdk": sdk, "pendulum": pendulum})
    try:
        for name in (
            "release_watch_dag", "edgar_watch_dag",
            "monthly_brief_dag", "hygiene_dag",
        ):
            path = ROOT / "dags" / f"{name}.py"
            spec = importlib.util.spec_from_file_location(name, path)
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
            assert "airflow.decorators" not in path.read_text()
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    assert set(ids) == {
        "release_watch_dag", "edgar_watch_dag",
        "monthly_brief_dag", "hygiene_dag",
    }


def test_scheduled_jobs_deduplicate_across_runs():
    with tempfile.TemporaryDirectory() as temp:
        release_state = Path(temp) / "release.json"
        first = release_job(ROOT, state_file=release_state)
        second = release_job(ROOT, state_file=release_state)
        assert sum(map(len, first["decisions"].values())) == 12
        assert sum(map(len, second["decisions"].values())) == 0

        edgar_state = Path(temp) / "edgar.json"
        first_filings = edgar_job(ROOT, state_file=edgar_state)["filings"]
        second_filings = edgar_job(ROOT, state_file=edgar_state)["filings"]
        assert first_filings and not second_filings
