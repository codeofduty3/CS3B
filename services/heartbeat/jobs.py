"""Part 5: the heartbeat jobs. Each DAG in dags/ is a thin wrapper over one of these.

    release_job   06:00 daily      next SEC BDC release out? signals -> route -> alert or hold
    edgar_job     every 30 min     new 10-Q / 10-K / 8-K 2.02 from a BDC -> alert, hold or not-material
    brief_job     07:30 weekdays   on a release day: one brief per mandate, to Priya, with the held items
    hygiene_job   Mondays 08:00    stale watchlist entries; client reporting dates in 5 business days

Every job reads the injected clock (LANTERN_CLOCK) unless `now` is passed, reads recorded feeds in
fixture mode, and makes no model call: the heartbeat path is deterministic, so an idle tick costs
no tokens. State (last release seen, dedup keys, deliveries) lives in outputs/heartbeat/state.json,
so a re-run never alerts twice. Everything written goes under outputs/heartbeat/:

    alerts.jsonl     one line per alert or held item (graded: alert_id, ts, mandate, bdc, trigger,
                     rule, text, citations), only for a mandate that holds the BDC
    decisions.jsonl  every decision, including not-material filings (recall is graded on these)
    ticks.jsonl      one line per job run: what it found, requests made, model tokens, seconds
    briefs.jsonl     one line per brief delivered; the brief itself in briefs/<date>_<mandate>.md
    hygiene/<date>.json
"""
import json
import re
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [p for p in (str(ROOT), str(ROOT / "src")) if p not in sys.path]

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from creditwatch import channel, clock, heartbeat, panel, signals  # noqa: E402
from services.signals.pairing import pair  # noqa: E402

WL_LINE = re.compile(r"^- (?P<bdc>.+?): (?P<reason>.+) \(added (?P<added>\d{4}-\d{2}-\d{2})\)$")
GENERIC = {"FIXTURE", "CAPITAL", "CORP", "CORPORATION", "INC", "BDC", "LENDING", "CREDIT", "FUND",
           "SENIOR", "LLC", "LP", "TRUST", "INCOME", "INVESTMENT", "THE", "CO", "L.L.C."}


# ------------------------------------------------------------------ context and state
class Ctx:
    def __init__(self, root=ROOT, out=None):
        self.root = Path(root)
        self.P = yaml.safe_load((self.root / "params.yaml").read_text())
        d = self.P["data"]
        self.fx = self.root / d["fixture_dir"]
        self.fixture = d["use_fixture"]
        self.mandates = json.loads((self.root / d["mandates"]).read_text())
        self.registry = json.loads((self.root / d["registry"]).read_text())
        self.out = Path(out) if out else self.root / "outputs" / "heartbeat"
        self.out.mkdir(parents=True, exist_ok=True)
        self.state_file = self.out / "state.json"

    def panel(self):
        # Real mode: data.panel is the refreshed CS2B two-period panel for the new release.
        return panel.load_panel(self.root / self.P["data"]["panel"])

    def state(self):
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        last = (json.loads((self.fx / "release_heads.json").read_text())["last_seen"]
                if self.fixture else None)
        return {"last_seen": last, "release_days": {}, "seen": [], "delivered": {}}

    def save(self, st):
        self.state_file.write_text(json.dumps(st, indent=1, sort_keys=True))

    def append(self, name, rows):
        with open(self.out / name, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True, default=str) + "\n")


def _held(ctx):
    return {m: list(v["positions"]) for m, v in ctx.mandates.items()}


def _tick(ctx, job, now, t0, **fields):
    row = {"job": job, "ts": now.isoformat(), "seconds": round(time.perf_counter() - t0, 4),
           "model_tokens": 0, **fields}          # no model call on the heartbeat path
    ctx.append("ticks.jsonl", [row])
    return row


# ------------------------------------------------------------------ feeds (recorded or live)
def _head(ctx):
    if ctx.fixture:
        heads = json.loads((ctx.fx / "release_heads.json").read_text())["heads"]
        return lambda url: heads.get(url, 404)
    return heartbeat.head_live(ctx.P["sec"]["user_agent"])


def _recent(ctx, cik):
    """EDGAR submissions 'recent' block for one CIK: recorded in fixture mode, data.sec.gov live."""
    empty = {"form": [], "items": [], "accessionNumber": [], "acceptanceDateTime": []}
    if ctx.fixture:
        return json.loads((ctx.fx / "edgar_recent.json").read_text()).get(cik, empty)
    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    channel.require_host(url, _NoAudit())
    req = urllib.request.Request(url, headers={"User-Agent": ctx.P["sec"]["user_agent"]})
    with urllib.request.urlopen(req, timeout=20) as r:
        rec = json.load(r)["filings"]["recent"]
    return {k: rec.get(k, []) for k in empty}


class _NoAudit:
    def record(self, *a, **k):
        pass


# ------------------------------------------------------------------ alert text and citations
def _signal_text(s):
    b, a = s["before"], s["after"]
    if s["rule"] == "MARKDOWN":                 # msg-102: price changes in points, never percent
        what = f"marked down {b:.1f} -> {a:.1f} ({a - b:+.1f} points)"
    elif s["rule"] == "PIK_UP":
        what = f"PIK rate up {b:.2f}% -> {a:.2f}%"
    else:
        what = f"maturity extended {b} -> {a}"
    return (f"{s['name']}: {s['identifier']} {what}. "
            f"Filing {s['adsh']}, current row {s['row']}, prior row {s['prior_row']}.")


def _signal_alerts(ctx, found, urls, release, now):
    st = ctx.state()
    seen = set(st["seen"])
    out, dec = [], []
    for m, sig in heartbeat.route(found, _held(ctx)).items():
        by_key = {(r["adsh"], r["row"], r["rule"]): r for r in sig.to_dict("records")}
        for d in heartbeat.decide(sig, m, seen, now):
            s = by_key[(d["adsh"], d["row"], d["rule"])]
            a = {"alert_id": f"al-{d['key']}", "ts": now.isoformat(), "mandate": m, "bdc": s["name"],
                 "trigger": f"{s['adsh']}#{s['row']}", "rule": s["rule"], "text": _signal_text(s),
                 "citations": [{"adsh": s["adsh"], "row": str(s["row"]), "prior_row": str(s["prior_row"]),
                                "url": urls.get(s["adsh"])}],
                 "action": d["action"], "source": "release_watch_dag", "release": release,
                 "detected_at": now.isoformat()}
            out.append(a)
            dec.append({k: a[k] for k in ("alert_id", "ts", "mandate", "bdc", "trigger", "rule", "action", "source")})
    st["seen"] = sorted(seen)
    for a in out:
        if a["action"] == "alert":
            st["delivered"][a["alert_id"]] = now.isoformat()
    return out, dec, st


# ------------------------------------------------------------------ the four jobs
def release_job(now=None, root=ROOT, out=None, head=None):
    """Is the next monthly release out? If so: signals (our pairing), route, alert or hold."""
    t0, ctx = time.perf_counter(), Ctx(root, out)
    now = now or clock.clock()
    st = ctx.state()
    tick = heartbeat.release_tick(st["last_seen"], head or _head(ctx), ctx.P["sec"]["base_url"])
    alerts = []
    if tick["published"]:
        p = ctx.panel()
        found = signals.find_signals(p, ctx.P["heartbeat"]["materiality"], pair=pair)
        urls = p.drop_duplicates("adsh").set_index("adsh")["inlineurl"].to_dict() if "inlineurl" in p else {}
        alerts, dec, st = _signal_alerts(ctx, found, urls, tick["release"], now)
        st["last_seen"] = tick["release"]
        st["release_days"][now.date().isoformat()] = tick["release"]
        ctx.append("alerts.jsonl", alerts)
        ctx.append("decisions.jsonl", dec)
        ctx.save(st)
    acts = pd.Series([a["action"] for a in alerts], dtype=object).value_counts().to_dict()
    _tick(ctx, "release_watch", now, t0, release=tick["release"], published=tick["published"],
          requests=1, found=len(alerts), **{k: int(v) for k, v in acts.items()})
    return {**tick, "alerts": acts.get("alert", 0), "held": acts.get("hold", 0)}


def edgar_job(now=None, root=ROOT, out=None, recent=None):
    """New watched filings from every registered BDC: an alert or hold for a mandate that holds
    the BDC, a logged not-material decision for one that does not."""
    t0, ctx = time.perf_counter(), Ctx(root, out)
    now = now or clock.clock()
    st = ctx.state()
    seen = set(st["seen"])
    held = _held(ctx)
    alerts, dec = [], []
    for bdc, cik in ctx.registry["bdc_cik"].items():
        new = heartbeat.new_filings(recent(cik) if recent else _recent(ctx, cik), seen=set())
        for m in ctx.mandates:
            for d in heartbeat.filing_decisions(new, bdc, held[m], m, seen, now):
                aid = f"al-{d['key']}"
                dec.append({"alert_id": aid, "ts": now.isoformat(), "mandate": m, "bdc": bdc,
                            "trigger": d["accn"], "rule": "FILING", "form": d["form"],
                            "accepted": d["accepted"], "action": d["action"], "source": "edgar_watch_dag"})
                if d["action"] == "not_material":
                    continue
                url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{d['accn'].replace('-', '')}/"
                alerts.append({"alert_id": aid, "ts": now.isoformat(), "mandate": m, "bdc": bdc,
                               "trigger": d["accn"], "rule": "FILING",
                               "text": f"{bdc} filed {'an' if d['form'][0] == '8' else 'a'} {d['form']} "
                                       f"({d['accn']}), accepted {d['accepted']}.",
                               "citations": [{"accn": d["accn"], "form": d["form"], "url": url}],
                               "action": d["action"], "source": "edgar_watch_dag",
                               "accepted": d["accepted"], "detected_at": now.isoformat()})
                if d["action"] == "alert":
                    st["delivered"][aid] = now.isoformat()
    st["seen"] = sorted(seen)
    q = ctx.P["heartbeat"]["quiet_hours"]
    if not heartbeat.quiet(now, q["start"], q["end"]) and now.date().isoformat() not in st["release_days"]:
        # No brief today: whatever quiet hours held is delivered on the first tick after them.
        path = ctx.out / "alerts.jsonl"
        old = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
        for a in old + alerts:
            if a["action"] == "hold" and a["alert_id"] not in st["delivered"]:
                st["delivered"][a["alert_id"]] = now.isoformat()
                dec.append({"alert_id": a["alert_id"], "ts": now.isoformat(), "mandate": a["mandate"],
                            "bdc": a["bdc"], "trigger": a["trigger"], "rule": a["rule"],
                            "action": "delivered_after_quiet_hours", "source": "edgar_watch_dag"})
    ctx.append("alerts.jsonl", alerts)
    ctx.append("decisions.jsonl", dec)
    ctx.save(st)
    acts = pd.Series([d["action"] for d in dec], dtype=object).value_counts().to_dict()
    _tick(ctx, "edgar_watch", now, t0, requests=len(ctx.registry["bdc_cik"]), found=len(dec),
          **{k: int(v) for k, v in acts.items()})
    return acts


def _coverage(p, bdc):
    """How many of the BDC's debt holdings our pairing found in the prior schedule (AGENTS.md, 3)."""
    sub = p[p["name"] == bdc]
    if sub.empty:
        return None
    r = signals.pairing_report(sub, pair(sub))
    return int(r["paired"].sum()), int(r["debt_holdings"].sum())


def _brief_text(mandate, m, items, cov, day, release):
    lines = [f"Monthly brief, {mandate}, {day} (release {release})", "",
             "Exposure: " + "; ".join(f"{b} {v:g}m" for b, v in m["positions"].items()), "",
             "Coverage (debt holdings paired with the prior schedule): "
             + "; ".join(f"{b} {c[0]}/{c[1]}" if c else f"{b} not in panel" for b, c in cov.items()), ""]
    if not items:
        lines.append("Nothing material since the last release.")
    elif len(items) <= 3:                  # PRINCIPAL.md: plain text; a table only for more than three rows
        lines += [f"- {a['rule']}: {a['text']}" for a in items]
    else:
        lines += ["| BDC | Rule | Signal | Citation |", "|---|---|---|---|"]
        for a in items:
            c = a["citations"][0]
            cells = [a["bdc"], a["rule"], a["text"].split(": ", 1)[-1], c.get("adsh") or c.get("accn")]
            lines.append("| " + " | ".join(str(x).replace("|", "\\|") for x in cells) + " |")
    return "\n".join(lines) + "\n"


def brief_job(now=None, root=ROOT, out=None):
    """On a release day, weekdays: one brief per mandate, to Priya, with everything held for it."""
    t0, ctx = time.perf_counter(), Ctx(root, out)
    now = now or clock.clock()
    st = ctx.state()
    day = now.date().isoformat()
    release = st["release_days"].get(day)
    if now.weekday() >= 5 or release is None:
        _tick(ctx, "monthly_brief", now, t0, requests=0, found=0, briefs=0)
        return []
    path = ctx.out / "alerts.jsonl"
    alerts = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
    p = ctx.panel()
    (ctx.out / "briefs").mkdir(exist_ok=True)
    done = []
    for mandate, m in ctx.mandates.items():
        items = [a for a in alerts if a["mandate"] == mandate and a["action"] == "hold"
                 and a["alert_id"] not in st["delivered"]]
        cov = {b: _coverage(p, b) for b in m["positions"]}
        f = ctx.out / "briefs" / f"{day}_{mandate}.md"
        f.write_text(_brief_text(mandate, m, items, cov, day, release))
        for a in items:
            st["delivered"][a["alert_id"]] = now.isoformat()
        done.append({"brief_id": f"br-{day}-{mandate}", "ts": now.isoformat(), "mandate": mandate,
                     "to": ctx.P["agent"]["principal"], "release": release,
                     "path": str(f.relative_to(ctx.root)) if f.is_relative_to(ctx.root) else str(f),
                     "alert_ids": [a["alert_id"] for a in items]})
    ctx.append("briefs.jsonl", done)
    ctx.save(st)
    _tick(ctx, "monthly_brief", now, t0, requests=0, found=sum(len(b["alert_ids"]) for b in done), briefs=len(done))
    return done


def _business_days(start, n):
    d, out = start, []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def _nth_business_day(year, month, n):
    d = date(year, month, 1)
    k = 0
    while True:
        if d.weekday() < 5:
            k += 1
            if k == n:
                return d
        d += timedelta(days=1)


def hygiene_job(now=None, root=ROOT, out=None, stale_days=30, horizon=5):
    """Watchlist entries not reviewed in `stale_days`; reporting dates in the next `horizon` business days."""
    t0, ctx = time.perf_counter(), Ctx(root, out)
    now = now or clock.clock()
    today = now.date()
    ws = ctx.root / ctx.P["agent"]["workspace"] / "mandates"
    stale, due, undated = [], [], []
    for mandate in ctx.mandates:
        mdir = ws / mandate
        logs = sorted((mdir / "log").glob("*.md")) if (mdir / "log").exists() else []
        wl = (mdir / "WATCHLIST.md").read_text().splitlines() if (mdir / "WATCHLIST.md").exists() else []
        for e in filter(None, map(WL_LINE.match, wl)):
            words = [w for w in e["bdc"].upper().split() if w not in GENERIC]
            last = date.fromisoformat(e["added"])
            for f in logs:                 # a log line that says it reviewed this BDC counts as a review
                for line in f.read_text().splitlines():
                    if "review" in line.lower() and any(w.lower() in line.lower() for w in words):
                        last = max(last, date.fromisoformat(f.stem))
            age = (today - last).days
            if age > stale_days:
                stale.append({"mandate": mandate, "bdc": e["bdc"], "reason": e["reason"],
                              "last_reviewed": last.isoformat(), "days": age})
        md = (mdir / "MANDATE.md").read_text() if (mdir / "MANDATE.md").exists() else ""
        rep = md.split("## Reporting", 1)[-1].split("##", 1)[0].strip() if "## Reporting" in md else ""
        nth = re.search(r"by the (\d+)(?:st|nd|rd|th) business day", rep)
        window = _business_days(today, horizon)
        if nth:
            for y, mo in {(d.year, d.month) for d in [today, *window]}:
                d = _nth_business_day(y, mo, int(nth.group(1)))
                if window[0] <= d <= window[-1]:
                    due.append({"mandate": mandate, "due": d.isoformat(), "what": rep})
        else:
            undated.append({"mandate": mandate, "what": rep})     # no date to count down to
    res = {"ts": now.isoformat(), "stale_watchlist": stale, "reporting_due": due,
           "undated_reporting": undated}
    (ctx.out / "hygiene").mkdir(exist_ok=True)
    (ctx.out / "hygiene" / f"{today.isoformat()}.json").write_text(json.dumps(res, indent=1))
    _tick(ctx, "hygiene", now, t0, requests=0, found=len(stale) + len(due))
    return res
