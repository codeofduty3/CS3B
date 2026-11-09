"""Part 2: run one dev task on the real model, in record or replay mode.

    PYTHONPATH=src:. python -m services.agent.run --task dev-01 --mode record   # once, needs a key
    PYTHONPATH=src:. python -m services.agent.run --task dev-01 --mode replay   # free, offline

Each run starts from a fresh copy of the SEED workspace and an empty output
folder, so the request sequence (and its cache keys) is identical every time."""
import argparse
import json
import os
import tempfile
from pathlib import Path

from creditwatch import audit, channel, clock, loop, skills, workspace
from services.chat.session import ROOT, check_isolation, load_world
from services.mcp.tools import Toolbox

from .model import ClaudeModel, client_for


def load_task(task_id, root=ROOT):
    tasks = [json.loads(x) for x in (root / "data" / "fixture" / "tasks" / "dev.jsonl").read_text().splitlines()]
    return next(t for t in tasks if t["task_id"] == task_id)


def run_task(task, client, world, token, run_id="run-1"):
    """One task, one session bound to the task's mandate. Returns the Appendix G record and the trace."""
    P = world["P"]
    with tempfile.TemporaryDirectory() as t:
        ws = workspace.fresh_workspace(ROOT / P["agent"]["seed"], Path(t) / "agent")
        s = channel.login(P["agent"]["principal"], token, task["mandate"], world["users"])
        tb = Toolbox(s, world, ws, Path(t) / "outputs", audit.Ledger(clock=clock.clock))
        idx = skills.skill_index(skills.load_skills(ws / "skills"))
        ctx, _ = workspace.assemble(workspace.load_parts(ws, task["mandate"], skills_index=idx),
                                    P["context"]["budgets"])
        check_isolation(ctx, world["mandates"], world["canaries"], task["mandate"])
        prompt = f"{ctx}\n\n## task (active mandate: {task['mandate']})\n{task['prompt']}"
        B = P["budget"]
        model = ClaudeModel(client, P["models"]["main"], max_steps=B["max_steps_per_task"])
        text, trace = loop.run_agent(model, tb.as_tools(), prompt, clock.clock,
                                     max_steps=B["max_steps_per_task"], usd_cap=B["usd_cap_per_task"],
                                     token_cap=B["token_cap_per_task"], run_id=run_id)
    record = {"task_id": task["task_id"], "mandate": task["mandate"], "run_id": run_id,
              "final_response": text if text is not None else f"(stopped: {trace[-1].get('stop')})",
              "actions": tb.actions}
    return record, trace


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run one dev task on the real model")
    ap.add_argument("--task", default="dev-01")
    ap.add_argument("--mode", choices=["record", "replay"], default="replay")
    a = ap.parse_args(argv)
    world = load_world()
    token = os.environ.get("LANTERN_SESSION_TOKEN") or "priya-fixture-token"
    client = client_for(world["P"]["models"], a.mode, ROOT / world["P"]["models"]["cache_dir"])
    rec, trace = run_task(load_task(a.task), client, world, token)
    print(json.dumps(rec, indent=1))
    print(f"\n{len(trace)} steps, ${sum(r.get('usd', 0) for r in trace):.4f}"
          f" ({'recorded' if a.mode == 'record' else 'replayed: no API calls'})")


if __name__ == "__main__":
    main()
