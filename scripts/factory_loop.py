#!/usr/bin/env python3
"""The factory run loop (docs/factory-design.md section 9), with every side effect injected.

State lives in GitHub issue labels and .factory/log.jsonl, never in this process, so a crash, a pause or a
closed laptop resumes by running the loop again. The loop itself spends no tokens: it checks plan usage,
computes which issues may run, asks the issue-selector to order them, and starts one fresh headless manager
session per issue, so no session's context grows across issues.
Standard library only.
"""
from __future__ import annotations

import re
import time
from typing import Callable

STATE_LABELS = ("todo", "in-progress", "awaiting-merge", "blocked", "backlog")
DEPENDS_RE = re.compile(r"(?im)^\s*depends on\s+#(\d+)")
PICK_RE = re.compile(r"(?im)^\s*PICK:\s*#?(\d+)\s*(?:\|\s*(.*))?$")


def parse_depends(body: str | None) -> list[int]:
    return sorted({int(n) for n in DEPENDS_RE.findall(body or "")})


def label_names(issue: dict) -> set[str]:
    return {(x["name"] if isinstance(x, dict) else x) for x in issue.get("labels") or []}


def compute_pool(issues: list[dict]) -> list[dict]:
    """Issues the factory may start: open, labelled `todo`, in no other state, with every dependency closed.
    Each result carries `depends_on`. A dependency that isn't in the list counts as unmet."""
    closed = {i["number"] for i in issues if str(i.get("state", "")).upper() == "CLOSED"}
    pool = []
    for i in sorted(issues, key=lambda x: x["number"]):
        labels = label_names(i)
        if str(i.get("state", "")).upper() != "OPEN" or "todo" not in labels:
            continue
        if labels & (set(STATE_LABELS) - {"todo"}):
            continue
        deps = parse_depends(i.get("body"))
        if all(d in closed for d in deps):
            pool.append({"number": i["number"], "title": i.get("title", ""), "depends_on": deps,
                         "labels": sorted(labels - set(STATE_LABELS))})
    return pool


def parse_pick(text: str, allowed: set[int]) -> tuple[int, str] | None:
    """The selector's last `PICK: <n> | <reason>` line, if it names an issue in the pool."""
    found = PICK_RE.findall(text or "")
    if not found:
        return None
    n, reason = found[-1]
    return (int(n), reason.strip() or "no reason given") if int(n) in allowed else None


def choose(pool: list[dict], ask: Callable[[list[dict]], str | None]) -> tuple[int, str]:
    """One candidate needs no judgement. Otherwise ask the selector; on any failure take the lowest number."""
    if len(pool) == 1:
        return pool[0]["number"], "only candidate"
    try:
        picked = parse_pick(ask(pool) or "", {p["number"] for p in pool})
    except Exception:  # a broken selector must not stop the factory
        picked = None
    return picked or (pool[0]["number"], "selector unavailable or invalid; lowest number")


def usage_gate(check: Callable[[], dict], sleep: Callable[[float], None] = time.sleep,
               now: Callable[[], float] = time.time, log: Callable[[dict], None] = lambda e: None,
               max_unknown: int = 3, retry_wait: float = 60, resume_buffer: float = 60) -> str:
    """Block until plan usage allows work. Returns "go", or "stop" after `max_unknown` unknown readings in a row
    (the caller raises a blocker). A pause sleeps until the blocking window resets and continues silently."""
    unknown = 0
    while True:
        r = check()
        decision = r.get("decision")
        if decision == "go":
            return "go"
        if decision == "pause":
            unknown = 0
            resume_at = r.get("resume_at")
            wait = max(resume_at - now() + resume_buffer, retry_wait) if resume_at else retry_wait
            log({"kind": "usage_pause", "reasons": r.get("reasons"), "resume_at": resume_at})
            sleep(wait)
            log({"kind": "usage_resume"})
            continue
        unknown += 1
        log({"kind": "usage_unknown", "count": unknown, "reasons": r.get("reasons")})
        if unknown >= max_unknown:
            return "stop"
        sleep(retry_wait)


def run_loop(*, gate: Callable[[], str], fetch_issues: Callable[[], list[dict]],
             select: Callable[[list[dict]], tuple[int, str]], set_state: Callable[[int, str], None],
             run_issue: Callable[[int], int], fetch_labels: Callable[[int], set[str]],
             raise_blocker: Callable[[str, int | None, str], None], log: Callable[[dict], None],
             max_issues: int = 20) -> dict:
    """Work the pool one issue at a time until it is empty. Returns {"results": [...], "stopped": reason|None}."""
    results, attempted, stopped = [], set(), None
    for i in fetch_issues():  # a crashed earlier run leaves issues in-progress; never guess, hand them back
        if "in-progress" in label_names(i) and str(i.get("state", "")).upper() == "OPEN":
            set_state(i["number"], "blocked")
            raise_blocker("The factory stopped while this issue was in progress. Its branch may be half done.",
                          i["number"], "Check the branch, then relabel the issue `todo` to retry or close it.")
            log({"kind": "orphan_blocked", "issue": i["number"]})
    while len(results) < max_issues:
        if gate() == "stop":
            raise_blocker("Plan usage could not be read three times in a row, so the factory stopped.", None,
                          "Check the statusline capture or run `usage.py --probe`, then start the factory again.")
            stopped = "usage unknown"
            break
        pool = [p for p in compute_pool(fetch_issues()) if p["number"] not in attempted]
        if not pool:
            break
        n, reason = select(pool)
        attempted.add(n)
        log({"kind": "issue_picked", "issue": n, "reason": reason, "pool": [p["number"] for p in pool]})
        set_state(n, "in-progress")
        log({"kind": "harness_started", "issue": n})
        code = run_issue(n)
        labels = fetch_labels(n)
        if "in-progress" in labels:  # the session ended without recording an outcome
            set_state(n, "blocked")
            raise_blocker(f"The session for this issue ended (exit code {code}) without finishing or blocking it.",
                          n, "Read the session output, then relabel the issue `todo` to retry.")
            outcome = "blocked"
        else:
            outcome = next((s for s in STATE_LABELS if s in labels), "closed")
        log({"kind": "harness_finished", "issue": n, "exit": code, "outcome": outcome})
        results.append({"issue": n, "outcome": outcome})
    return {"results": results, "stopped": stopped}
