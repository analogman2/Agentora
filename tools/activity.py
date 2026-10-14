#!/usr/bin/env python3
"""Who's using Agentora: reads the shared activity log (logs/activity-YYYY-MM.jsonl) and the live radio listeners.

  tools/activity.py                 who's here now (active in the last 10 minutes) and who's listening
  tools/activity.py --since 24h     a timeline per person and device (also 90m, 7d)
  tools/activity.py --since 24h --all   include the "still here" heartbeats
"""
import argparse, json, time, urllib.request
from collections import defaultdict
from pathlib import Path

LOGS = Path(__file__).resolve().parent.parent / "logs"
ap = argparse.ArgumentParser()
ap.add_argument("--since", help="e.g. 90m, 24h, 7d")
ap.add_argument("--all", action="store_true", help="include heartbeats (active, listening)")
ap.add_argument("--now", type=int, default=10, help="minutes that count as 'here now' (default 10)")
A = ap.parse_args()


def span(s):
    return int(s[:-1]) * {"m": 60, "h": 3600, "d": 86400}[s[-1]]


def records(since):
    out = []
    for f in sorted(LOGS.glob("activity-*.jsonl"))[-3:]:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                r["ts"] = time.mktime(time.strptime(r["t"], "%Y-%m-%d %H:%M:%S"))
            except (ValueError, KeyError):
                continue
            if r["ts"] >= since:
                out.append(r)
    return out


def who(r):
    return r.get("user") or "not signed in"


if A.since:
    rs = records(time.time() - span(A.since))
    if not A.all:
        rs = [r for r in rs if r["event"] not in ("active", "listening")]
    people = defaultdict(list)
    for r in rs:
        people[(who(r), r.get("device") or "?")].append(r)
    print(f"Activity since {time.strftime('%a %H:%M', time.localtime(time.time() - span(A.since)))}: {len({w for w, _ in people})} people, {len(people)} devices")
    for (w, dev), items in sorted(people.items(), key=lambda kv: kv[1][0]["ts"]):
        print(f"\n{w} on {dev}")
        for r in items:
            extra = ", ".join(f"{k}={v}" for k, v in r.items() if k not in ("t", "ts", "app", "user", "device", "ip", "event"))
            print(f"  {r['t'][5:16]}  {r['app']:<11} {r['event']}{'  (' + extra + ')' if extra else ''}")
else:
    cutoff = time.time() - A.now * 60
    rs = records(time.time() - 86400)
    here = {}
    for r in rs:
        if r["ts"] >= cutoff and r.get("device") != "script" and r["event"] not in ("sign out", "sign-in failed"):
            here[(who(r), r.get("device") or "?")] = r
        if r["event"] == "sign out":
            here.pop((who(r), r.get("device") or "?"), None)
    try:
        lis = json.loads(urllib.request.urlopen("http://127.0.0.1:7860/api/radio/listeners", timeout=5).read())["listeners"]
    except Exception:
        lis = None
    users = {w for w, _ in here if w != "not signed in"}
    print(f"Here now (active in the last {A.now} min): {len(users)} signed-in user{'s' if len(users) != 1 else ''}"
          f"{' (' + ', '.join(sorted(users)) + ')' if users else ''}, {len(here)} device{'s' if len(here) != 1 else ''}")
    for (w, dev), r in sorted(here.items()):
        print(f"  {w:<15} {dev:<28} last: {r['event']} on {r['app']} at {r['t'][11:16]}")
    if lis is not None:
        print(f"Radio listeners right now: {len(lis)}")
        for l in lis:
            print(f"  {l.get('device') or '?'}  for {round((l.get('seconds') or l.get('for') or 0) / 60)} min" if isinstance(l, dict) else f"  {l}")
