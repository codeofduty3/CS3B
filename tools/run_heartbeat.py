"""Produce Part 28 heartbeat and metric artifacts through Kahan's jobs."""
import argparse
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from creditwatch import evals, panel, signals
from services.heartbeat import brief_job, edgar_job, hygiene_job, release_job
from services.signals.pairing import pair

ROOT = Path(__file__).resolve().parents[1]
MORNING = datetime.fromisoformat("2026-10-15T06:00:00-04:00")
BRIEF_TIME = datetime.fromisoformat("2026-10-15T07:30:00-04:00")
EDGAR_TIME = datetime.fromisoformat("2026-10-15T09:30:00-04:00")
HYGIENE_TIME = datetime.fromisoformat("2026-10-19T08:00:00-04:00")


def _variant_scores(root):
    path = Path(root) / "tools" / "make_fixture.py"
    spec = importlib.util.spec_from_file_location("heartbeat_fixture_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    params = yaml.safe_load((Path(root) / "params.yaml").read_text())
    materiality = params["heartbeat"]["materiality"]
    rows = []
    for seed in (1, 2, 3):
        generated, truth = module.build(seed)
        frame = pd.DataFrame(generated)
        for column in panel.NUMERIC:
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        found = signals.find_signals(frame, materiality, pair=pair)
        score = evals.signal_scores(found, pd.DataFrame(truth).astype(str))
        rows.append({"variant": seed, **score})
    return rows


def generate(root=ROOT, output_root=None):
    root = Path(root)
    output = Path(output_root or root / "outputs" / "heartbeat")
    output.mkdir(parents=True, exist_ok=True)
    for name in ("alerts.jsonl", "decisions.jsonl", "ticks.jsonl", "briefs.jsonl", "state.json"):
        path = output / name
        if path.exists():
            path.unlink()

    release = release_job(now=MORNING, root=root, out=output)
    briefs = brief_job(now=BRIEF_TIME, root=root, out=output)
    filings = edgar_job(now=EDGAR_TIME, root=root, out=output)
    hygiene = hygiene_job(now=HYGIENE_TIME, root=root, out=output)

    alerts_path = output / "alerts.jsonl"
    alerts = [
        json.loads(line) for line in alerts_path.read_text().splitlines()
        if line.strip()
    ] if alerts_path.exists() else []
    release_alerts = [row for row in alerts if row.get("source") == "release_watch_dag"]

    params = yaml.safe_load((root / "params.yaml").read_text())
    fixture = root / params["data"]["fixture_dir"]
    current_panel = panel.load_panel(root / params["data"]["panel"])
    truth = pd.read_csv(fixture / "ground_truth.csv", dtype=str)
    found = signals.find_signals(current_panel, params["heartbeat"]["materiality"], pair=pair)
    score = evals.signal_scores(found, truth)
    mandates = json.loads((root / params["data"]["mandates"]).read_text())
    metrics = {
        "precision": score["precision"],
        "recall": score["recall"],
        "true_positives": score["tp"],
        "false_positives": score["fp"],
        "false_negatives": score["fn"],
        "by_rule": score["by_rule"],
        "variants": _variant_scores(root),
        "pairing": signals.pairing_report(current_panel, pair(current_panel)).to_dict("records"),
        "alerts_total": len(alerts),
        "release_alerts": len(release_alerts),
        "alerts_per_mandate_week": len(release_alerts) / max(1, len(mandates)),
        "release_to_brief_minutes": int((BRIEF_TIME - MORNING).total_seconds() / 60),
        "idle_cost_usd_per_head_check": 0.0,
        "idle_cost_note": "No model calls occur on the heartbeat path.",
    }
    (output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    summary = {
        "release": release,
        "briefs": briefs,
        "filings": filings,
        "hygiene": hygiene,
    }
    (output / "run.json").write_text(json.dumps(summary, indent=2) + "\n")
    return {"alerts": alerts, "metrics": metrics, "run": summary}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = generate(args.root, args.output)
    print(json.dumps({
        "alerts": len(result["alerts"]),
        "precision": result["metrics"]["precision"],
        "recall": result["metrics"]["recall"],
        "variants": [
            {"variant": row["variant"], "precision": row["precision"], "recall": row["recall"]}
            for row in result["metrics"]["variants"]
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
