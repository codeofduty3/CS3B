"""Deterministic release brief rows: signals (JSON list) -> Markdown.

    python signal_digest.py signals.json hold1 hold2 ...

The script preserves the source row and accession number and never infers a
signal. It is intentionally small enough to review and test.
"""
import json
import sys


def short(name):
    return name.removeprefix("FIXTURE ").title()


def fmt(value):
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def digest(signals, held=()):
    held = set(held)
    rows = [s for s in signals if not held or s["name"] in held]
    rows.sort(key=lambda s: (s["name"], s["rule"], str(s["row"])))
    if not rows:
        return "No material credit signals in the paired holdings."
    return "\n".join(
        f"- {short(s['name'])} | {s['rule']} | {fmt(s['before'])} to "
        f"{fmt(s['after'])} | {s['adsh']} row {s['row']} (prior {s['prior_row']})"
        for s in rows)


if __name__ == "__main__":
    values = json.load(open(sys.argv[1], encoding="utf-8"))
    print(digest(values, sys.argv[2:]))
