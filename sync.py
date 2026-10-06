#!/usr/bin/env python3
"""Sync Strava activities + daily steps (both read from Echo) into Walktober.

Every run reconciles the whole challenge window (1 Oct .. today), so a day that was
partial at 21:00 is corrected on the next night, and a re-run is a no-op.

  sync.py --dry-run           show the plan, post nothing, send nothing
  sync.py                     post changed days, then send the Telegram report
  sync.py --no-telegram       post but print the report instead of sending it
"""
import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
import urllib.request
from collections import OrderedDict
from pathlib import Path
from zoneinfo import ZoneInfo

from mapping import map_activity
from walktober import Walktober, WalktoberError, load_env

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
POSTED = STATE / "posted.json"          # what WE put on each day, so user-added rows survive
ENV = load_env()
ECHO_DB = Path(ENV["ECHO_DB"])           # Echo's SQLite, opened read-only
ECHO_URL = ENV.get("ECHO_URL", "").rstrip("/")      # optional: Echo API, to refresh steps first
ECHO_ENV = ENV.get("ECHO_ENV_FILE")      # Echo's .env, read for its API key
TZ = ZoneInfo(ENV.get("TZ_NAME", "America/Los_Angeles"))
MAX_PER_DAY = 4                          # promotion app_settings.max_exercise_activities_per_day


# ---------- Echo (read-only) ----------

def echo_refresh_steps(log):
    """Ask Echo to pull the last 2 days from Google Health so today's steps are current.

    Echo's own syncs run at 01:30 and 07:30, so at 21:00 today's steps would be 13 h stale.
    Best effort: on failure we post what Echo has and say so in the report.
    """
    if not ECHO_URL:
        return True, None
    key = load_env(ECHO_ENV).get("COACH_API_KEY") if ECHO_ENV else None
    req = urllib.request.Request(
        f"{ECHO_URL}/api/google-health/sync", method="POST",
        data=json.dumps({"mode": "hourly", "days": 2}).encode(),
        headers={"Content-Type": "application/json", "X-O2-Auth-Key": key or ""})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            ok = json.loads(r.read()).get("ok")
        return bool(ok), None
    except Exception as e:  # noqa: BLE001 — report, don't fail the run
        log(f"echo steps refresh failed: {e}")
        return False, str(e)


def echo_db():
    return sqlite3.connect(f"file:{ECHO_DB}?mode=ro", uri=True)


def echo_steps(db, start, end):
    rows = db.execute(
        "SELECT substr(date,1,10), source_family, SUM(value) FROM health_metrics "
        "WHERE metric_type='steps' AND substr(date,1,10) BETWEEN ? AND ? "
        "GROUP BY 1, 2", (start, end)).fetchall()
    out = {}
    for day, fam, val in rows:   # prefer Google Health (Echo's active source), else Apple
        if fam == "google_health" or day not in out:
            out[day] = int(round(val or 0))
    return out


def echo_activities(db, start, end):
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT strava_id, sport_type, type, name, start_date_local, moving_time_s, "
        "elapsed_time_s, average_watts, average_heartrate, average_speed, device_name, "
        "json_extract(raw_json, '$.trainer') AS trainer "
        "FROM activities WHERE substr(start_date_local,1,10) BETWEEN ? AND ? "
        "ORDER BY start_date_local", (start, end)).fetchall()
    db.row_factory = None
    return [dict(r) for r in rows]


def drop_duplicates(acts):
    """The same session can arrive twice (e.g. Peloton and Fitbit Air both upload it).

    Two activities overlapping by more than half of the shorter one are one session;
    keep the one with power data, else the longer one.
    """
    def span(a):
        s = dt.datetime.fromisoformat(a["start_date_local"].replace("Z", ""))
        return s, s + dt.timedelta(seconds=a["elapsed_time_s"] or a["moving_time_s"] or 0)

    keep, dropped = [], []
    for a in acts:
        a0, a1 = span(a)
        clash = None
        for k in keep:
            k0, k1 = span(k)
            overlap = (min(a1, k1) - max(a0, k0)).total_seconds()
            shorter = min((a1 - a0).total_seconds(), (k1 - k0).total_seconds()) or 1
            if overlap > 0.5 * shorter:
                clash = k
                break
        if not clash:
            keep.append(a)
            continue
        better = max((clash, a), key=lambda x: (bool(x["average_watts"]), x["moving_time_s"] or 0))
        worse = a if better is clash else clash
        if better is a:
            keep[keep.index(clash)] = a
        dropped.append(worse)
    return keep, dropped


# ---------- planning ----------

def plan_day(acts, catalogue):
    """-> (rows [{id, minutes}], notes [str]) for one day's Strava activities."""
    merged, notes = OrderedDict(), []
    for a in acts:
        eid, why = map_activity(a)
        label = f"{a['name']} ({a['sport_type']})"
        if eid is None:
            notes.append(f"skipped {label}: {why}")
            continue
        minutes = max(1, round((a["moving_time_s"] or 0) / 60))
        merged[eid] = merged.get(eid, 0) + minutes
    rows = [{"id": k, "minutes": v} for k, v in merged.items()]
    rows.sort(key=lambda r: -r["minutes"] * catalogue[r["id"]]["steps_per_minute"])
    for r in rows[MAX_PER_DAY:]:
        notes.append(f"over the {MAX_PER_DAY}/day limit, dropped {catalogue[r['id']]['name']} {r['minutes']}m")
    return rows[:MAX_PER_DAY], notes


def current_rows(entry):
    return [{"id": int(x["exercise_activity_id"]), "minutes": int(x["minutes"] or 0)}
            for x in (entry or {}).get("user_exercise_activities", [])]


def key(rows):
    return sorted((r["id"], r["minutes"]) for r in rows)


# ---------- report ----------

def fmt_day(day):
    return dt.date.fromisoformat(day).strftime("%a %-d %b")


def send_telegram(env, text):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage", method="POST",
        data=json.dumps({"chat_id": env["TELEGRAM_CHAT_ID"], "text": text,
                         "disable_web_page_preview": True}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if not json.loads(r.read()).get("ok"):
            raise RuntimeError("telegram send not ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--no-refresh", action="store_true", help="skip asking Echo to refresh steps")
    args = ap.parse_args()
    env = ENV
    log_lines = []
    log = lambda m: (log_lines.append(m), print(m, file=sys.stderr))  # noqa: E731
    STATE.mkdir(exist_ok=True)

    today = dt.datetime.now(TZ).date()
    report, status, done = [], 0, []      # done = days actually posted (survives a mid-run failure)
    entries, catalogue, promo = {}, {}, None

    def line(ds):
        e = entries.get(ds) or {}
        out = [f"{fmt_day(ds)} — {e.get('total_steps', 0):,} steps · {e.get('total_points', 0)} pts",
               f"   • {e.get('manual_steps') or e.get('device_steps') or 0:,} steps (Fitbit)"]
        for x in e.get("user_exercise_activities", []):
            ex = catalogue.get(int(x["exercise_activity_id"]), {})
            out.append(f"   • {ex.get('name', '?')}: {x['minutes']} min → {x.get('steps') or 0:,} steps")
        return out

    try:
        w = Walktober(env["WALKTOBER_BASE"], env["WALKTOBER_EMAIL"], env["WALKTOBER_PASSWORD"])
        w.sign_in()
        promo = w.promotion()
        closes = promo["logging_ends_on"]
        if today.isoformat() > closes:
            report = [f"⏰ Walktober logging closed on {fmt_day(closes)} — nothing was posted.",
                      "Time to remove this automation — run",
                      "systemctl --user disable --now walktober.timer"]
            raise SystemExit  # skip the sync, still send the reminder
        start = promo["starts_on"]
        end = min(today.isoformat(), promo["ends_on"])
        catalogue = {a["id"]: a for a in w.exercise_activities(promo["id"])}

        refreshed, refresh_err = (True, None) if args.no_refresh or args.dry_run else echo_refresh_steps(log)
        db = echo_db()
        steps = echo_steps(db, start, end)
        acts, dupes = drop_duplicates(echo_activities(db, start, end))
        db.close()

        entries = w.entries()
        posted = json.loads(POSTED.read_text()) if POSTED.exists() else {}
        changed, notes_all, sent = [], [], {}
        day = dt.date.fromisoformat(start)
        try:
            while day.isoformat() <= end:
                ds = day.isoformat()
                ours, notes = plan_day([a for a in acts if a["start_date_local"][:10] == ds], catalogue)
                notes += [f"duplicate dropped: {a['name']} ({a['device_name']})"
                          for a in dupes if a["start_date_local"][:10] == ds]
                entry = entries.get(ds)
                have = current_rows(entry)
                # Rows the user added by hand (not ours last time) are kept, ours are replaced.
                prev_ours = {(r["id"], r["minutes"]) for r in posted.get(ds, [])}
                theirs = [r for r in have if (r["id"], r["minutes"]) not in prev_ours]
                want = (theirs + ours)[:MAX_PER_DAY]
                want_steps = steps.get(ds) or None
                if want_steps is None and entry and entry.get("manually_recorded"):
                    want_steps = entry.get("manual_steps")      # Echo has nothing: keep what's there
                have_steps = entry.get("manual_steps") if entry and entry.get("manually_recorded") else None
                if key(want) != key(have) or want_steps != have_steps:
                    changed.append(ds)
                    sent[ds] = (key(want), want_steps)
                    if not args.dry_run:
                        w.log_day(ds, want, want_steps)
                        posted[ds] = ours
                        done.append(ds)
                notes_all += [f"{fmt_day(ds)}: {n}" for n in notes]
                if args.dry_run:
                    desc = ", ".join(f"{catalogue[r['id']]['name']} {r['minutes']}m" for r in want) or "-"
                    print(f"{ds}  steps={want_steps}  {desc}  {'CHANGE' if ds in changed else 'same'}")
                day += dt.timedelta(days=1)
        finally:
            if not args.dry_run:
                POSTED.write_text(json.dumps(posted, indent=1, sort_keys=True))

        if not args.dry_run:
            entries = w.entries()   # read back what Walktober actually recorded
            bad = [d for d, (rows, st) in sent.items()
                   if key(current_rows(entries.get(d))) != rows
                   or (st is not None and (entries.get(d) or {}).get("manual_steps") != st)]
        else:
            bad = []

        # ----- report -----
        days = [d.isoformat() for d in (dt.date.fromisoformat(start) + dt.timedelta(n) for n in range(40))
                if d.isoformat() <= end]
        total = sum((entries.get(d) or {}).get("total_steps", 0) for d in days)
        pts = sum((entries.get(d) or {}).get("total_points", 0) for d in days)
        logged = sum(1 for d in days if (entries.get(d) or {}).get("total_steps"))
        if bad:
            status = 1
            report.append(f"❌ Walktober sync — posted, but {', '.join(fmt_day(d) for d in bad)} did not read back as sent")
        elif args.dry_run:
            report.append(f"🧪 Walktober dry run — would post {len(changed)} day(s)")
        elif changed:
            report.append(f"✅ Walktober sync succeeded — posted {len(changed)} day(s)")
        else:
            report.append("✅ Walktober sync succeeded — nothing new to post")
        report.append("")
        shown = [d for d in changed if d != end] + [end]
        for d in shown:
            tag = "posted" if d in changed else "unchanged"
            if d == end and end == today.isoformat():
                tag += ", day still in progress"
            block = line(d)
            block[0] += f"  [{tag}]"
            report += block
        report.append("")
        report.append(f"October to date: {total:,} steps · {pts} pts · {logged}/{len(days)} days logged")
        if notes_all:
            report.append("")
            report.append("Not posted:")
            report += [f"   • {n}" for n in notes_all]
        if not refreshed:
            report.append("")
            report.append(f"⚠️ Echo step refresh failed ({refresh_err}); today's steps may be stale")
        if promo["ends_on"] <= today.isoformat() <= closes or \
                (dt.date.fromisoformat(closes) - today).days <= 3:
            report.append("")
            report.append(f"⏰ Logging closes {fmt_day(closes)}. After that, remove this automation "
                          "(systemctl --user disable --now walktober.timer).")
    except SystemExit:
        pass
    except Exception as e:  # noqa: BLE001 — every failure must reach Telegram
        status = 1
        report = ["❌ Walktober sync FAILED", f"{type(e).__name__}: {e}"]
        if done:
            report += ["", "Posted before the failure:"] + [f"   • {fmt_day(d)}" for d in done]
        else:
            report += ["", "Nothing was posted."]

    report.insert(1 if report else 0, fmt_day(today.isoformat()) + dt.datetime.now(TZ).strftime(" · %H:%M"))
    text = "\n".join(report)
    run = {"at": dt.datetime.now(TZ).isoformat(), "dry_run": args.dry_run, "status": status,
           "posted": done, "report": text, "log": log_lines}
    (STATE / "last-run.json").write_text(json.dumps(run, indent=1))
    if args.dry_run or args.no_telegram:
        print(text)
        return status
    for attempt in (1, 2):   # one retry: the report is the only way you hear about a run
        try:
            send_telegram(env, text)
            break
        except Exception as e:  # noqa: BLE001
            print(f"telegram send attempt {attempt} failed: {e}", file=sys.stderr)
            if attempt == 2:
                print(text, file=sys.stderr)
                return 2
            time.sleep(30)
    print(text)
    return status

if __name__ == "__main__":
    sys.exit(main())
