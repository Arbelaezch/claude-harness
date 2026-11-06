#!/usr/bin/env python3
"""Plan-usage check: how much of the 5-hour and weekly windows is used, and whether to keep going.

Two sources, both normalized to percent used (0-100) per window:
  snapshot  ~/.claude/statusline-latest.json, written by statusline_capture.py while a session is open
  probe     a one-turn headless `claude -p` run; its stream-json carries a rate_limit_event

Fails closed: if no fresh data is available the decision is "unknown", never "go".
Standard library only.

Usage: python3 usage.py [--probe] [--max-five-hour 80] [--max-weekly 90] [--max-age 300] [--json]
Exit codes: 0 go, 10 pause, 2 unknown.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

SNAPSHOT = Path.home() / ".claude" / "statusline-latest.json"
WINDOWS = ("five_hour", "seven_day")
EXIT = {"go": 0, "pause": 10, "unknown": 2}


def from_statusline(data: dict) -> dict:
    """Statusline JSON: rate_limits.<window>.{used_percentage 0-100, resets_at epoch}."""
    out = {}
    for w in WINDOWS:
        info = (data.get("rate_limits") or {}).get(w) or {}
        if info.get("used_percentage") is not None:
            out[w] = {"used_pct": float(info["used_percentage"]), "resets_at": info.get("resets_at"), "status": None}
    return {"windows": out, "using_overage": False}


def from_stream(text: str) -> dict:
    """Headless stream-json: the last rate_limit_event's unifiedWindows (utilization 0-1, resetsAt epoch)."""
    last = None
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "rate_limit_event":
            last = ev.get("rate_limit_info") or {}
    out = {}
    for w, info in ((last or {}).get("unifiedWindows") or {}).items():
        if w in WINDOWS and info.get("utilization") is not None:
            out[w] = {"used_pct": float(info["utilization"]) * 100, "resets_at": info.get("resetsAt"), "status": info.get("status")}
    return {"windows": out, "using_overage": bool((last or {}).get("isUsingOverage"))}


def read_snapshot(path: Path = SNAPSHOT, max_age: float = 300, now: float | None = None) -> dict | None:
    """The snapshot as a reading, or None when it is missing, unreadable or older than max_age seconds."""
    now = time.time() if now is None else now
    try:
        if now - path.stat().st_mtime > max_age:
            return None
        reading = from_statusline(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None
    return {**reading, "source": "snapshot"} if reading["windows"] else None


def run_probe(model: str = "haiku", timeout: int = 90) -> dict | None:
    """One cheap headless turn. None if claude is missing, fails, or emits no rate-limit event."""
    try:
        p = subprocess.run(
            ["claude", "-p", "Reply with the single word ok", "--output-format", "stream-json", "--verbose", "--model", model],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    reading = from_stream(p.stdout)
    return {**reading, "source": "probe"} if reading["windows"] else None


def decide(reading: dict | None, limits: dict, now: float | None = None) -> dict:
    """go / pause / unknown. A pause carries resume_at: when the blocking window resets."""
    now = time.time() if now is None else now
    if not reading:
        return {"decision": "unknown", "reasons": ["no fresh usage data"], "resume_at": None}
    reasons, resets = [], []
    for w in WINDOWS:
        info = reading["windows"].get(w)
        if info is None:
            return {"decision": "unknown", "reasons": [f"no {w} data"], "resume_at": None}
        if info.get("resets_at") is not None and info["resets_at"] < now:
            return {"decision": "unknown", "reasons": [f"{w} data predates its reset"], "resume_at": None}
        blocked = None
        if info["used_pct"] >= limits[w]:
            blocked = f"{w} at {info['used_pct']:.0f}% (limit {limits[w]:.0f}%)"
        elif info.get("status") not in (None, "allowed"):
            blocked = f"{w} status {info['status']}"
        if blocked:
            reasons.append(blocked)
            resets.append(info.get("resets_at"))
    if reading.get("using_overage"):
        reasons.append("using overage")
        resets += [i.get("resets_at") for i in reading["windows"].values()]
    if not reasons:
        return {"decision": "go", "reasons": [], "resume_at": None}
    known = [r for r in resets if r is not None]
    # Resuming needs every blocking window to have reset, so wait for the latest one.
    return {"decision": "pause", "reasons": reasons, "resume_at": max(known) if known else None}


def check(limits: dict, max_age: float = 300, probe: bool = False, probe_model: str = "haiku") -> dict:
    """Snapshot first; probe when asked or when the snapshot is unusable."""
    reading = read_snapshot(max_age=max_age)
    if probe or decide(reading, limits)["decision"] == "unknown":
        reading = run_probe(probe_model) or reading
    result = decide(reading, limits)
    result["source"] = (reading or {}).get("source")
    result["usage"] = {w: {"used_pct": round(i["used_pct"], 1), "resets_at": i["resets_at"]}
                       for w, i in ((reading or {}).get("windows") or {}).items()}
    return result


def fmt_epoch(t) -> str:
    return dt.datetime.fromtimestamp(t).astimezone().strftime("%Y/%m/%d %H:%M %Z") if t else "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-five-hour", type=float, default=80, help="pause at or above this %% of the 5-hour window")
    ap.add_argument("--max-weekly", type=float, default=90, help="pause at or above this %% of the weekly window")
    ap.add_argument("--max-age", type=float, default=300, help="seconds before the snapshot counts as stale")
    ap.add_argument("--probe", action="store_true", help="always take a fresh headless reading")
    ap.add_argument("--probe-model", default="haiku")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = check({"five_hour": a.max_five_hour, "seven_day": a.max_weekly}, a.max_age, a.probe, a.probe_model)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        for w, label in (("five_hour", "5-hour"), ("seven_day", "weekly")):
            u = r["usage"].get(w)
            if u:
                print(f"{label}: {u['used_pct']:.0f}% used, resets {fmt_epoch(u['resets_at'])}")
        print(f"{r['decision']}" + (f": {'; '.join(r['reasons'])}" if r["reasons"] else "")
              + (f" (resume {fmt_epoch(r['resume_at'])})" if r["resume_at"] else "")
              + (f" [source: {r['source']}]" if r["source"] else ""))
    return EXIT[r["decision"]]


if __name__ == "__main__":
    sys.exit(main())
