"""Replay Priya's scripted corrections and write Part 7 evidence.

    PYTHONPATH=src:. python tools/run_learning.py

The run is deterministic and offline. It restores the seeded PRINCIPAL.md,
replays each correction through the authenticated memory path, compares the
agent output before and after, and preserves one audit event per learned rule.
"""
import json
from pathlib import Path

import yaml

from creditwatch import audit, channel, clock, loop, memory, panel, standin

ROOT = Path(__file__).resolve().parents[1]


def _jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))


def _reset_principal(principal, seed, corrections):
    """Remove prior generated rules without discarding a human's unrelated edit."""
    seed_text = seed.read_text()
    current = principal.read_text()
    if not current.startswith(seed_text):
        raise RuntimeError("PRINCIPAL.md contains an edit before the seeded text; refusing to overwrite it")
    generated = {
        f"- {c['content']} ({c['message_id']}, {c['ts'][:10]})"
        for c in corrections
    }
    extra = [line for line in current[len(seed_text):].splitlines() if line.strip()]
    unknown = [line for line in extra if line not in generated]
    if unknown:
        raise RuntimeError(f"PRINCIPAL.md has non-generated additions: {unknown[:2]}")
    principal.write_text(seed_text)


def main():
    P = yaml.safe_load((ROOT / "params.yaml").read_text())
    FX = ROOT / P["data"]["fixture_dir"]
    agent = ROOT / P["agent"]["workspace"]
    seed = ROOT / P["agent"]["seed"]
    principal = agent / "PRINCIPAL.md"
    corrections = _jsonl(FX / "corrections.jsonl")
    users = json.loads((FX / "users.json").read_text())
    mandates = json.loads((ROOT / P["data"]["mandates"]).read_text())
    panel_data = panel.load_panel(ROOT / P["data"]["panel"])
    materiality = P["heartbeat"]["materiality"]
    now = clock.clock()

    _reset_principal(principal, seed / "PRINCIPAL.seed.md", corrections)
    audit_dir = ROOT / "logs" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / "learning.jsonl"
    audit_path.write_text("")
    ledger = audit.Ledger(path=audit_path, clock=clock.clock)

    session = channel.login("priya", "priya-fixture-token", "mandate_a", users)
    tools = standin.read_tools(session, ledger, panel_data, mandates, materiality)
    plan = [
        {"tool": "mandate_profile", "args": {"scope": "mandate_a"},
         "rationale": "the client's positions"},
        {"tool": "bdc_signals", "args": {"scope": "public"},
         "rationale": "what changed this release"},
        {"tool": "bdc_holdings", "args": {"scope": "public"},
         "rationale": "terms behind each signal"},
    ]

    def note(rules_text, run_id):
        text, trace = loop.run_agent(
            standin.FixtureModel(plan, standin.note_writer), tools,
            "Standing rules:\n" + rules_text, clock.clock, run_id=run_id)
        return text, trace

    checks = {
        "msg-101": lambda text: "PIK (watch)" not in text,
        "msg-102": lambda text: all(
            "%" not in line for line in text.splitlines() if "marked" in line),
        "msg-103": lambda text: text.startswith("Your exposure"),
        "msg-104": lambda text: all(
            "[0009" in line
            for line in text.splitlines()
            if ": " in line and not line.startswith(("Your exposure", "PIK"))),
    }

    output_dir = ROOT / "outputs" / "learning"
    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("msg-*.jsonl"):
        old.unlink()

    report = []
    for correction in corrections:
        message_id = correction["message_id"]
        if message_id not in checks:
            raise RuntimeError(f"no adherence check for {message_id}")
        before_text, before_trace = note(principal.read_text(), f"{message_id}-before")
        if channel.authority(correction, users) != "instruction":
            raise RuntimeError(f"{message_id} is not authenticated")
        status = memory.propose_write(
            "mandate_a", correction["content"], "principal", message_id,
            ledger, principal, agent / "proposals", correction["ts"][:10])
        if status != "written":
            raise RuntimeError(f"{message_id} did not reach governed memory")
        after_text, after_trace = note(principal.read_text(), f"{message_id}-after")

        earlier = all(checks[row["correction"]](after_text) for row in report)
        record = {
            "correction": message_id,
            "before": checks[message_id](before_text),
            "after": checks[message_id](after_text),
            "earlier_rules_held": earlier,
            "adhered": checks[message_id](after_text) and earlier,
        }
        report.append(record)
        _write_jsonl(output_dir / f"{message_id}.jsonl", [
            {
                "phase": "before",
                "task_id": f"learning-{message_id}",
                "mandate": "mandate_a",
                "correction": message_id,
                "final_response": before_text,
                "checks": {
                    "adhered": checks[message_id](before_text),
                    "earlier_rules_held": True,
                },
                "trace": before_trace,
            },
            {
                "phase": "after",
                "task_id": f"learning-{message_id}",
                "mandate": "mandate_a",
                "correction": message_id,
                "final_response": after_text,
                "checks": {
                    "adhered": checks[message_id](after_text),
                    "earlier_rules_held": earlier,
                },
                "trace": after_trace,
            },
        ])

    proposals = sorted((agent / "proposals").glob("*.json"))
    reflexion = {
        "content": (
            "When a client asks about another client, say it is confidential "
            "and offer to discuss its own holdings."
        ),
        "origin": "self_reflection",
        "scope": "mandate_a",
    }
    if not proposals:
        memory.propose_write(
            "mandate_a", reflexion["content"], reflexion["origin"], None, ledger,
            principal, agent / "proposals", now.date().isoformat())

    summary = {
        "mode": "offline replay",
        "mandate": "mandate_a",
        "corrections": report,
        "adherence_after": sum(row["adhered"] for row in report),
        "regressions": [row["correction"] for row in report if not row["earlier_rules_held"]],
        "provenance": [
            event["message_id"] for event in ledger.find("memory_write")
        ],
    }
    (output_dir / "report.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if all(row["adhered"] for row in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
