"""Produce the Part 28 heartbeat artifacts from the recorded fixture.

    PYTHONPATH=src:. python tools/run_heartbeat.py
"""
import argparse
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from creditwatch import evals, panel, signals
from services import heartbeat
from services.signals.pairing import pair

ROOT = Path(__file__).resolve().parents[1]
MORNING = datetime.fromisoformat("2026-10-15T06:00:00-04:00")
BRIEF_TIME = datetime.fromisoformat("2026-10-15T07:30:00-04:00")


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def _scores(root, params, frame, truth):
    found = signals.find_signals(frame, params["heartbeat"]["materiality"], pair=pair)
    return evals.signal_scores(found, truth)


def _variant_scores(root):
    module_path = Path(root) / "tools" / "make_fixture.py"
    spec = importlib.util.spec_from_file_location("heartbeat_fixture_builder", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    params = yaml.safe_load((Path(root) / "params.yaml").read_text())
    out = []
    pricing = params["heartbeat"]["materiality"]
    for seed in (1, 2, 3):
        rows, truth = module.build(seed)
        frame = pd.DataFrame(rows)
        for column in panel.NUMERIC:
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        found = signals.find_signals(frame, pricing, pair=pair)
        score = evals.signal_scores(found, pd.DataFrame(truth).astype(str))
        out.append({"variant": seed, **score})
    return out


def generate(root=ROOT, output_root=None):
    root = Path(root)
    output = Path(output_root or root / "outputs" / "heartbeat")
    params = yaml.safe_load((root / "params.yaml").read_text())
    fixture = root / params["data"]["fixture_dir"]
    panel_data = panel.load_panel(root / params["data"]["panel"])
    truth = pd.read_csv(fixture / "ground_truth.csv", dtype=str)
    mandates = json.loads((root / params["data"]["mandates"]).read_text())

    release = heartbeat.release_job(root, now=MORNING)
    signal_rows = release["signals"]
    alerts = []
    for mandate, decisions in release["decisions"].items():
        for decision in decisions:
            event = next(
                row for row in signal_rows
                if row["adsh"] == decision["adsh"]
                and str(row["row"]) == str(decision["row"])
                and row["rule"] == decision["rule"])
            alerts.append({
                "alert_id": decision["key"],
                "ts": BRIEF_TIME.isoformat(),
                "mandate": mandate,
                "bdc": event["name"],
                "trigger": release["tick"]["release"],
                "rule": event["rule"],
                "text": (
                    f"{event['rule']}: {event['identifier']} moved from "
                    f"{event['before']} to {event['after']}."),
                "citations": [
                    f"{event['adsh']} current row {event['row']} "
                    f"(prior row {event['prior_row']})"
                ],
                "delivery": "brief",
            })
    alerts.sort(key=lambda row: (row["mandate"], row["bdc"], row["rule"], row["alert_id"]))
    _jsonl(output / "alerts.jsonl", alerts)

    brief = heartbeat.monthly_brief_job(root, now=BRIEF_TIME)
    (output / "briefs").mkdir(parents=True, exist_ok=True)
    for mandate, text in brief["briefs"].items():
        (output / "briefs" / f"{mandate}.md").write_text(text)

    filings = heartbeat.edgar_job(root)
    _jsonl(output / "filings.jsonl", filings["filings"])
    hygiene = heartbeat.hygiene_job(root)

    score = _scores(root, params, panel_data, truth)
    pair_report = signals.pairing_report(panel_data, pair(panel_data))
    variants = _variant_scores(root)
    mandate_weeks = max(1, len(mandates))
    metrics = {
        "precision": score["precision"],
        "recall": score["recall"],
        "true_positives": score["tp"],
        "false_positives": score["fp"],
        "false_negatives": score["fn"],
        "by_rule": score["by_rule"],
        "variants": variants,
        "pairing": pair_report.to_dict("records"),
        "alerts_total": len(alerts),
        "alerts_per_mandate_week": len(alerts) / mandate_weeks,
        "release_to_brief_minutes": int(
            (BRIEF_TIME - MORNING).total_seconds() / 60),
        "idle_cost_usd_per_head_check": 0.0,
        "idle_cost_note": (
            "Release detection is an HTTP HEAD request. "
            "No model calls occur when no release is published."),
    }
    (output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    run = {
        "release": release["tick"],
        "route_counts": release["route_counts"],
        "decision_counts": {
            mandate: len(decisions)
            for mandate, decisions in release["decisions"].items()
        },
        "brief_paths": [
            f"outputs/heartbeat/briefs/{mandate}.md" for mandate in sorted(mandates)
        ],
        "filings": len(filings["filings"]),
        "hygiene": hygiene,
    }
    (output / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    return {"alerts": alerts, "metrics": metrics, "run": run}


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
