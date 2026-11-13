"""Importable heartbeat jobs used by the Airflow DAGs and the fixture runner."""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from creditwatch import heartbeat as core
from creditwatch import panel, signals

from services.signals.pairing import pair

ROOT = Path(__file__).resolve().parents[1]


def state_path(kind, root=ROOT):
    """Persistent deduplication state for a scheduled job."""
    return Path(root) / "outputs" / "heartbeat" / "state" / f"{kind}.json"


def _seen(state_file):
    path = Path(state_file) if state_file else None
    if path and path.exists():
        return set(json.loads(path.read_text()).get("seen", []))
    return set()


def _remember(state_file, seen):
    if not state_file:
        return
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seen": sorted(seen)}, indent=2) + "\n")


def _load(root):
    import yaml

    root = Path(root)
    params = yaml.safe_load((root / "params.yaml").read_text())
    fixture = root / params["data"]["fixture_dir"]
    world = {
        "params": params,
        "fixture": fixture,
        "panel": panel.load_panel(root / params["data"]["panel"]),
        "mandates": json.loads((root / params["data"]["mandates"]).read_text()),
        "registry": json.loads((root / params["data"]["registry"]).read_text()),
    }
    return world


def _routes(world):
    held = {mandate: list(data["positions"]) for mandate, data in world["mandates"].items()}
    return held


def release_job(root=ROOT, now=None, head=None, state_file=None):
    """Check the next monthly release and route its within-filing signals."""
    world = _load(root)
    params = world["params"]
    heads = json.loads((world["fixture"] / "release_heads.json").read_text())
    now = now or datetime.fromisoformat("2026-10-15T06:00:00-04:00")
    head = head or (lambda url: heads["heads"].get(url, 404))
    tick = core.release_tick(heads["last_seen"], head, params["sec"]["base_url"])
    found = signals.find_signals(
        world["panel"], params["heartbeat"]["materiality"], pair=pair)
    routed = core.route(found, _routes(world))
    seen = _seen(state_file)
    decisions = {
        mandate: core.decide(routed[mandate], mandate, seen, now)
        for mandate in sorted(routed)
    }
    _remember(state_file, seen)
    return {
        "tick": tick,
        "signals": found.to_dict("records"),
        "route_counts": {mandate: int(len(frame)) for mandate, frame in routed.items()},
        "decisions": decisions,
    }


def edgar_job(root=ROOT, now=None, state_file=None):
    """Process watched filings for every BDC held by at least one mandate."""
    world = _load(root)
    now = now or datetime.fromisoformat("2026-10-15T09:00:00-04:00")
    recent = json.loads((world["fixture"] / "edgar_recent.json").read_text())
    seen = _seen(state_file)
    out = []
    for cik, feed in recent.items():
        name = next(
            bdc for bdc, registered in world["registry"]["bdc_cik"].items()
            if registered == cik)
        filings = core.new_filings(feed, seen=set())
        for mandate, data in world["mandates"].items():
            if name in data["positions"]:
                out.extend(core.filing_decisions(
                    filings, name, data["positions"], mandate, seen, now))
    _remember(state_file, seen)
    return {"filings": out}


def monthly_brief_job(root=ROOT, now=None):
    """Build one held-BDC brief per mandate, with cited signal rows."""
    world = _load(root)
    params = world["params"]
    now = now or datetime.fromisoformat("2026-10-15T07:30:00-04:00")
    found = signals.find_signals(
        world["panel"], params["heartbeat"]["materiality"], pair=pair)
    briefs = {}
    for mandate, data in world["mandates"].items():
        events = found[found["name"].isin(data["positions"])].sort_values(
            ["name", "rule", "row"])
        exposure = ", ".join(
            f"{name}: ${value:,.1f}m" for name, value in data["positions"].items())
        lines = [
            f"# Release brief: {mandate}",
            "",
            f"Prepared: {now.isoformat()}",
            f"Exposure: {exposure}",
            "",
        ]
        if events.empty:
            lines.append("No within-filing credit signals were found.")
        else:
            for event in events.itertuples():
                lines.append(
                    f"- {event.name} | {event.rule} | {event.before} to {event.after} | "
                    f"{event.adsh} row {event.row} (prior {event.prior_row})")
        briefs[mandate] = "\n".join(lines) + "\n"
    return {"briefs": briefs}


def hygiene_job(root=ROOT, now=None):
    """Find stale watchlist items and client reporting dates due soon."""
    root = Path(root)
    now = now or datetime.fromisoformat("2026-10-15T08:00:00-04:00")
    stale = []
    watch = re.compile(r"^- (?P<bdc>.+?): .+? \(added (?P<added>\d{4}-\d{2}-\d{2})\)$")
    for path in sorted((root / "agent" / "mandates").glob("*/WATCHLIST.md")):
        mandate = path.parent.name
        for line in path.read_text().splitlines():
            match = watch.match(line)
            if not match:
                continue
            added = datetime.fromisoformat(match.group("added")).date()
            age = (now.date() - added).days
            if age >= 30:
                stale.append({
                    "mandate": mandate,
                    "bdc": match.group("bdc"),
                    "added": added.isoformat(),
                    "age_days": age,
                })
    schedules = []
    pattern = re.compile(r"monthly note by the (\d+)(?:st|nd|rd|th) business day", re.I)
    for path in sorted((root / "agent" / "mandates").glob("*/MANDATE.md")):
        match = pattern.search(path.read_text())
        if not match:
            continue
        ordinal = int(match.group(1))
        year, month = now.year, now.month
        for attempt in range(3):
            candidate_month = month + attempt
            candidate_year = year + (candidate_month - 1) // 12
            candidate_month = (candidate_month - 1) % 12 + 1
            weekday = 0
            for day in range(1, 32):
                try:
                    candidate = datetime(candidate_year, candidate_month, day).date()
                except ValueError:
                    break
                if candidate.weekday() < 5:
                    weekday += 1
                if weekday == ordinal:
                    if candidate >= now.date():
                        schedules.append({
                            "mandate": path.parent.name,
                            "rule": f"{ordinal}th business day",
                            "due": candidate.isoformat(),
                            "within_window": (candidate - now.date()).days <= 5,
                        })
                    break
            if schedules and schedules[-1]["mandate"] == path.parent.name:
                break
    return {
        "stale_watchlist": stale,
        "reporting_schedule": schedules,
        "upcoming_reporting": [row for row in schedules if row["within_window"]],
        "reporting_window_end": (now.date() + timedelta(days=5)).isoformat(),
        "calendar_note": "Weekends excluded; public holidays are not modelled.",
    }
