"""Part 28 regression tests for pairing, jobs, state and generated alerts."""
import importlib.util
import json
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from creditwatch import evals, panel, policy, signals  # noqa: E402
from services.chat.session import load_world, others  # noqa: E402
from services.heartbeat import brief_job, edgar_job, hygiene_job, release_job  # noqa: E402
from services.signals.pairing import pair  # noqa: E402

MORNING = datetime.fromisoformat("2026-10-15T06:00:00-04:00")
BRIEF = datetime.fromisoformat("2026-10-15T07:30:00-04:00")
EDGAR = datetime.fromisoformat("2026-10-15T09:30:00-04:00")
HYGIENE = datetime.fromisoformat("2026-10-19T08:00:00-04:00")


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
    score = evals.signal_scores(
        signals.find_signals(fixture, materiality, pair=pair), truth)
    assert (score["precision"], score["recall"], score["fp"], score["fn"]) == (1.0, 1.0, 0, 0)

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


def test_jobs_use_shared_state_route_held_bdcs_and_deduplicate():
    with tempfile.TemporaryDirectory() as temp:
        output = Path(temp) / "heartbeat"
        first = release_job(now=MORNING, root=ROOT, out=output)
        assert first["published"] is True and first["held"] == 12
        dedup = release_job(now=MORNING, root=ROOT, out=output)
        assert dedup["published"] is False and dedup["held"] == 0

        briefs = brief_job(now=BRIEF, root=ROOT, out=output)
        assert len(briefs) == 2
        assert all((ROOT / row["path"]).exists() or (output / "briefs" / Path(row["path"]).name).exists()
                   for row in briefs)

        filings = edgar_job(now=EDGAR, root=ROOT, out=output)
        assert filings
        hygiene = hygiene_job(now=HYGIENE, root=ROOT, out=output)
        assert hygiene["stale_watchlist"]


def test_generated_alerts_are_complete_routed_and_isolated():
    from tools.run_heartbeat import generate

    world = load_world()
    with tempfile.TemporaryDirectory() as temp:
        result = generate(ROOT, temp)
        alerts = result["alerts"]
        required = {"alert_id", "ts", "mandate", "bdc", "trigger", "rule", "text", "citations"}
        assert alerts and all(required <= set(alert) for alert in alerts)
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
