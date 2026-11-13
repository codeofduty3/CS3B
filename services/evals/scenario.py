"""Release-morning integration and the unit-economics table."""
import json
import shutil
import tempfile
import importlib.util
from pathlib import Path

from creditwatch import audit, channel, clock, econ, heartbeat, panel, signals

from services.chat.session import ROOT, load_world
from services.evals.runner import _short
from services.mcp.tools import Toolbox


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def _workspace_copy(root, world, destination):
    shutil.copytree(
        root / world["P"]["agent"]["workspace"], destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.sqlite", ".ipynb_checkpoints"))
    return destination


def _pairing(root):
    """Use Kahan's Part 28 pairing as soon as it is present."""
    path = Path(root) / "services" / "signals" / "pairing.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("creditwatch_team_pairing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pair


def _brief(profile, found):
    exposure = ", ".join(
        f"{_short(name)} ${value:,.1f}m" for name, value in profile.positions.items())
    lines = [f"Your exposure: {exposure}."]
    for event in found:
        if event.rule == "MARKDOWN":
            move = f"{event.before:.1f} to {event.after:.1f} ({event.before - event.after:.1f} points)"
        elif event.rule == "PIK_UP":
            move = f"{event.before:.2f}% to {event.after:.2f}%"
        else:
            move = f"{event.before} to {event.after}"
        lines.append(
            f"{_short(event.name)}: {event.rule} {move}; "
            f"{event.adsh}, row {event.row} (prior {event.prior_row}).")
    return "\n".join(lines)


def run_release_morning(root=ROOT, output=None):
    """Run the fixture release through the real Toolbox and mock outputs."""
    root = Path(root)
    world = load_world(root)
    pane = panel.load_panel(root / world["P"]["data"]["panel"])
    materiality = world["P"]["heartbeat"]["materiality"]
    now = clock.clock()
    fixture = root / world["P"]["data"]["fixture_dir"]
    heads = json.loads((fixture / "release_heads.json").read_text())
    tick = heartbeat.release_tick(
        heads["last_seen"], lambda url: heads["heads"].get(url, 404),
        world["P"]["sec"]["base_url"])
    pair = _pairing(root)
    found_all = signals.find_signals(pane, materiality, pair=pair) if pair else signals.find_signals(
        pane, materiality)
    held_map = {m: list(data["positions"]) for m, data in world["mandates"].items()}
    routed = heartbeat.route(found_all, held_map)
    mandate = "mandate_a"
    seen = set()
    decisions = heartbeat.decide(routed[mandate], mandate, seen, now)
    steps = [{
        "step": 1,
        "name": "release_tick",
        "release": tick["release"],
        "url": tick["url"],
        "published": tick["published"],
        "actions": decisions,
    }]

    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        workspace = _workspace_copy(root, world, temp / "agent")
        ledger = audit.Ledger(clock=clock.clock)
        session = channel.login(
            world["P"]["agent"]["principal"], "priya-fixture-token",
            mandate, world["users"])
        tb = Toolbox(session, world, workspace, temp / "outputs", ledger)

        profile = tb.call("mandate_profile", scope=mandate)
        found = tb.call("bdc_signals", scope="public")
        held_signals = [event for event in found.signals if event.name in profile.positions]
        body = _brief(profile, held_signals)
        steps.append({
            "step": 2,
            "name": "brief_ready",
            "time": "07:30",
            "held_signal_count": len(held_signals),
            "body": body,
        })

        draft = tb.call(
            "draft_email", scope=mandate,
            to=world["mandates"][mandate]["contacts"][:1],
            subject=f"BDC credit brief, release {tick['release']}",
            body=body, attachments=[])
        steps.append({"step": 3, "name": "draft_email", "draft_id": draft.draft_id})

        request = tb.call("request_approval", item_id=draft.draft_id)
        steps.append({
            "step": 4,
            "name": "approval_requested",
            "item_id": request.item_id,
            "payload_hash": request.payload_hash,
        })

        token = tb.approve(draft.draft_id)
        sent = tb.call(
            "send_email", scope=mandate, draft_id=draft.draft_id,
            approval_token=token)
        steps.append({
            "step": 5,
            "name": "send_email",
            "outbox_message_id": sent.outbox_message_id,
            "destination": "mock outbox only",
        })

        watch_bdc = (
            held_signals[0].name if held_signals
            else next(iter(profile.positions)))
        requested = tb.call(
            "watchlist_write", scope=mandate, bdc=watch_bdc,
            reason=f"Material release signal in {tick['release']}")
        steps.append({
            "step": 6,
            "name": "watchlist_requested",
            "request_id": requested.request_id,
            "bdc": watch_bdc,
        })

        watch_token = tb.approve(requested.request_id)
        written = tb.call(
            "watchlist_write", scope=mandate, bdc=watch_bdc,
            reason=f"Material release signal in {tick['release']}",
            approval_token=watch_token)
        steps.append({
            "step": 7,
            "name": "watchlist_written",
            "entry_id": written.entry_id,
            "bdc": watch_bdc,
        })
        actions = list(tb.actions)

    result = {
        "scenario": "release_morning",
        "mode": "offline replay",
        "clock": now.isoformat(),
        "mandate": mandate,
        "release": tick["release"],
        "published": tick["published"],
        "held_alerts": decisions,
        "steps": steps,
        "outbox_message_id": sent.outbox_message_id,
        "watchlist_entry_id": written.entry_id,
        "actions": actions,
    }
    target = Path(output or root / "outputs" / "scenario" / "release_morning.json")
    _write_json(target, result)
    return result


def build_economics(root=ROOT, output_dir=None):
    """Estimate one release month from recorded replay usage and params."""
    root = Path(root)
    world = load_world(root)
    params = world["P"]
    models = params["models"]
    main = models["main"]
    small = models["small"]
    note_steps = 6
    note_cost = note_steps * econ.step_usd(
        2000, 300, main["usd_per_mtok_in"], main["usd_per_mtok_out"])
    signal_step_cost = econ.step_usd(
        600, 80, small["usd_per_mtok_in"], small["usd_per_mtok_out"])
    pair = _pairing(root)
    found = signals.find_signals(
        world["panel"], params["heartbeat"]["materiality"], pair=pair
    ) if pair else signals.find_signals(world["panel"], params["heartbeat"]["materiality"])
    rates = params["economics"]
    rows = []
    for mandate, data in world["mandates"].items():
        held = set(data["positions"])
        signal_count = int(found["name"].isin(held).sum())
        model_cost = note_cost + signal_count * signal_step_cost
        review_value = len(held) * rates["analyst_hours_per_review"] * rates["analyst_rate_usd"]
        saved = econ.hours_saved_table(
            rates["analyst_hours_per_review"], rates["analyst_rate_usd"],
            tasks=len(held))
        rows.append({
            "mandate": mandate,
            "held_bdcs": len(held),
            "material_signals": signal_count,
            "estimated_model_cost_usd": round(model_cost, 6),
            "estimated_cost_per_mandate_month_usd": round(model_cost, 6),
            "analyst_review_value_usd": round(review_value, 2),
            "net_value_at_x1_usd": round(review_value - model_cost, 2),
            "sensitivity": saved,
        })
    privacy_response_cost = 8 * rates["analyst_rate_usd"]
    result = {
        "mode": "production estimate from configured model prices",
        "releases_per_mandate_month": 1,
        "note_steps": note_steps,
        "assumptions": [
            "One client note per mandate per monthly release.",
            "Six main-model steps per completed note at 2,000 input and 300 output tokens per step.",
            "One small-model materiality call per held signal at 600 input and 80 output tokens.",
            "Manual review assumes 3 analyst hours per held BDC at $120 per hour.",
            "Infrastructure, retries and human approval time are excluded from model cost.",
        ],
        "mandates": rows,
        "failure_cost": {
            "privacy_response_hours": 8,
            "privacy_response_cost_usd": privacy_response_cost,
            "note": "Reputational and contractual effects are not priced; approvals and R-OTHER-MANDATE prevent the modeled incidence.",
        },
    }
    report_dir = Path(output_dir or root / "outputs" / "reports")
    target = report_dir / "unit_economics.json"
    _write_json(target, result)

    markdown = [
        "# Unit economics",
        "",
        "| Mandate | Held BDCs | Signals | Model cost / month | Analyst review value | Net value at x1 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        markdown.append(
            f"| {row['mandate']} | {row['held_bdcs']} | {row['material_signals']} | "
            f"${row['estimated_cost_per_mandate_month_usd']:.4f} | "
            f"${row['analyst_review_value_usd']:,.0f} | ${row['net_value_at_x1_usd']:,.2f} |")
    markdown += [
        "",
        "The estimate assumes one release and one completed client note per mandate-month. "
        "Model cost is computed from the prices in `params.yaml`; it is not a provider invoice.",
        "",
        "The cost of failure is controlled by exact-payload approval and client isolation. "
        f"The modeled incident-response labor floor is ${privacy_response_cost:,.0f}; "
        "reputational and contractual effects are deliberately left unpriced.",
    ]
    report = report_dir / "unit_economics.md"
    report.write_text("\n".join(markdown) + "\n")
    return result
