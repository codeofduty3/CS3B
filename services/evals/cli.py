"""Command line for the Part 8 evaluator."""
import argparse
import json
import sys

from creditwatch import llm

from .runner import run_suite
from .scenario import build_economics, run_release_morning


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lantern")
    commands = parser.add_subparsers(dest="command", required=True)
    evaluation = commands.add_parser("eval")
    actions = evaluation.add_subparsers(dest="action", required=True)
    run = actions.add_parser("run")
    run.add_argument("--suite", choices=["dev", "variant"], default="dev")
    run.add_argument("--mode", choices=["record", "replay", "fixture"], default="replay")
    run.add_argument("--output")
    run.add_argument("--task", action="append", dest="task_ids",
                     help="run only this task; repeat for multiple tasks")
    scenario = commands.add_parser("scenario")
    scenario_actions = scenario.add_subparsers(dest="action", required=True)
    scenario_run = scenario_actions.add_parser("run")
    scenario_run.add_argument("--output")
    args = parser.parse_args(argv)

    if args.command == "eval" and args.action == "run":
        try:
            _, summary = run_suite(
                suite=args.suite, mode=args.mode, output=args.output,
                task_ids=args.task_ids)
        except (RuntimeError, llm.CacheMiss) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2))
        return 0 if not summary["failed"] else 1
    if args.command == "scenario" and args.action == "run":
        result = run_release_morning(output=args.output)
        economics = build_economics()
        print(json.dumps({
            "scenario": result["scenario"],
            "outbox_message_id": result["outbox_message_id"],
            "watchlist_entry_id": result["watchlist_entry_id"],
            "economics": "outputs/reports/unit_economics.json",
        }, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
