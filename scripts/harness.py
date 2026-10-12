#!/usr/bin/env python3
"""Harness CLI (Claude Code plugin).

Deterministic plumbing for the /ship pipeline: run ledger, metrics hooks, git
guard, test gates, the cross-lab reviewer call, loop decisions and reports.
Standard library only. Requires Python 3.11+ (tomllib).

Usage: python3 <plugin>/scripts/harness.py <command> [...]   (run with -h for details)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if sys.version_info < (3, 8):
    sys.exit("harness needs Python 3.8 or newer.")
try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - older Pythons use the small parser below
    tomllib = None

PLUGIN_DIR = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = PLUGIN_DIR / "scripts" / "defaults.toml"
PROJECT_CONFIG_REL = Path(".claude") / "harness.toml"


def _find_root() -> tuple[Path, bool]:
    """Project root = the nearest folder with .claude/harness.toml. Order: --root, $HARNESS_ROOT,
    $CLAUDE_PROJECT_DIR (set for hooks), then walk up from the current directory."""
    explicit = None
    if "--root" in sys.argv:
        i = sys.argv.index("--root")
        if i + 1 < len(sys.argv):
            explicit = sys.argv[i + 1]
            del sys.argv[i:i + 2]
    for cand in (explicit, os.environ.get("HARNESS_ROOT")):
        if cand:
            p = Path(cand).expanduser().resolve()
            return p, (p / PROJECT_CONFIG_REL).exists() or bool(os.environ.get("HARNESS_CONFIG"))
    starts = [os.environ.get("CLAUDE_PROJECT_DIR"), os.getcwd()]
    for start in starts:
        if not start:
            continue
        p = Path(start).resolve()
        for d in (p, *p.parents):
            if (d / PROJECT_CONFIG_REL).exists():
                return d, True
    base = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    return base, bool(os.environ.get("HARNESS_CONFIG"))


ROOT, CONFIGURED = _find_root()
STATE_DIR = ROOT / ".harness"
RUNS_DIR = STATE_DIR / "runs"
METRICS_DIR = STATE_DIR / "metrics"
EVENTS_FILE = METRICS_DIR / "events.jsonl"
SEEN_DIR = METRICS_DIR / "seen"
CURRENT_FILE = STATE_DIR / "current"
AGENTS_DIR = PLUGIN_DIR / "agents"
HARNESS_SCRIPT = Path(__file__).resolve()
SEVERITIES = ("blocking", "should_fix", "nit")


# --------------------------------------------------------------------------- utils
def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None = None) -> str:
    return (t or now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class _MiniToml:
    """Small TOML reader for Pythons without tomllib (3.8-3.10). Supports what config.toml
    uses: [tables] incl. quoted parts, dotted/quoted keys, strings, numbers, booleans,
    (multi-line) arrays and inline tables, and comments."""

    def __init__(self, text: str):
        self.s, self.i, self.root = text, 0, {}

    def err(self, msg):
        line = self.s.count("\n", 0, self.i) + 1
        raise ValueError(f"TOML line {line}: {msg}")

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip(self, newlines=False):
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t\r" or (newlines and c == "\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def key(self):
        parts = []
        while True:
            self.skip()
            c = self.peek()
            if c in "\"'":
                parts.append(self.string())
            else:
                m = re.match(r"[A-Za-z0-9_-]+", self.s[self.i:])
                if not m:
                    self.err("expected a key")
                parts.append(m.group(0))
                self.i += len(m.group(0))
            self.skip()
            if self.peek() == ".":
                self.i += 1
                continue
            return parts

    def string(self):
        q = self.peek()
        self.i += 1
        out = []
        while True:
            if self.i >= len(self.s) or self.s[self.i] == "\n":
                self.err("unterminated string")
            c = self.s[self.i]
            if c == q:
                self.i += 1
                return "".join(out)
            if c == "\\" and q == '"':
                nxt = self.s[self.i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}.get(nxt, nxt))
                self.i += 2
                continue
            out.append(c)
            self.i += 1

    def value(self):
        self.skip()
        c = self.peek()
        if c in "\"'":
            return self.string()
        if c == "[":
            self.i += 1
            arr = []
            while True:
                self.skip(True)
                if self.peek() == "]":
                    self.i += 1
                    return arr
                arr.append(self.value())
                self.skip(True)
                if self.peek() == ",":
                    self.i += 1
                elif self.peek() != "]":
                    self.err("expected , or ] in array")
        if c == "{":
            self.i += 1
            tbl = {}
            while True:
                self.skip()
                if self.peek() == "}":
                    self.i += 1
                    return tbl
                k = self.key()
                if self.peek() != "=":
                    self.err("expected =")
                self.i += 1
                self.assign(tbl, k, self.value())
                self.skip()
                if self.peek() == ",":
                    self.i += 1
        m = re.match(r"[^\s,\]}#]+", self.s[self.i:])
        if not m:
            self.err("expected a value")
        tok = m.group(0)
        self.i += len(tok)
        if tok in ("true", "false"):
            return tok == "true"
        try:
            return int(tok.replace("_", ""))
        except ValueError:
            try:
                return float(tok.replace("_", ""))
            except ValueError:
                self.err(f"bad value {tok!r}")

    @staticmethod
    def assign(tbl, parts, val):
        for p in parts[:-1]:
            tbl = tbl.setdefault(p, {})
        tbl[parts[-1]] = val

    def parse(self):
        cur = self.root
        while True:
            self.skip(True)
            if self.i >= len(self.s):
                return self.root
            if self.peek() == "[":
                self.i += 1
                parts = self.key()
                if self.peek() != "]":
                    self.err("expected ]")
                self.i += 1
                cur = self.root
                for p in parts:
                    cur = cur.setdefault(p, {})
            else:
                k = self.key()
                if self.peek() != "=":
                    self.err("expected =")
                self.i += 1
                self.assign(cur, k, self.value())
            self.skip()
            if self.peek() not in ("\n", ""):
                self.err("unexpected text after value")


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    if tomllib is not None and not os.environ.get("HARNESS_FORCE_MINITOML"):
        with open(path, "rb") as f:
            return tomllib.load(f)
    return _MiniToml(path.read_text(encoding="utf-8")).parse()


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "repos":
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def project_config_path() -> Path:
    return Path(os.environ.get("HARNESS_CONFIG") or ROOT / PROJECT_CONFIG_REL)


def load_config() -> dict:
    """Plugin defaults (scripts/defaults.toml) overlaid with the project's .claude/harness.toml."""
    return _deep_merge(_read_toml(DEFAULTS_FILE), _read_toml(project_config_path()))


def require_project():
    if not CONFIGURED:
        sys.exit(f"No harness config found for {ROOT} (looked for .claude/harness.toml here and in parent "
                 f"folders). Run /harness-init in this project first.")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def log_event(event: dict) -> None:
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": iso(), **event}
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events() -> list[dict]:
    out = []
    if EVENTS_FILE.exists():
        for line in EVENTS_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def slugify(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:n].rstrip("-")) or "task"


def run_cmd(args, cwd=None, timeout=None, env=None, shell=False) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, timeout=timeout, env=env, shell=shell,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def git(repo: Path, *args: str) -> str:
    r = run_cmd(["git", "-C", str(repo), *args])
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}")
    return r.stdout


def is_git_repo(path: Path) -> bool:
    return path.is_dir() and run_cmd(["git", "-C", str(path), "rev-parse", "--git-dir"]).returncode == 0


# --------------------------------------------------------------------------- runs
def current_run_id() -> str | None:
    if CURRENT_FILE.exists():
        rid = CURRENT_FILE.read_text().strip()
        if rid and (RUNS_DIR / rid).is_dir():
            return rid
    return None


def run_dir(run_id: str | None = None) -> Path:
    rid = run_id or current_run_id()
    if not rid:
        sys.exit("No active run. Start one with: harness run start --task '...'")
    d = RUNS_DIR / rid
    if not d.is_dir():
        sys.exit(f"Run not found: {rid}")
    return d


def load_run(run_id: str | None = None) -> tuple[Path, dict]:
    d = run_dir(run_id)
    return d, read_json(d / "run.json", {})


def save_run(d: Path, run: dict) -> None:
    write_json(d / "run.json", run)


def repos(cfg: dict) -> dict[str, dict]:
    out = {}
    for name, r in (cfg.get("repos") or {}).items():
        out[name] = {**r, "abs": (ROOT / r.get("path", name)).resolve()}
    return out


# --------------------------------------------------------------------------- github / remotes
GH = os.environ.get("HARNESS_GH", "gh")
NET_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


_GH_ENV = None


def gh_env() -> dict:
    """Environment for gh: if the project names a GitHub account, use that account's token so
    work and personal projects hit GitHub as the right user."""
    global _GH_ENV
    if _GH_ENV is None:
        _GH_ENV = dict(NET_ENV)
        user = ((load_config().get("accounts") or {}).get("github") or "").strip() if CONFIGURED else ""
        if user and not os.environ.get("GH_TOKEN"):
            try:
                r = subprocess.run([GH, "auth", "token", "--user", user], capture_output=True, text=True, timeout=20)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                r = None
            if r is None or r.returncode != 0 or not r.stdout.strip():
                raise RuntimeError(f"This project uses GitHub account '{user}', but gh has no login for it. "
                                   f"Run `gh auth login` and sign in as {user} (gh keeps several accounts).")
            _GH_ENV["GH_TOKEN"] = r.stdout.strip()
    return _GH_ENV


def gh(*args: str, input_text: str | None = None, timeout: int = 60) -> str:
    try:
        r = subprocess.run([GH, *args], input=input_text, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=gh_env())
    except FileNotFoundError:
        raise RuntimeError("GitHub CLI `gh` not found. Install it (brew install gh) and run `gh auth login`.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gh {' '.join(args[:3])} timed out")
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[:600]}")
    return r.stdout


def gh_json(*args: str):
    out = gh(*args)
    return json.loads(out) if out.strip() else None


def git_net(repo: Path, *args: str, timeout: int = 120) -> tuple[bool, str]:
    """Network git command (fetch) that can't hang on a credentials prompt."""
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=NET_ENV)
        return r.returncode == 0, (r.stderr or r.stdout).strip()
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"


def repo_slug(path: Path) -> str | None:
    """owner/name from the origin URL (works with ssh host aliases like github-work)."""
    r = run_cmd(["git", "-C", str(path), "remote", "get-url", "origin"])
    if r.returncode != 0:
        return None
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", r.stdout.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def slug_map(cfg: dict) -> dict[str, str]:
    """lower-case owner/name -> harness repo name."""
    out = {}
    for name, r in repos(cfg).items():
        sl = r.get("github") or (repo_slug(r["abs"]) if is_git_repo(r["abs"]) else None)
        if sl:
            out[sl.lower()] = name
    return out


def repo_aliases(cfg: dict) -> dict[str, str]:
    """Names a user might type for a repo: harness name, folder name, GitHub repo name."""
    out = {}
    for name, r in repos(cfg).items():
        out[name.lower()] = name
        out[Path(r.get("path", name)).name.lower()] = name
    for sl, name in slug_map(cfg).items():
        out[sl.split("/")[1]] = name
    return out


def parse_gh_ref(ref: str, cfg: dict, default_repo: str | None = None) -> tuple[str, int]:
    """Accepts a URL, owner/name#N, repo#N (harness, folder or GitHub name) or #N with a default repo."""
    ref = ref.strip()
    m = re.search(r"github\.com/([^/]+)/([^/]+)/(?:issues|pull)/(\d+)", ref)
    if m:
        sl = f"{m.group(1)}/{m.group(2)}".lower()
        name = slug_map(cfg).get(sl)
        if not name:
            raise RuntimeError(f"{sl} isn't one of the workspace repos")
        return name, int(m.group(3))
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+)#(\d+)", ref)
    if m:
        name = slug_map(cfg).get(f"{m.group(1)}/{m.group(2)}".lower())
        if not name:
            raise RuntimeError(f"{m.group(1)}/{m.group(2)} isn't one of the workspace repos")
        return name, int(m.group(3))
    m = re.fullmatch(r"([\w.-]+)#(\d+)", ref)
    if m:
        name = repo_aliases(cfg).get(m.group(1).lower())
        if not name:
            raise RuntimeError(f"Unknown repo '{m.group(1)}' (try {', '.join(repos(cfg))})")
        return name, int(m.group(2))
    m = re.fullmatch(r"#?(\d+)", ref)
    if m and default_repo:
        return default_repo, int(m.group(1))
    raise RuntimeError(f"Can't tell which repo '{ref}' belongs to. Use a URL or <repo>#<number>, "
                       f"e.g. frontend#431 or perks_2.0#431.")


def slug_of(cfg: dict, rname: str) -> str:
    r = repos(cfg)[rname]
    sl = r.get("github") or repo_slug(r["abs"])
    if not sl:
        raise RuntimeError(f"Can't find the GitHub repo for {rname} (no origin remote)")
    return sl


LABEL_TYPES = [("hotfix", "hotfix"), ("bug", "bug"), ("fix", "fix"), ("ui", "ui"), ("design", "ui"),
               ("enhancement", "feat"), ("feature", "feat")]


def ensure_state_excluded() -> None:
    """If the project root is itself inside a git repo (single-repo projects), add .harness/ to that
    repo's local .git/info/exclude so run files never show up as changes. Nothing is committed."""
    r = run_cmd(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel", "--git-common-dir"])
    if r.returncode != 0:
        return
    top, gitdir = r.stdout.strip().splitlines()[:2]
    gitdir = Path(gitdir) if os.path.isabs(gitdir) else Path(top) / gitdir
    try:
        rel = (ROOT / ".harness").resolve().relative_to(Path(top).resolve()).as_posix()
    except ValueError:
        return
    excl = gitdir / "info" / "exclude"
    line = f"/{rel}/"
    current = excl.read_text(encoding="utf-8") if excl.exists() else ""
    if line not in current.splitlines():
        excl.parent.mkdir(parents=True, exist_ok=True)
        with open(excl, "a", encoding="utf-8") as f:
            f.write(("" if current.endswith("\n") or not current else "\n") + f"# harness run files\n{line}\n")


def new_run(cfg: dict, task: str, mode: str, force: bool = False, extra: dict | None = None) -> tuple[Path, dict]:
    ensure_state_excluded()
    if current_run_id() and not force:
        rid = current_run_id()
        sys.exit(f"Run {rid} is still active. End it first (harness run end --status aborted) or pass --force.")
    rid = f"{now():%Y%m%d-%H%M%S}-{slugify(task, 32)}"
    d = RUNS_DIR / rid
    d.mkdir(parents=True)
    run = {"id": rid, "task": task, "mode": mode, "status": "running",
           "started_at": iso(), "repos": {}, "review_rounds": 0, **(extra or {})}
    save_run(d, run)
    CURRENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(rid)
    log_event({"kind": "run_start", "run_id": rid, "mode": mode})
    return d, run


def cmd_run_start(a, cfg):
    if not a.task and not a.issue:
        sys.exit("Give --task and/or --issue.")
    issue, task_md = None, None
    if a.issue:
        rname, num = parse_gh_ref(a.issue, cfg, a.repo)
        sl = slug_of(cfg, rname)
        try:
            data = gh_json("issue", "view", str(num), "--repo", sl, "--json",
                           "number,title,body,labels,url,state,comments,assignees")
        except RuntimeError as exc:
            sys.exit(str(exc))
        labels = [lb.get("name", "") for lb in data.get("labels") or []]
        btype = next((t for key, t in LABEL_TYPES if any(key in lb.lower() for lb in labels)), "feat")
        issue = {"repo": rname, "slug": sl, "number": num, "title": data.get("title", ""),
                 "url": data.get("url"), "labels": labels, "state": data.get("state"), "branch_type": btype}
        comments = (data.get("comments") or [])[-12:]
        task_md = (f"# Task: {sl}#{num} {data.get('title', '')}\n\n{data.get('url', '')}\n"
                   f"Labels: {', '.join(labels) or 'none'}   State: {data.get('state')}\n\n"
                   f"## Issue description\n\n{(data.get('body') or '(empty)').strip()}\n")
        if comments:
            task_md += "\n## Comments\n"
            for c in comments:
                task_md += f"\n**{(c.get('author') or {}).get('login', '?')}:** {(c.get('body') or '').strip()[:2500]}\n"
        if a.task:
            task_md += f"\n## Extra instructions from the user\n\n{a.task}\n"
    task = f"{issue['slug']}#{issue['number']}: {issue['title']}" if issue else a.task
    d, run = new_run(cfg, task, a.mode, a.force, {"issue": issue} if issue else None)
    (d / "task.md").write_text(task_md or f"# Task\n\n{a.task}\n", encoding="utf-8")
    out = {"run_id": run["id"], "run_dir": str(d), "harness": f"python3 {HARNESS_SCRIPT} --root {ROOT}",
           "git_mode": (cfg.get("git") or {}).get("mode", "uncommitted")}
    if cfg.get("models"):
        out["model_overrides"] = cfg["models"]
    if issue:
        out.update({"issue": f"{issue['slug']}#{issue['number']}", "issue_title": issue["title"],
                    "issue_repo": issue["repo"], "suggested_branch_type": issue["branch_type"]})
    print(json.dumps(out, indent=2))


def manager_usage(run: dict, cfg: dict) -> dict | None:
    """Usage of the main (manager) session between run start and now."""
    path = run.get("session_transcript")
    if not path or not Path(path).exists():
        return None
    start, end = parse_ts(run.get("started_at")), now()
    entries = []
    for e in iter_transcript(Path(path)):
        if e.get("isSidechain"):
            continue
        t = parse_ts(e.get("timestamp"))
        if t and start and start <= t <= end:
            entries.append(e)
    return summarize_entries(entries, cfg, seen=set())


def cmd_run_end(a, cfg):
    d, run = load_run(a.run)
    if run.get("status") != "running" and not a.force:
        sys.exit(f"Run {run['id']} already ended with status {run.get('status')}.")
    usage = manager_usage(run, cfg)
    if usage and usage["requests"]:
        log_event({"kind": "agent", "run_id": run["id"], "stage": "manager", **usage})
    run.update(status=a.status, ended_at=iso())
    if a.note:
        run["note"] = a.note
    save_run(d, run)
    totals = run_totals(run["id"])
    log_event({"kind": "run_end", "run_id": run["id"], "status": a.status, "mode": run.get("mode"),
               "duration_s": round((now() - parse_ts(run["started_at"])).total_seconds()),
               "review_rounds": run.get("review_rounds", 0), **totals})
    if current_run_id() == run["id"]:
        CURRENT_FILE.unlink(missing_ok=True)
    print(f"Run {run['id']} ended: {a.status}")
    print(f"  Claude (API-equivalent): ${totals['claude_cost_usd_est']:.2f}   "
          f"outside models (actual): ${totals['external_cost_usd']:.3f}")


def cmd_run_show(a, cfg):
    d, run = load_run(a.run)
    print(json.dumps(run, indent=2))
    print("\nFiles:", ", ".join(sorted(p.name for p in d.iterdir())))
    t = run_totals(run["id"])
    print(f"\nCost so far: Claude ${t['claude_cost_usd_est']:.2f} (API-equivalent), "
          f"outside ${t['external_cost_usd']:.3f}")


def run_totals(run_id: str) -> dict:
    claude = ext = 0.0
    for e in read_events():
        if e.get("run_id") != run_id or e.get("kind") not in ("agent", "llm_call"):
            continue
        c = e.get("cost_usd_est") or 0.0
        if e.get("gen_ai.system") == "anthropic":
            claude += c
        else:
            ext += c
    return {"claude_cost_usd_est": round(claude, 4), "external_cost_usd": round(ext, 4)}


# --------------------------------------------------------------------------- transcripts & pricing
def iter_transcript(path: Path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def price_for(model: str, cfg: dict) -> dict | None:
    table = cfg.get("pricing") or {}
    best = None
    for key in table:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return table.get(best) if best else None


def claude_request_cost(usage: dict, model: str, cfg: dict) -> float | None:
    p = price_for(model, cfg)
    if not p:
        return None
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cc = usage.get("cache_creation_input_tokens", 0) or 0
    detail = usage.get("cache_creation") or {}
    c1h = detail.get("ephemeral_1h_input_tokens", 0) or 0
    c5m = detail.get("ephemeral_5m_input_tokens", cc - c1h) or 0
    pin, pout, pcr = p["input"], p["output"], p.get("cache_read", p["input"] * 0.1)
    thr = p.get("long_context_threshold")
    if thr and inp + cr + cc > thr:
        pin, pout, pcr = p.get("long_input", pin), p.get("long_output", pout), p.get("long_cache_read", pcr)
    cost = (inp * pin + out * pout + cr * pcr
            + c5m * p.get("cache_write_5m", pin * 1.25) + c1h * p.get("cache_write_1h", pin * 2)) / 1e6
    return cost


def summarize_entries(entries: list[dict], cfg: dict, seen: set[str]) -> dict:
    """Aggregate assistant usage. Streaming writes one line per content block with
    the same message id and usage, so usage is counted once per message id."""
    models: dict[str, int] = {}
    tin = tout = tcr = tcc = 0
    cost = 0.0
    cost_known = True
    requests = 0
    tool_ids: set[str] = set()
    first = last = None
    new_ids = []
    already = set(seen)
    # Only the part of the transcript not counted before (a resumed agent appends to its file).
    start = 0
    for i, e in enumerate(entries):
        mid = (e.get("message") or {}).get("id") if e.get("type") == "assistant" else None
        if mid and mid in already:
            start = i + 1
    entries = entries[start:]
    for e in entries:
        t = parse_ts(e.get("timestamp"))
        if t:
            first = t if first is None or t < first else first
            last = t if last is None or t > last else last
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        mid = msg.get("id")
        if mid in already:
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_ids.add(block.get("id") or f"{mid}-{len(tool_ids)}")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        new_ids.append(mid)
        u = msg.get("usage") or {}
        model = msg.get("model") or "unknown"
        if model == "<synthetic>":
            continue
        requests += 1
        models[model] = models.get(model, 0) + 1
        tin += u.get("input_tokens", 0) or 0
        tout += u.get("output_tokens", 0) or 0
        tcr += u.get("cache_read_input_tokens", 0) or 0
        tcc += u.get("cache_creation_input_tokens", 0) or 0
        c = claude_request_cost(u, model, cfg)
        if c is None:
            cost_known = False
        else:
            cost += c
    model = max(models, key=models.get) if models else "unknown"
    return {
        "gen_ai.system": "anthropic",
        "gen_ai.request.model": model,
        "models": models,
        "requests": requests,
        "gen_ai.usage.input_tokens": tin,
        "gen_ai.usage.output_tokens": tout,
        "gen_ai.usage.cache_read_tokens": tcr,
        "gen_ai.usage.cache_creation_tokens": tcc,
        "cost_usd_est": round(cost, 5) if cost_known else (round(cost, 5) if cost else None),
        "cost_complete": cost_known,
        "billing": (cfg.get("metrics") or {}).get("anthropic_billing", "seat"),
        "tool_uses": len(tool_ids),
        "duration_s": round((last - first).total_seconds()) if first and last else None,
        "_new_ids": new_ids,
    }


def find_agent_transcript(payload: dict) -> Path | None:
    p = payload.get("agent_transcript_path")
    if p and Path(p).exists():
        return Path(p)
    aid, sid, tp = payload.get("agent_id"), payload.get("session_id"), payload.get("transcript_path")
    if not aid or not tp:
        return None
    base = Path(tp).parent
    cand = base / (sid or "") / "subagents" / f"agent-{aid}.jsonl"
    if cand.exists():
        return cand
    hits = list(base.glob(f"**/agent-{aid}.jsonl"))
    return hits[0] if hits else None


# --------------------------------------------------------------------------- hooks
def hook_subagent_stop(cfg):
    payload = json.loads(sys.stdin.read() or "{}")
    rid = current_run_id()
    if rid and payload.get("transcript_path"):
        d = RUNS_DIR / rid
        run = read_json(d / "run.json", {})
        if run.get("session_transcript") != payload["transcript_path"]:
            run["session_transcript"] = payload["transcript_path"]
            save_run(d, run)
    path = None
    for _ in range(6):  # transcripts are written asynchronously; give the last lines a moment
        path = find_agent_transcript(payload)
        if path:
            break
        time.sleep(1)
    time.sleep(1.5)
    aid = payload.get("agent_id") or "unknown"
    stage = (payload.get("agent_type") or "unknown").split(":")[-1]
    if not path:
        log_event({"kind": "agent", "run_id": rid, "stage": stage, "agent_id": aid,
                   "error": "transcript not found"})
        return
    seen_file = SEEN_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', aid)}.json"
    seen = set(read_json(seen_file, []))
    summary = summarize_entries(list(iter_transcript(path)), cfg, seen)
    new_ids = summary.pop("_new_ids")
    if not new_ids:
        return  # resumed agent with nothing new, or duplicate hook fire
    write_json(seen_file, sorted(seen))
    log_event({"kind": "agent", "run_id": rid, "stage": stage, "agent_id": aid, **summary})


GIT_GLOBAL_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
ALWAYS_BLOCKED = {
    "pull": "The harness fetches origin itself at branch time; pulling mid-run would move the code under the agents.",
    "rebase": "Rewriting history is not allowed.",
    "filter-branch": "Rewriting history is not allowed.",
    "clean": "git clean deletes untracked files.",
    "remote": None,  # only mutating subcommands, checked below
}
UNCOMMITTED_BLOCKED = {
    "commit": "Git mode is 'uncommitted': leave changes in the working tree for review.",
    "add": "Git mode is 'uncommitted': don't stage changes.",
    "stash": "Stashing hides work from review.",
    "merge": "Merging is not part of the pipeline.",
    "cherry-pick": "Not part of the pipeline.",
    "am": "Not part of the pipeline.",
    "revert": "Not part of the pipeline.",
}


def split_segments(command: str) -> list[list[str]]:
    segs = []
    for part in re.split(r"&&|\|\||;|\||\n|\$\(|`|\(|\)", command):
        part = part.strip()
        if not part:
            continue
        try:
            toks = shlex.split(part, posix=True)
        except ValueError:
            toks = part.split()
        while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in ("sudo", "command", "exec", "env", "time", "nohup")):
            toks = toks[1:]
        if toks:
            segs.append(toks)
    return segs


def git_parts(toks: list[str]) -> tuple[str | None, list[str], str | None]:
    """Return (subcommand, args, -C dir) for a git invocation."""
    if not toks or Path(toks[0]).name not in ("git", "git.exe"):
        return None, [], None
    i, cdir = 1, None
    while i < len(toks) and toks[i].startswith("-"):
        opt = toks[i]
        if opt in GIT_GLOBAL_OPTS_WITH_ARG and i + 1 < len(toks):
            if opt == "-C":
                cdir = toks[i + 1]
            i += 2
            continue
        i += 1
    if i >= len(toks):
        return None, [], cdir
    return toks[i], toks[i + 1:], cdir


def current_branch(path: str | None) -> str | None:
    if not path:
        return None
    r = run_cmd(["git", "-C", path, "branch", "--show-current"])
    return r.stdout.strip() if r.returncode == 0 else None


# gh subcommands that need an account from [github].write_accounts instead of being blocked outright.
GH_ACCOUNT_GATED = {("pr", "create"), ("pr", "merge"), ("issue", "create")}


def github_create_denial(command: str, cfg: dict, what: str) -> str | None:
    """`git push`, `gh pr create|merge` and `gh issue create` are allowed only as an account listed in
    [github].write_accounts (default: personal only). Fails closed: unknown identity, token overrides and a
    project pinned to another account all deny."""
    allowed = {u.lower() for u in ((cfg.get("github") or {}).get("write_accounts") or [])}
    if not allowed:
        return f"`{what}` is blocked: no GitHub account is allowed to push, create/merge PRs or create issues ([github].write_accounts)."
    if re.search(r"\b(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GH_HOST|GH_CONFIG_DIR)\b", command):
        return f"`{what}` is blocked: don't override the GitHub token or host; it must run as your active gh account."
    pinned = ((cfg.get("accounts") or {}).get("github") or "").strip()
    if pinned and pinned.lower() not in allowed:
        return (f"`{what}` is blocked: this project uses GitHub account '{pinned}', which isn't allowed to "
                f"push, create or merge PRs or create issues. Do it yourself.")
    try:
        r = subprocess.run([GH, "api", "user", "--jq", ".login"], capture_output=True, text=True,
                           timeout=10, env=NET_ENV)
        active = r.stdout.strip() if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        active = ""
    if not active:
        return f"`{what}` is blocked: couldn't confirm which GitHub account gh is using."
    if active.lower() not in allowed:
        return (f"`{what}` is blocked: gh is logged in as '{active}', which isn't allowed to push, create or merge PRs "
                f"or create issues. Only: {', '.join(sorted(allowed))}.")
    return None


def push_denial(command: str, args: list[str], cfg: dict, target: str | None) -> str | None:
    """`git push` runs only as a [github].write_accounts account, and never as a force push, a branch/tag
    deletion, a mirror push or a push to a protected branch."""
    protected = set((cfg.get("git") or {}).get("protected_branches", []))
    positional = [a for a in args if not a.startswith("-")]
    for a in args:
        if a in ("--force", "--mirror", "--delete", "--prune") or a.startswith("--force-with-lease") or \
                a == "--force-if-includes" or (not a.startswith("--") and a.startswith("-") and set(a[1:]) & {"f", "d"}):
            return f"`git push {a}` is blocked: force pushes, deletions and mirror pushes aren't allowed."
    refspecs = positional[1:]  # first positional is the remote
    if not refspecs:
        br = current_branch(target)
        refspecs = [br] if br else []
    for ref in refspecs:
        if ref.startswith("+"):
            return "Force pushes (`+refspec`) are blocked."
        if ref.startswith(":"):
            return "Deleting remote branches via `git push` is blocked."
        dst = ref.split(":", 1)[-1].removeprefix("refs/heads/")
        if dst in protected or dst == "HEAD" and current_branch(target) in protected:
            return f"Refusing to push to protected branch '{dst}'."
    return github_create_denial(command, cfg, "git push")


def check_bash(command: str, cfg: dict, cwd: str | None = None, subagent: bool = False) -> str | None:
    """Return a denial reason, or None when the command is allowed."""
    gcfg = cfg.get("git") or {}
    mode = gcfg.get("mode", "uncommitted")
    protected = set(gcfg.get("protected_branches", []))
    workdir = cwd
    for toks in split_segments(command):
        if toks[0] == "cd" and len(toks) > 1:
            workdir = toks[1] if os.path.isabs(toks[1]) else os.path.join(workdir or ".", toks[1])
            continue
        name = Path(toks[0]).name
        if name == "gh" and len(toks) >= 3 and toks[1] == "auth" and toks[2] == "switch":
            return "`gh auth switch` is blocked: the GitHub identity can't be changed from inside a session."
        if name == "gh" and len(toks) >= 3 and (toks[1], toks[2]) in GH_ACCOUNT_GATED:
            denial = github_create_denial(command, cfg, f"gh {toks[1]} {toks[2]}")
            if denial:
                return denial
            continue
        if name == "gh" and len(toks) >= 3 and toks[1] == "pr" and toks[2] in (
                "close", "edit", "ready", "review", "comment", "reopen", "lock"):
            return (f"`gh pr {toks[2]}` is blocked: you manage PRs yourself. Pending reviews are created only "
                    f"with `harness pr post` after the user confirms.")
        if name == "gh" and len(toks) >= 3 and toks[1] == "issue" and toks[2] in (
                "close", "comment", "edit", "delete", "reopen", "lock", "transfer", "pin"):
            return f"`gh issue {toks[2]}` is blocked: the harness only reads issues."
        if name == "gh" and len(toks) >= 2 and toks[1] == "api":
            rest = toks[2:]
            method = next((rest[i + 1] for i, t in enumerate(rest[:-1]) if t in ("-X", "--method")), None)
            method = method or next((t.split("=", 1)[1] for t in rest if t.startswith("--method=")), None)
            writes = any(t in ("-f", "-F", "--field", "--raw-field", "--input") or t.startswith(("--field=", "--raw-field="))
                         for t in rest)
            if (method and method.upper() != "GET") or (writes and not method):
                return "`gh api` write requests are blocked; the harness only reads from GitHub."
        if name == "gh" and len(toks) >= 3 and toks[1] == "repo" and toks[2] in ("delete", "edit", "rename"):
            return "Changing GitHub repos is blocked."
        sub, args, cdir = git_parts(toks)
        if not sub:
            continue
        target = cdir if cdir and os.path.isabs(cdir) else (os.path.join(workdir or ".", cdir) if cdir else workdir)
        if sub == "remote" and args and args[0] in ("add", "remove", "rm", "rename", "set-url", "set-head"):
            return "Changing git remotes is blocked."
        if sub == "push":
            if subagent:
                return "`git push` is blocked for subagents. Pushing is done by the main session or by you."
            denial = push_denial(command, args, cfg, target)
            if denial:
                return denial
            continue
        if sub in ALWAYS_BLOCKED and ALWAYS_BLOCKED[sub]:
            return f"`git {sub}` is blocked by the harness. {ALWAYS_BLOCKED[sub]}"
        if sub == "reset" and ("--hard" in args or "--merge" in args or "--keep" in args):
            return "`git reset --hard` discards work and is blocked."
        if sub == "restore" and "--staged" not in args:
            return "`git restore` discards working-tree changes and is blocked."
        if sub in ("checkout", "switch"):
            creating = any(x in args for x in ("-b", "-B", "-c", "-C", "--create", "--orphan"))
            if "--" in args or "." in args:
                return "`git checkout -- <path>` discards changes and is blocked."
            if not creating:
                return (f"`git {sub}` between branches is blocked inside the pipeline "
                        "(branches are managed by `harness branch`).")
        if sub == "branch" and any(x in args for x in ("-D", "-d", "--delete", "-m", "-M", "--force", "-f")):
            return "Deleting, renaming or forcing branches is blocked."
        if sub == "tag" and args and not args[0].startswith("-l") and args[0] not in ("--list",):
            return "Creating or deleting tags is blocked."
        if sub == "commit" and ("--amend" in args):
            return "`git commit --amend` rewrites history and is blocked."
        if mode != "commit" and sub in UNCOMMITTED_BLOCKED:
            return f"`git {sub}` is blocked. {UNCOMMITTED_BLOCKED[sub]} (Change git.mode in `.claude/harness.toml` to allow commits.)"
        if mode == "commit":
            if sub in ("stash", "merge", "cherry-pick", "am", "revert"):
                return f"`git {sub}` is not part of the pipeline."
            if sub in ("commit", "add"):
                br = current_branch(target)
                if br and br in protected:
                    return f"Refusing `git {sub}` on protected branch '{br}'. Run `harness branch` first."
    return None


def check_github_global(command: str, cfg: dict, cwd: str | None = None) -> str | None:
    """Hard stop for every project (also ones without a harness config): creating or merging PRs and
    creating issues, via gh or `gh api`, only works as an account in [github].write_accounts."""
    workdir = cwd
    for toks in split_segments(command):
        if toks[0] == "cd" and len(toks) > 1:
            workdir = toks[1] if os.path.isabs(toks[1]) else os.path.join(workdir or ".", toks[1])
            continue
        sub, args, cdir = git_parts(toks)
        if sub == "push":
            target = cdir if cdir and os.path.isabs(cdir) else (os.path.join(workdir or ".", cdir) if cdir else workdir)
            denial = push_denial(command, args, cfg, target)
            if denial:
                return denial
        if Path(toks[0]).name != "gh" or len(toks) < 3:
            continue
        if toks[1] == "auth" and toks[2] == "switch":
            return "`gh auth switch` is blocked: the GitHub identity can't be changed from inside a session."
        if (toks[1], toks[2]) in GH_ACCOUNT_GATED:
            denial = github_create_denial(command, cfg, f"gh {toks[1]} {toks[2]}")
            if denial:
                return denial
        if toks[1] == "api":
            rest = toks[2:]
            method = next((rest[i + 1] for i, t in enumerate(rest[:-1]) if t in ("-X", "--method")), None)
            method = method or next((t.split("=", 1)[1] for t in rest if t.startswith("--method=")), None)
            writes = any(t in ("-f", "-F", "--field", "--raw-field", "--input") or t.startswith(("--field=", "--raw-field="))
                         for t in rest)
            if ((method and method.upper() != "GET") or (writes and not method)) and \
                    any(re.search(r"graphql|/(issues|pulls)\b", t) for t in rest):
                denial = github_create_denial(command, cfg, "gh api (issue/PR write)")
                if denial:
                    return denial
    return None


def hook_guard_github(cfg):
    payload = json.loads(sys.stdin.read() or "{}")
    cmd = (payload.get("tool_input") or {}).get("command", "")
    reason = check_github_global(cmd, cfg, payload.get("cwd"))
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": f"[harness github guard] {reason}"}}))


def hook_guard_bash(cfg):
    payload = json.loads(sys.stdin.read() or "{}")
    cmd = (payload.get("tool_input") or {}).get("command", "")
    reason = check_bash(cmd, cfg, payload.get("cwd"), subagent=bool(payload.get("agent_type")))
    if reason:
        log_event({"kind": "guard_block", "run_id": current_run_id(),
                   "stage": (payload.get("agent_type") or "manager").split(":")[-1], "command": cmd[:300], "reason": reason})
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": f"[harness guard] {reason}"}}))


def account_dir(cfg: dict, name: str | None) -> str | None:
    """Config dir for a named Claude account ('' / None = the default ~/.claude)."""
    if not name:
        return None
    dirs = (cfg.get("accounts") or {}).get("dirs") or {}
    d = dirs.get(name)
    return str(Path(d).expanduser()) if d else None


def hook_session_start(cfg):
    sys.stdin.read()
    want = (cfg.get("accounts") or {}).get("claude")
    if not want:
        return
    want_dir = account_dir(cfg, want)
    have = os.environ.get("CLAUDE_CONFIG_DIR")
    have_dir = str(Path(have).expanduser().resolve()) if have else None
    norm_want = str(Path(want_dir).resolve()) if want_dir else None
    if have_dir != norm_want:
        names = {str(Path(v).expanduser().resolve()) if v else None: k
                 for k, v in ((cfg.get("accounts") or {}).get("dirs") or {}).items()}
        current = names.get(have_dir, have_dir or "default (~/.claude)")
        print(json.dumps({"systemMessage":
            f"[harness] This project is set to use your '{want}' Claude account, but this session is on "
            f"'{current}'. Quit and start `claude` again from this folder (the harness shell function picks "
            f"the account automatically), or run `claude-{want}`."}))


def cmd_hook(a, cfg):
    if a.event == "guard-github":  # user-level hook: applies in every project, configured or not
        hook_guard_github(cfg)
        return
    if not CONFIGURED:
        if a.event != "session-start":
            sys.stdin.read()
        return  # not a harness project: hooks do nothing
    try:
        {"subagent-stop": hook_subagent_stop, "guard-bash": hook_guard_bash,
         "session-start": hook_session_start}[a.event](cfg)
    except Exception as exc:  # a broken metrics hook must never break the session
        try:
            log_event({"kind": "hook_error", "hook": a.event, "error": repr(exc)[:500]})
        except Exception:
            pass
        if a.event == "guard-bash":
            raise


# --------------------------------------------------------------------------- branches
DEP_FILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "package.json", "requirements.txt",
             "test_requirements.txt", "requirements-dev.txt", "Pipfile.lock", "poetry.lock")


def base_branch_for(cfg: dict, rname: str) -> str:
    return repos(cfg)[rname].get("base_branch") or (cfg.get("git") or {}).get("base_branch", "dev")


def branch_name(cfg: dict, run: dict, btype: str | None, slug: str | None) -> str:
    gcfg = cfg.get("git") or {}
    issue = run.get("issue") or {}
    btype = btype or issue.get("branch_type") or "feat"
    slug = slugify(slug or issue.get("title") or run["task"], 45)
    if issue.get("number"):
        return gcfg.get("branch_template", "{type}/{issue}-{slug}").format(type=btype, issue=issue["number"], slug=slug)
    return gcfg.get("branch_template_no_issue", "{type}/{slug}").format(type=btype, slug=slug)


def cmd_branch(a, cfg):
    """Fetch origin and create the work branch from a fresh origin/<base>. Never touches the user's
    other branches; refuses to carry over uncommitted tracked changes unless --allow-dirty."""
    d, run = load_run()
    name = a.full_name or branch_name(cfg, run, a.type, a.name)
    protected = set((cfg.get("git") or {}).get("protected_branches", []))
    if name in protected:
        sys.exit(f"Refusing to use protected branch name {name}")
    rs = repos(cfg)
    problems = 0
    for rname in [r.strip() for r in a.repos.split(",") if r.strip()]:
        if rname not in rs:
            print(f"{rname}: unknown repo (known: {', '.join(rs)})")
            problems += 1
            continue
        path = rs[rname]["abs"]
        if not is_git_repo(path):
            print(f"{rname}: {path} is not a git repo")
            problems += 1
            continue
        base = base_branch_for(cfg, rname)
        cur = git(path, "branch", "--show-current").strip()
        dirty = git(path, "status", "--porcelain", "--untracked-files=no").strip()
        info = run["repos"].get(rname) or {}
        if cur != name and dirty and not a.allow_dirty:
            print(f"{rname}: has uncommitted changes to tracked files on '{cur}':\n{dirty}\n"
                  f"Ask the user to commit or stash them, or rerun with --allow-dirty to carry them over.")
            problems += 1
            continue
        has_origin = run_cmd(["git", "-C", str(path), "remote", "get-url", "origin"]).returncode == 0
        if has_origin:
            ok, msg = git_net(path, "fetch", "origin", base)
            remote_ref = f"origin/{base}"
        else:
            ok, msg, remote_ref = False, "no origin remote", base
        have_remote = run_cmd(["git", "-C", str(path), "rev-parse", "--verify", "--quiet", remote_ref]).returncode == 0
        if not has_origin and have_remote:
            print(f"{rname}: no origin remote; branching from local {base}")
        elif ok:
            print(f"{rname}: fetched origin/{base}")
        elif have_remote:
            when = git(path, "log", "-1", "--format=%cr", remote_ref).strip()
            print(f"{rname}: WARNING could not fetch origin ({msg.splitlines()[-1] if msg else 'error'}). "
                  f"Using the last-fetched {remote_ref} (latest commit {when}).")
        else:
            print(f"{rname}: could not fetch origin and there is no {remote_ref}: {msg}")
            problems += 1
            continue
        prev_head = git(path, "rev-parse", "HEAD").strip()
        if cur == name:
            print(f"{rname}: already on {name} (not resetting it)")
        else:
            exists = run_cmd(["git", "-C", str(path), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"]).returncode == 0
            r = run_cmd(["git", "-C", str(path), "switch", name] if exists else
                        ["git", "-C", str(path), "switch", "--no-track", "-c", name, remote_ref])
            if r.returncode != 0:
                print(f"{rname}: could not switch to {name}: {r.stderr.strip()}")
                problems += 1
                continue
            print(f"{rname}: {'switched to existing branch' if exists else 'created branch'} {name}"
                  f"{'' if exists else ' from ' + remote_ref} (you were on {cur or 'a detached HEAD'})")
            info.setdefault("previous_branch", cur)
        head = git(path, "rev-parse", "HEAD").strip()
        behind = run_cmd(["git", "-C", str(path), "rev-list", "--count", f"HEAD..{remote_ref}"]).stdout.strip()
        if behind and behind != "0":
            print(f"{rname}: note: this branch is {behind} commit(s) behind {remote_ref}")
        changed_deps = [f for f in git(path, "diff", "--name-only", prev_head, head).splitlines()
                        if Path(f).name in DEP_FILES]
        if changed_deps:
            print(f"{rname}: DEPENDENCIES CHANGED vs your previous checkout: {', '.join(changed_deps)}. "
                  f"Ask the user to reinstall (npm install / pip install -r ...) before running baselines or gates.")
            info["dependency_changes"] = changed_deps
        if "preexisting_untracked" not in info:
            info["preexisting_untracked"] = [u for u in git(path, "ls-files", "--others", "--exclude-standard").splitlines() if u]
        info["base_branch"] = base
        info["base_commit"] = git(path, "merge-base", "HEAD", remote_ref).strip()
        info["branch"] = git(path, "branch", "--show-current").strip()
        run["repos"][rname] = info
    save_run(d, run)
    sys.exit(3 if problems else 0)


# --------------------------------------------------------------------------- gates
def python_for(repo: Path) -> str:
    for cand in (".venv/bin/python", "venv/bin/python", "env/bin/python",
                 ".venv/Scripts/python.exe", "venv/Scripts/python.exe"):
        if (repo / cand).exists():
            return str(repo / cand)
    return "python3" if shutil.which("python3") else "python"


def js_runner(pkg_dir: Path, repo: Path) -> str:
    for d in (pkg_dir, repo):
        if (d / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (d / "yarn.lock").exists():
            return "yarn"
    return "npm"


SKIP_DIRS = {"node_modules", ".git", ".next", "dist", "build", ".expo", "ios", "android", ".venv", "venv", "env", "__pycache__"}


def package_jsons(repo: Path, max_depth: int = 3) -> list:
    """package.json files up to max_depth folders down, skipping dependency/build folders."""
    out = []
    def walk(d: Path, depth: int):
        pj = d / "package.json"
        if pj.is_file():
            out.append(pj)
        if depth >= max_depth:
            return
        try:
            subs = sorted(x for x in d.iterdir() if x.is_dir() and x.name not in SKIP_DIRS and not x.name.startswith("."))
        except OSError:
            return
        for x in subs:
            walk(x, depth + 1)
    walk(repo, 0)
    return out


def detect_gates(repo: Path, cfg: dict) -> list[dict]:
    gates = []
    lint = (cfg.get("gates") or {}).get("autodetect_lint", False)
    if (repo / "manage.py").exists():
        py = python_for(repo)
        pyproject = (repo / "pyproject.toml").read_text(errors="ignore") if (repo / "pyproject.toml").exists() else ""
        if (repo / "pytest.ini").exists() or (repo / "conftest.py").exists() or "[tool.pytest" in pyproject:
            gates.append({"name": "tests", "cmd": f"{shlex.quote(py)} -m pytest -q", "kind": "pytest"})
        else:
            gates.append({"name": "tests", "cmd": f"{shlex.quote(py)} manage.py test --noinput", "kind": "django"})
        if lint and ((repo / "ruff.toml").exists() or "[tool.ruff" in pyproject):
            gates.append({"name": "lint", "cmd": f"{shlex.quote(py)} -m ruff check ."})
    pkgs = []
    for pj in package_jsons(repo):
        pkgs.append(pj)
    for pj in pkgs:
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        scripts = data.get("scripts") or {}
        rel = pj.parent.relative_to(repo).as_posix()
        label = "" if rel == "." else rel.replace("/", "-") + "-"
        runner = js_runner(pj.parent, repo)
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if (pj.parent / "tsconfig.json").exists() and "typescript" in deps:
            gates.append({"name": f"{label}typecheck", "cmd": "npx tsc --noEmit --pretty false", "cwd": rel, "kind": "tsc"})
        elif "typecheck" in scripts:
            gates.append({"name": f"{label}typecheck", "cmd": f"{runner} run typecheck", "cwd": rel})
        test_script = scripts.get("test", "")
        if test_script and "no test specified" not in test_script:
            if "jest" in test_script or "jest" in deps or "jest-expo" in deps:
                gates.append({"name": f"{label}tests", "cmd": "npx jest --ci --silent", "cwd": rel, "kind": "jest"})
            elif "vitest" in test_script or "vitest" in deps:
                gates.append({"name": f"{label}tests", "cmd": "npx vitest run", "cwd": rel})
            else:
                gates.append({"name": f"{label}tests", "cmd": f"{runner} test --silent", "cwd": rel})
        if lint and "lint" in scripts:
            gates.append({"name": f"{label}lint", "cmd": f"{runner} run lint", "cwd": rel})
    if not gates and ((repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists() or (repo / "tests").is_dir()) \
            and any(repo.glob("**/test_*.py")):
        gates.append({"name": "tests", "cmd": f"{shlex.quote(python_for(repo))} -m pytest -q", "kind": "pytest"})
    return gates


def gates_for(rname: str, rcfg: dict, cfg: dict) -> tuple[list[dict], bool]:
    if rcfg.get("gates"):
        return list(rcfg["gates"]), False
    return detect_gates(rcfg["abs"], cfg), True


def run_untracked(path: Path, rname: str, run: dict) -> list[str]:
    """Untracked files created during the run (excludes ones the user already had lying around)."""
    pre = set((run.get("repos", {}).get(rname) or {}).get("preexisting_untracked", []))
    return [u for u in git(path, "ls-files", "--others", "--exclude-standard").splitlines() if u and u not in pre]


def changed_repos(cfg: dict, run: dict) -> list[str]:
    out = []
    for name, r in repos(cfg).items():
        if not is_git_repo(r["abs"]):
            continue
        base = (run.get("repos", {}).get(name) or {}).get("base_commit")
        tracked = git(r["abs"], "status", "--porcelain", "--untracked-files=no").strip()
        moved = base and git(r["abs"], "rev-parse", "HEAD").strip() != base
        if tracked or moved or run_untracked(r["abs"], name, run):
            out.append(name)
    return out


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def failure_signatures(kind: str, output: str) -> list[str]:
    """Stable identifiers for each failure in a gate's output (no line numbers, so edits elsewhere
    in a file don't make old failures look new)."""
    sigs = set()
    cur_file = None
    for raw in ANSI_RE.sub("", output).splitlines():
        line = raw.rstrip()
        if kind == "jest":
            m = re.match(r"^FAIL (\S+)", line)
            if m:
                sigs.add(f"suite {m.group(1)}")
            m = re.match(r"^\s*\u25cf (?!Console\b|Test suite failed to run\b)(.+?)$", line)
            if m:
                sigs.add(f"test {m.group(1).strip()}")
        elif kind == "tsc":
            m = re.match(r"^(\S+?)\(\d+,\d+\): (error TS\d+: .+)$", line)
            if m:
                sigs.add(f"{m.group(1)}: {m.group(2)}")
        elif kind == "pytest":
            m = re.match(r"^(FAILED|ERROR) (\S+)", line)
            if m:
                sigs.add(f"{m.group(1)} {m.group(2)}")
        elif kind == "django":
            m = re.match(r"^(FAIL|ERROR): (\S+) \((\S+)\)", line)
            if m:
                where = m.group(3)  # Python 3.11+ already ends with ".<test name>"
                sigs.add(f"{m.group(1)} {where if where.endswith('.' + m.group(2)) else where + '.' + m.group(2)}")
        elif kind == "eslint":
            # Handles ESLint's default "stylish" output and `next lint` output.
            m = re.match(r"^\s*\d+:\d+\s+(?:error|Error:)\s+(.+?)\s{2,}(\S+)\s*$", line)
            if m:
                sigs.add(f"{cur_file}: {m.group(2)}: {m.group(1)}")
            elif re.match(r"^(\.{0,2}/|[A-Za-z]:\\)\S+$", line.strip()) and not line[:1].isspace():
                cur_file = line.strip()
    return sorted(sigs)


def baseline_path(d: Path, rname: str, gname: str) -> Path:
    return d / "baseline" / f"{rname}-{gname}.json"


def planned_files(d: Path, rname: str) -> list[str]:
    plan = read_json(d / "tasks.json", {}) or {}
    files = []
    for t in plan.get("tasks", []):
        if t.get("repo") == rname:
            files += [f for f in t.get("files", []) if isinstance(f, str)]
    return files


def cmd_baseline(a, cfg):
    """Run baseline-aware gates on the untouched code and record their failures."""
    d, run = load_run()
    rs = repos(cfg)
    env = {**os.environ, "CI": "true", "FORCE_COLOR": "0"}
    gcfg = cfg.get("gates") or {}
    for rname in [r.strip() for r in a.repos.split(",") if r.strip()]:
        if rname not in rs:
            print(f"[{rname}] unknown repo")
            continue
        gates, _ = gates_for(rname, rs[rname], cfg)
        files = planned_files(d, rname)
        repo_prefix = rs[rname].get("path", rname).rstrip("/") + "/"
        files = [f[len(repo_prefix):] if f.startswith(repo_prefix) else f for f in files]
        for g in gates:
            if not g.get("kind"):
                continue
            cwd = rs[rname]["abs"] / g.get("cwd", ".")
            cmd = expand_gate_cmd(g["cmd"], files, rs[rname]["abs"], cwd, require_changes=False)
            if cmd is None:
                print(f"[{rname}] {g['name']}: no baseline needed (plan doesn't touch it)")
                continue
            t0 = time.time()
            try:
                r = run_cmd(cmd, cwd=cwd, timeout=g.get("timeout_seconds", gcfg.get("timeout_seconds", 900)), env=env, shell=True)
                code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
            except subprocess.TimeoutExpired as exc:
                code, out = -1, f"TIMEOUT after {exc.timeout}s"
            sigs = failure_signatures(g["kind"], out)
            write_json(baseline_path(d, rname, g["name"]), {
                "repo": rname, "gate": g["name"], "cmd": cmd, "exit": code, "signatures": sigs,
                "commit": git(rs[rname]["abs"], "rev-parse", "HEAD").strip(), "recorded_at": iso()})
            (d / "baseline" / f"{rname}-{g['name']}.log").write_text(f"$ (cd {cwd} && {cmd})\n\n{out}", encoding="utf-8")
            state = "clean" if code == 0 else (f"{len(sigs)} pre-existing failures" if sigs else f"exit {code}, no failures parsed")
            print(f"[{rname}] {g['name']}: baseline {state} ({round(time.time() - t0)}s)")
            log_event({"kind": "baseline", "run_id": run["id"], "repo": rname, "gate": g["name"],
                       "exit": code, "failures": len(sigs)})


def changed_files(rname: str, rcfg: dict, run: dict) -> list[str]:
    """Paths (relative to the repo) changed since the run's base commit, incl. untracked files."""
    path = rcfg["abs"]
    base = (run.get("repos", {}).get(rname) or {}).get("base_commit") or "HEAD"
    files = set(git(path, "diff", "--name-only", base).splitlines())
    files |= set(run_untracked(path, rname, run))
    return sorted(f for f in files if f)


def expand_gate_cmd(cmd: str, files: list, repo: Path, cwd: Path, require_changes: bool = True):
    """Fill {changed_files} / {changed_django_apps} from `files` (repo-relative paths).
    Returns None when the gate has nothing to check. With require_changes=False (baselines),
    an empty file list means "unknown scope": unscoped gates still run."""
    try:
        rel = cwd.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        rel = "."
    prefix = "" if rel == "." else rel + "/"
    under = [f[len(prefix):] for f in files if f.startswith(prefix)]
    if prefix and not under and (require_changes or files):
        return None  # gate scoped to a subfolder that isn't touched
    if "{changed_files}" in cmd:
        existing = [f for f in under if (cwd / f).exists()]
        if not existing:
            return None
        cmd = cmd.replace("{changed_files}", " ".join(shlex.quote(f) for f in existing))
    if "{changed_django_apps}" in cmd:
        apps = sorted({f.split("/")[0] for f in under if "/" in f
                       and ((cwd / f.split("/")[0] / "apps.py").exists() or (cwd / f.split("/")[0] / "models.py").exists())})
        if not apps:
            return None
        cmd = cmd.replace("{changed_django_apps}", " ".join(apps))
    return cmd


def cmd_gates(a, cfg):
    d, run = load_run()
    gcfg = cfg.get("gates") or {}
    rs = repos(cfg)
    targets = a.repo or changed_repos(cfg, run)
    if not targets:
        print("No changed repos; nothing to check.")
        return
    tail_n = gcfg.get("failure_tail_lines", 60)
    all_ok = True
    logs = d / "gates"
    logs.mkdir(exist_ok=True)
    attempt = max([int(p.name[:3]) for p in logs.glob("*.log") if p.name[:3].isdigit()] or [0]) + 1
    env = {**os.environ, "CI": "true", "FORCE_COLOR": "0"}
    for rname in targets:
        if rname not in rs:
            print(f"[{rname}] unknown repo")
            all_ok = False
            continue
        gates, auto = gates_for(rname, rs[rname], cfg)
        if not gates:
            print(f"[{rname}] no gates configured or detected; add some to `.claude/harness.toml`")
            continue
        files = changed_files(rname, rs[rname], run)
        for g in gates:
            cwd = rs[rname]["abs"] / g.get("cwd", ".")
            cmd = expand_gate_cmd(g["cmd"], files, rs[rname]["abs"], cwd)
            if cmd is None:
                print(f"[{rname}] {g['name']}: SKIP (no relevant changed files)")
                continue
            t0 = time.time()
            try:
                r = run_cmd(cmd, cwd=cwd, timeout=g.get("timeout_seconds", gcfg.get("timeout_seconds", 900)), env=env, shell=True)
                ok, out = r.returncode == 0, (r.stdout or "") + (r.stderr or "")
            except subprocess.TimeoutExpired as exc:
                ok, out = False, f"TIMEOUT after {exc.timeout}s\n{exc.stdout or ''}"
            secs = round(time.time() - t0, 1)
            log = logs / f"{attempt:03d}-{rname}-{g['name']}.log"
            log.write_text(f"$ (cd {cwd} && {cmd})\n\n{out}", encoding="utf-8")
            note, new_sigs, pre = "", [], 0
            if not ok and g.get("kind"):
                base = read_json(baseline_path(d, rname, g["name"]))
                sigs = failure_signatures(g["kind"], out)
                if base is None:
                    note = "  (no baseline recorded; run `harness baseline` before editing to ignore pre-existing failures)"
                elif sigs:
                    known = set(base.get("signatures", []))
                    new_sigs = [x for x in sigs if x not in known]
                    pre = len(sigs) - len(new_sigs)
                    if not new_sigs:
                        ok = True
                        note = f"  ({pre} pre-existing failure(s) ignored; no new failures)"
                    else:
                        note = f"  ({len(new_sigs)} NEW failure(s); {pre} pre-existing ignored)"
                else:
                    note = "  (failed without recognisable failure lines, e.g. a crash or config error)"
            print(f"[{rname}] {g['name']}: {'PASS' if ok else 'FAIL'} ({secs}s){note}{'  [auto-detected]' if auto else ''}")
            if not ok:
                all_ok = False
                if new_sigs:
                    print("New failures (not present before your changes):")
                    for x in new_sigs[:40]:
                        print(f"  - {x}")
                    print(f"(full log: {log})")
                else:
                    tail = "\n".join(out.strip().splitlines()[-tail_n:])
                    print(f"--- last {tail_n} lines (full log: {log}) ---\n{tail}\n---")
            log_event({"kind": "gate", "run_id": run["id"], "stage": "gates", "repo": rname,
                       "gate": g["name"], "passed": ok, "duration_s": secs, "task": a.task,
                       "attempt": a.attempt, "new_failures": len(new_sigs), "preexisting_failures": pre})
    print("GATES: PASS" if all_ok else "GATES: FAIL")
    sys.exit(0 if all_ok else 1)


# --------------------------------------------------------------------------- diffs
def repo_diff(rname: str, rcfg: dict, run: dict, context: int = 12) -> tuple[str, str]:
    path = rcfg["abs"]
    base = (run.get("repos", {}).get(rname) or {}).get("base_commit") or "HEAD"
    diff = git(path, "diff", f"-U{context}", "--no-color", base)
    stat = git(path, "diff", "--stat", "--no-color", base)
    for u in run_untracked(path, rname, run):
        f = path / u
        try:
            if f.stat().st_size > 200_000:
                diff += f"\n--- /dev/null\n+++ b/{u}\n(new file, {f.stat().st_size} bytes, too large to include)\n"
                continue
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            diff += f"\n--- /dev/null\n+++ b/{u}\n(new binary file)\n"
            continue
        lines = text.splitlines()
        diff += f"\ndiff --git a/{u} b/{u}\nnew file\n--- /dev/null\n+++ b/{u}\n@@ -0,0 +1,{len(lines)} @@\n"
        diff += "\n".join("+" + ln for ln in lines) + "\n"
        stat += f" {u} | {len(lines)} + (new, untracked)\n"
    return diff, stat


def cmd_diff(a, cfg):
    _, run = load_run()
    rs = repos(cfg)
    if run.get("mode") == "review":
        for pr in run.get("prs", []):
            print(f"=== {pr['repo']}#{pr['number']} {pr['title']} ({pr['head_branch']} -> {pr['base']}; "
                  f"read files with: git -C {pr['path']} show {pr['ref']}:<path>) ===")
            print(pr.get("stat", "") if a.stat else pr_diff(pr))
        return
    for rname in changed_repos(cfg, run):
        diff, stat = repo_diff(rname, rs[rname], run)
        print(f"=== {rname} ({rs[rname]['path']}) ===")
        print(stat if a.stat else diff)


# --------------------------------------------------------------------------- review
REVIEW_SYSTEM = """You are a senior staff engineer doing code review for a small product team.
You are reviewing changes written by an AI coding agent from a different lab; be skeptical and concrete.

{project_context}

Look for, in priority order:
1. Bugs: wrong logic, unhandled errors or null/undefined, race conditions, off-by-one, broken edge cases.
2. Security: missing authentication/authorization on endpoints, object-level permission gaps, data
   exposure in API responses, injection, secrets in code.
3. Contracts between parts of the system: API fields, URLs, status codes or payload shapes that a client,
   another repo or another module consumes differently than the code now produces; changed props or
   exports without updated callers.
4. Data: schema changes without a migration, unsafe migrations, N+1 queries, missing indexes.
5. Missing or weak tests for the behaviour that changed. Requirements from the plan that were not implemented.
6. Project-specific checks listed above, and platform pitfalls for the stack.
Do NOT report pure style or formatting (linters handle that) or speculative rewrites.

Severity:
- "blocking": a real bug, security issue, broken contract, data loss risk, or a plan requirement not met.
- "should_fix": likely problem or clear maintainability/test gap worth fixing before merge.
- "nit": minor; optional.

Every finding must point at a specific file and line from the diff and quote the relevant code as evidence.
If you are not confident a finding is real, lower its severity or leave it out. An empty list is a fine answer.

Respond with a single JSON object, no prose outside it:
{"summary": "<2-3 sentences>",
 "findings": [{"severity": "blocking|should_fix|nit", "repo": "<repo name>", "file": "<path>", "line": <int or null>,
   "title": "<short>", "evidence": "<quoted code>", "detail": "<what is wrong and why>",
   "suggestion": "<concrete fix>", "status": "new|unresolved"}]}
"status" is "unresolved" only when the finding repeats an issue from the previous round's list that still is not fixed."""


def build_pr_review_prompt(run: dict, d: Path, cfg: dict) -> tuple[str, bool]:
    rcfg = cfg.get("reviewer") or {}
    rs = repos(cfg)
    parts = [(d / "task.md").read_text(encoding="utf-8"), ""]
    diffs = []
    for pr in run.get("prs", []):
        desc = rs[pr["repo"]].get("description", "")
        diffs.append(f"## Repo: {pr['repo']} ({rs[pr['repo']].get('path')}: {desc}), PR {pr['repo']}#{pr['number']}\n"
                     f"```diff\n{pr_diff(pr)}\n```\n")
    body = "# Changes\n" + "\n".join(diffs)
    limit = rcfg.get("max_diff_chars", 300000)
    truncated = len(body) > limit
    if truncated:
        body = body[:limit] + "\n\n[DIFF TRUNCATED: the remaining changes were not included. Say so in your summary.]\n"
    parts.append(body)
    parts.append(f"Return at most {rcfg.get('max_findings', 15)} findings, most severe first.")
    return "\n".join(parts), truncated


PR_REVIEW_PREAMBLE = """You are a senior staff engineer reviewing a teammate's pull request(s) for a small product team.
When several PRs are listed they are designed to be merged together (e.g. an app PR and the component-library
PR it needs). Review them as one change: check that they agree with each other (component props and exports,
payload fields, endpoints, versions) and that nothing one PR relies on is missing from the others.
Findings will be posted as inline review comments, so "repo" must be the harness repo name from the
"## Repo:" header, "file" a path in that PR, and "line" a line number on the NEW side of the diff.
Write "detail" and "suggestion" the way you'd write a review comment to a colleague: direct, specific, polite.
Also return "questions": [{"repo": "...", "file": "...", "line": <int or null>, "question": "..."}] for things
you'd ask the author (intent, missing context) rather than assert as problems."""


def project_context(cfg: dict) -> str:
    proj = cfg.get("project") or {}
    lines = []
    if proj.get("name") or proj.get("description"):
        lines.append(f"Project: {proj.get('name', '')}\n{proj.get('description', '').strip()}".strip())
    rs = repos(cfg)
    if rs:
        lines.append("Repos:\n" + "\n".join(f"- {n}: {r.get('description', r.get('path', n))}" for n, r in rs.items()))
    focus = proj.get("review_focus") or []
    if focus:
        lines.append("Project-specific things to check:\n" + "\n".join(f"- {f}" for f in focus))
    return "\n\n".join(lines) or "(No project description configured.)"


def review_system_prompt(run: dict, cfg: dict | None = None) -> str:
    base = REVIEW_SYSTEM.replace("{project_context}", project_context(cfg or load_config()))
    if run.get("mode") != "review":
        return base
    rest = base.split("\n\n", 1)[1]  # drop the "AI-written changes" intro
    rest = rest.replace('"status": "new|unresolved"}]}', '"status": "new"}], "questions": [...]}')
    return PR_REVIEW_PREAMBLE + "\n\n" + rest


def build_review_prompt(run: dict, d: Path, cfg: dict, round_n: int) -> tuple[str, bool]:
    if run.get("mode") == "review":
        return build_pr_review_prompt(run, d, cfg)
    rcfg = cfg.get("reviewer") or {}
    rs = repos(cfg)
    parts = [f"# Task\n{run['task']}\n"]
    for name, title in (("brief.md", "Clarified requirements"), ("plan.md", "Implementation plan")):
        if (d / name).exists():
            parts.append(f"# {title}\n{(d / name).read_text(encoding='utf-8')}\n")
    prev = read_json(d / f"verified-{round_n - 1}.json") if round_n > 1 else None
    if prev:
        parts.append("# Findings from the previous round (confirmed by a second reviewer)\n"
                     "Check whether each one is now fixed. Repeat only the ones that are not, with status \"unresolved\".\n"
                     + json.dumps(prev.get("findings", []), indent=1) + "\n")
    diffs = []
    for rname in changed_repos(cfg, run):
        diff, _ = repo_diff(rname, rs[rname], run)
        diffs.append(f"## Repo: {rname} ({rs[rname].get('description', '')})\n```diff\n{diff}\n```\n")
    body = "# Changes\n" + ("\n".join(diffs) if diffs else "(no changes found)")
    limit = rcfg.get("max_diff_chars", 300000)
    truncated = len(body) > limit
    if truncated:
        body = body[:limit] + "\n\n[DIFF TRUNCATED: the remaining changes were not included. Say so in your summary.]\n"
    parts.append(body)
    parts.append(f"Return at most {rcfg.get('max_findings', 15)} findings, most severe first.")
    return "\n".join(parts), truncated


def in_peak(windows: list[str], weekdays_only: bool, t: dt.datetime) -> bool:
    if weekdays_only and t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    for w in windows:
        a, b = w.split("-")
        ah, am = map(int, a.split(":"))
        bh, bm = map(int, b.split(":"))
        if ah * 60 + am <= hm < bh * 60 + bm:
            return True
    return False


def external_cost(usage: dict, p: dict, t: dt.datetime) -> tuple[float, dict]:
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    prompt = usage.get("prompt_tokens", 0) or 0
    if hit is None:
        hit = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        miss = prompt - hit
    out = usage.get("completion_tokens", 0) or 0
    mult = 1.0
    if p.get("peak_windows_utc") and not in_peak(p["peak_windows_utc"], p.get("peak_weekdays_only", True), t):
        mult = p.get("offpeak_multiplier", 1.0)
    cost = (miss * p.get("price_input", 0) + hit * p.get("price_cached_input", p.get("price_input", 0))
            + out * p.get("price_output", 0)) / 1e6 * mult
    reasoning = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0
    return cost, {"gen_ai.usage.input_tokens": miss, "gen_ai.usage.cache_read_tokens": hit,
                  "gen_ai.usage.output_tokens": out, "gen_ai.usage.reasoning_tokens": reasoning,
                  "price_multiplier": mult}


def call_chat(p: dict, system: str, user: str, timeout: int) -> dict:
    key = os.environ.get(p.get("api_key_env", ""), "")
    url = p["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": p["model"], "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}],
            "max_tokens": p.get("max_tokens", 16000), "stream": False}
    if p.get("json_mode", True):
        body["response_format"] = {"type": "json_object"}
    body.update(p.get("extra_body") or {})
    data = json.dumps(body).encode()
    last_err = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            last_err = f"HTTP {exc.code}: {detail}"
            if exc.code not in (408, 429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(last_err or "request failed")


def parse_json_content(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def write_review_md(path: Path, title: str, summary: str, findings: list[dict]) -> None:
    lines = [f"# {title}", "", summary or "", ""]
    for f in findings:
        loc = f"{f.get('repo', '')}:{f.get('file', '')}" + (f":{f['line']}" if f.get("line") else "")
        lines += [f"## {f['id']} [{f.get('severity')}] {f.get('title', '')}", f"`{loc}`", "",
                  f.get("detail", ""), ""]
        if f.get("evidence"):
            lines += ["```", str(f["evidence"])[:1500], "```", ""]
        if f.get("suggestion"):
            lines += [f"**Suggestion:** {f['suggestion']}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def cmd_review(a, cfg):
    d, run = load_run()
    rcfg = cfg.get("reviewer") or {}
    provider = a.provider or rcfg.get("provider", "deepseek")
    p = (cfg.get("providers") or {}).get(provider)
    if provider == "claude" or not p:
        print(f"FALLBACK: provider '{provider}' is not an outside model. Spawn the claude-reviewer subagent "
              f"for round {a.round} instead; it writes {d}/review-{a.round}.json.")
        sys.exit(4)
    if not os.environ.get(p.get("api_key_env", "")):
        print(f"FALLBACK: ${p.get('api_key_env')} is not set, so {provider} can't be called. Spawn the "
              f"claude-reviewer subagent for round {a.round} instead.")
        sys.exit(4)
    prompt, truncated = build_review_prompt(run, d, cfg, a.round)
    (d / f"review-{a.round}.prompt.md").write_text(prompt, encoding="utf-8")
    t0 = time.time()
    try:
        resp = call_chat(p, review_system_prompt(run, cfg), prompt, rcfg.get("request_timeout_seconds", 600))
    except RuntimeError as exc:
        log_event({"kind": "llm_call", "run_id": run["id"], "stage": "reviewer", "provider": provider,
                   "gen_ai.system": provider, "gen_ai.request.model": p["model"], "error": str(exc)[:500]})
        print(f"FALLBACK: {provider} request failed ({exc}). Spawn the claude-reviewer subagent for round {a.round}.")
        sys.exit(4)
    secs = round(time.time() - t0, 1)
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    try:
        result = parse_json_content(msg.get("content", ""))
    except (json.JSONDecodeError, TypeError):
        (d / f"review-{a.round}.raw.txt").write_text(msg.get("content", "") or "", encoding="utf-8")
        result = {"summary": "Reviewer returned unparseable output; see raw file.", "findings": [], "parse_error": True}
    findings = []
    for i, f in enumerate(result.get("findings") or [], 1):
        if not isinstance(f, dict):
            continue
        sev = f.get("severity") if f.get("severity") in SEVERITIES else "should_fix"
        findings.append({**f, "id": f"R{a.round}-{i}", "severity": sev, "source": f"{provider}:{p['model']}"})
    cost, usage = external_cost(resp.get("usage") or {}, p, now())
    out = {"round": a.round, "provider": provider, "model": p["model"], "summary": result.get("summary", ""),
           "truncated": truncated, "findings": findings,
           "questions": [q for q in result.get("questions") or [] if isinstance(q, dict)]}
    write_json(d / f"review-{a.round}.json", out)
    write_review_md(d / f"review-{a.round}.md", f"Review round {a.round} ({provider}: {p['model']})",
                    out["summary"], findings)
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    log_event({"kind": "llm_call", "run_id": run["id"], "stage": "reviewer", "round": a.round,
               "provider": provider, "gen_ai.system": provider, "gen_ai.request.model": p["model"],
               "cost_usd_est": round(cost, 5), "billing": "api", "duration_s": secs,
               "requests": 1, "findings": counts, "truncated": truncated, **usage})
    run["review_rounds"] = max(run.get("review_rounds", 0), a.round)
    save_run(d, run)
    print(f"REVIEW round {a.round} ({provider}/{p['model']}): {counts['blocking']} blocking, "
          f"{counts['should_fix']} should_fix, {counts['nit']} nit  (${cost:.4f}, {secs}s)")
    if truncated:
        print("WARNING: diff was truncated to max_diff_chars.")
    print(f"Findings: {d}/review-{a.round}.json")


def cmd_verdict(a, cfg):
    d, run = load_run()
    lcfg = cfg.get("loop") or {}
    review = read_json(d / f"review-{a.round}.json")
    verified = read_json(d / f"verified-{a.round}.json")
    if review is None:
        sys.exit(f"Missing {d}/review-{a.round}.json")
    if verified is None:
        sys.exit(f"Missing {d}/verified-{a.round}.json (the verifier writes it)")
    by_id = {f["id"]: f for f in review.get("findings", [])}
    judged = {v.get("id"): v for v in verified.get("findings", []) if v.get("id")}
    final = []
    stats = {"proposed": len(by_id), "confirmed": 0, "rejected": 0, "uncertain": 0, "unjudged": 0,
             "unresolved_from_previous": 0, "verifier_added": 0}
    for fid, f in by_id.items():
        v = judged.get(fid)
        if not v:
            stats["unjudged"] += 1
            final.append({**f, "verdict": "unjudged"})
            continue
        verdict = v.get("verdict", "uncertain")
        stats[verdict if verdict in ("confirmed", "rejected", "uncertain") else "uncertain"] += 1
        sev = v.get("severity") if v.get("severity") in SEVERITIES else f["severity"]
        final.append({**f, "verdict": verdict, "severity": sev, "verifier_reason": v.get("reason", "")})
        if verdict == "confirmed" and f.get("status") == "unresolved":
            stats["unresolved_from_previous"] += 1
    for j, extra in enumerate(verified.get("additional") or [], 1):
        sev = extra.get("severity") if extra.get("severity") in SEVERITIES else "should_fix"
        final.append({**extra, "id": f"V{a.round}-{j}", "severity": sev, "verdict": "confirmed",
                      "source": "verifier"})
        stats["verifier_added"] += 1
    fix_sev = set(lcfg.get("fix_severities", ["blocking", "should_fix"]))
    to_fix = [f for f in final if f["verdict"] == "confirmed" and f["severity"] in fix_sev]
    # An uncertain blocking finding can't be sent to the engineer (unproven), so a human has to judge it.
    uncertain_blocking = [f for f in final if f["verdict"] == "uncertain" and f["severity"] == "blocking"]
    max_rounds = lcfg.get("quick_max_review_rounds", 1) if run.get("mode") == "quick" else lcfg.get("max_review_rounds", 3)
    write_json(d / f"final-{a.round}.json", {"round": a.round, "findings": final})
    judged_n = stats["confirmed"] + stats["rejected"] + stats["uncertain"]
    precision = round(stats["confirmed"] / judged_n, 3) if judged_n else None
    log_event({"kind": "review_quality", "run_id": run["id"], "stage": "reviewer", "round": a.round,
               "provider": review.get("provider"), "gen_ai.request.model": review.get("model"),
               "precision": precision, "to_fix": len(to_fix), **stats,
               "confirmed_by_severity": {s: sum(1 for f in final if f["verdict"] == "confirmed" and f["severity"] == s) for s in SEVERITIES}})
    if run.get("mode") == "review":
        decision = "DRAFT"
    elif not to_fix and not uncertain_blocking:
        decision = "DONE"
    elif not to_fix or a.round >= max_rounds:
        decision = "ESCALATE"
    else:
        decision = "FIX"
        write_json(d / f"fix-{a.round}.json", {"round": a.round, "findings": to_fix})
    print(f"Round {a.round}: {stats['proposed']} proposed, {stats['confirmed']} confirmed, "
          f"{stats['rejected']} rejected, {stats['uncertain']} uncertain, {stats['verifier_added']} added by verifier; "
          f"precision {precision if precision is not None else 'n/a'}")
    print(f"DECISION: {decision}")
    if decision == "FIX":
        print(f"Send the engineer: {d}/fix-{a.round}.json ({len(to_fix)} findings)")
    elif decision == "ESCALATE":
        print(f"{len(to_fix)} confirmed and {len(uncertain_blocking)} uncertain-blocking findings still open "
              f"after {a.round} round(s): {d}/final-{a.round}.json")
    if uncertain_blocking and decision != "DRAFT":
        print(f"UNCERTAIN BLOCKING ({len(uncertain_blocking)}): the verifier could not confirm or rule these out:")
        for f in uncertain_blocking:
            print(f"  - {f['id']} {f.get('file', '')}:{f.get('line', '')} {f.get('title', '')}")
    elif decision == "DRAFT":
        print("Next: harness pr draft")


# --------------------------------------------------------------------------- PR review
PR_FIELDS = ("number,title,body,url,state,isDraft,author,baseRefName,headRefName,headRefOid,"
             "comments,additions,deletions,changedFiles,closingIssuesReferences")
CLOSING_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*$", re.I)


def pr_meta(cfg: dict, rname: str, num: int) -> dict:
    sl = slug_of(cfg, rname)
    try:
        data = gh_json("pr", "view", str(num), "--repo", sl, "--json", PR_FIELDS)
    except RuntimeError as exc:  # older gh without closingIssuesReferences
        if "closingIssuesReferences" not in str(exc):
            raise
        data = gh_json("pr", "view", str(num), "--repo", sl, "--json", PR_FIELDS.rsplit(",", 1)[0])
    data["_repo"], data["_slug"] = rname, sl
    return data


def open_prs(cfg: dict, rname: str, cache: dict) -> list:
    if rname not in cache:
        try:
            cache[rname] = gh_json("pr", "list", "--repo", slug_of(cfg, rname), "--state", "open", "--limit", "80",
                                   "--json", "number,title,body,url,author,headRefName,isDraft") or []
        except RuntimeError:
            cache[rname] = []
    return cache[rname]


def is_pull(cfg: dict, rname: str, num: int) -> bool:
    try:
        out = gh("api", f"repos/{slug_of(cfg, rname)}/issues/{num}", "--jq", ".pull_request != null")
        return out.strip() == "true"
    except RuntimeError:
        return False


def extract_pr_links(text: str, cfg: dict, this_repo: str, budget: list) -> list:
    """(repo, number) pairs that refer to PRs in workspace repos."""
    found, smap, aliases = [], slug_map(cfg), repo_aliases(cfg)
    text = text or ""
    for m in re.finditer(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", text):
        r = smap.get(f"{m.group(1)}/{m.group(2)}".lower())
        if r:
            found.append((r, int(m.group(3))))
    maybe = []
    for m in re.finditer(r"(?<![\w/.-])(?:([\w.-]+)/)?([\w.-]+)#(\d+)\b", text):
        owner, name, num = m.group(1), m.group(2), int(m.group(3))
        r = smap.get(f"{owner}/{name}".lower()) if owner else aliases.get(name.lower())
        if r:
            maybe.append((r, num, m.start()))
    for m in re.finditer(r"(?<![\w/#.-])#(\d+)\b", text):
        maybe.append((this_repo, int(m.group(1)), m.start()))
    for r, num, pos in maybe:
        if CLOSING_RE.search(text[max(0, pos - 20):pos]):
            continue  # "Closes #12" refers to an issue
        if (r, num) in found:
            continue
        if budget[0] <= 0:
            break
        budget[0] -= 1
        if is_pull(cfg, r, num):
            found.append((r, num))
    return list(dict.fromkeys(found))


def dependency_branch_refs(diff: str, cfg: dict) -> list:
    """Lines like  +"acme-ui": "github:acme/acme-ui#ui/new-button"."""
    out = []
    for sl, rname in slug_map(cfg).items():
        name = re.escape(sl.split("/")[1])
        for line in diff.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            for m in re.finditer(rf'"[^"]*{name}[^"]*"\s*:\s*"[^"]*{name}(?:\.git)?#([^"]+)"', line, re.I):
                out.append((rname, m.group(1)))
    return out


def cmd_pr_start(a, cfg):
    root_repo, root_num = parse_gh_ref(a.ref, cfg, a.repo)
    try:
        me = gh("api", "user", "--jq", ".login").strip()
    except RuntimeError as exc:
        sys.exit(f"{exc}\nRun `gh auth status` / `gh auth login` first.")
    metas, reasons, context_prs, warnings, candidates = {}, {}, {}, [], {}
    cache, budget = {}, [10]
    queue = [((root_repo, root_num), 0)]
    reasons[(root_repo, root_num)] = "requested"
    while queue and len(metas) < a.max_prs:
        key, depth = queue.pop(0)
        if key in metas or key in context_prs:
            continue
        try:
            meta = pr_meta(cfg, *key)
        except RuntimeError as exc:
            warnings.append(f"{key[0]}#{key[1]}: couldn't load ({exc})")
            continue
        if meta.get("state") != "OPEN" and key != (root_repo, root_num):
            context_prs[key] = meta
            continue
        metas[key] = meta
        if a.no_discover or depth >= 2:
            continue
        rname, num = key
        texts = [meta.get("body") or ""] + [c.get("body") or "" for c in meta.get("comments") or []]
        for link in extract_pr_links("\n".join(texts), cfg, rname, budget):
            if link not in reasons:
                reasons[link] = f"linked from {rname}#{num}"
                queue.append((link, depth + 1))
        url, sl = meta.get("url", ""), meta["_slug"]
        for other in repos(cfg):
            for p in open_prs(cfg, other, cache):
                k2 = (other, p["number"])
                if k2 == key or k2 in reasons:
                    continue
                body = p.get("body") or ""
                same_repo_ref = other == rname and any(
                    not CLOSING_RE.search(body[max(0, m.start() - 20):m.start()])
                    for m in re.finditer(rf"(?<![\w/#.-])#{num}\b", body))
                mentions = bool(url and url in body) or f"{sl}#{num}".lower() in body.lower() or same_repo_ref
                if mentions:
                    reasons[k2] = f"links to {rname}#{num}"
                    queue.append((k2, depth + 1))
                elif other != rname and (p.get("author") or {}).get("login") == (meta.get("author") or {}).get("login"):
                    words = set(re.findall(r"[a-z]{4,}", (meta.get("title", "") + " " + meta.get("headRefName", "")).lower()))
                    theirs = set(re.findall(r"[a-z]{4,}", (p.get("title", "") + " " + p.get("headRefName", "")).lower()))
                    overlap = words & theirs - {"from", "with", "into", "that", "this", "update", "fixes"}
                    if len(overlap) >= 2:
                        candidates[k2] = {"repo": other, "number": p["number"], "title": p.get("title"), "url": p.get("url"),
                                          "why": f"same author, similar title/branch ({', '.join(sorted(overlap)[:4])})"}
        try:
            diff = gh("pr", "diff", str(num), "--repo", sl, timeout=120)
        except RuntimeError:
            diff = ""
        for dep_repo, ref in dependency_branch_refs(diff, cfg):
            if ref in ("dev", "main", "master") or re.fullmatch(r"v?\d[\w.]*|[0-9a-f]{7,40}", ref):
                continue
            match = [p for p in open_prs(cfg, dep_repo, cache) if p.get("headRefName") == ref]
            if match:
                k2 = (dep_repo, match[0]["number"])
                if k2 not in reasons:
                    reasons[k2] = f"{rname}#{num} installs {dep_repo} from branch '{ref}'"
                    queue.append((k2, depth + 1))
            else:
                warnings.append(f"{rname}#{num} installs {dep_repo} from branch '{ref}', but there's no open PR "
                                f"for that branch. Ask the author how it gets merged.")
    for k in list(candidates):
        if k in metas:
            candidates.pop(k)
    root = metas.get((root_repo, root_num))
    if not root:
        sys.exit(f"Couldn't load {root_repo}#{root_num}: {'; '.join(warnings)}")
    task = f"Review {root['_slug']}#{root_num}: {root.get('title', '')}"
    d, run = new_run(cfg, task, "review", a.force, {"reviewer_login": me})
    prs = []
    for key, meta in metas.items():
        pr = fetch_pr(cfg, meta, reasons.get(key, ""))
        prs.append(pr)
        if pr.get("warning"):
            warnings.append(pr["warning"])
    run["prs"], run["pr_context"] = prs, [
        {"repo": k[0], "number": k[1], "title": m.get("title"), "state": m.get("state"), "url": m.get("url")}
        for k, m in context_prs.items()]
    run["pr_candidates"], run["warnings"] = list(candidates.values()), warnings
    save_run(d, run)
    write_pr_task(d, run, metas)
    print_pr_set(d, run)


def fetch_pr(cfg: dict, meta: dict, reason: str) -> dict:
    rname, num = meta["_repo"], meta["number"]
    path = repos(cfg)[rname]["abs"]
    ref = f"refs/harness/pr/{num}"
    base = meta.get("baseRefName") or base_branch_for(cfg, rname)
    ok, msg = git_net(path, "fetch", "origin", f"+refs/pull/{num}/head:{ref}", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    pr = {"repo": rname, "slug": meta["_slug"], "number": num, "title": meta.get("title"), "url": meta.get("url"),
          "author": (meta.get("author") or {}).get("login"), "base": base, "head_branch": meta.get("headRefName"),
          "head_sha": meta.get("headRefOid"), "ref": ref, "reason": reason, "draft": meta.get("isDraft", False),
          "state": meta.get("state"), "path": str(path)}
    if not ok:
        pr["warning"] = f"{rname}#{num}: git fetch failed ({msg.splitlines()[-1] if msg else 'error'})"
        return pr
    local = git(path, "rev-parse", ref).strip()
    if pr["head_sha"] and local != pr["head_sha"]:
        pr["warning"] = f"{rname}#{num}: fetched head {local[:8]} differs from GitHub's {pr['head_sha'][:8]}"
    pr["merge_base"] = git(path, "merge-base", f"origin/{base}", ref).strip()
    pr["stat"] = git(path, "diff", "--stat", "--no-color", pr["merge_base"], ref).strip()
    return pr


def write_pr_task(d: Path, run: dict, metas: dict) -> None:
    lines = [f"# {run['task']}", ""]
    for pr in run["prs"]:
        m = metas.get((pr["repo"], pr["number"])) or {}
        lines += [f"## {pr['slug']}#{pr['number']}: {pr['title']}",
                  f"{pr['url']}  by @{pr['author']}  `{pr['head_branch']}` -> `{pr['base']}`"
                  f"{'  (draft)' if pr.get('draft') else ''}  [{pr['reason']}]", ""]
        body = (m.get("body") or "(no description)").strip()
        lines += [body[:6000] + ("\n[truncated]" if len(body) > 6000 else ""), ""]
        issues = m.get("closingIssuesReferences") or []
        if issues:
            lines += ["Closes: " + ", ".join(f"#{i.get('number')} {i.get('title', '')}" for i in issues), ""]
    if run.get("pr_context"):
        lines += ["## Linked PRs not under review"] + [
            f"- {c['repo']}#{c['number']} {c['title']} ({c['state'].lower()})" for c in run["pr_context"]] + [""]
    (d / "task.md").write_text("\n".join(lines), encoding="utf-8")


def print_pr_set(d: Path, run: dict) -> None:
    print(f"run_dir: {d}")
    print(f"Reviewing {len(run['prs'])} PR(s) together:")
    for pr in run["prs"]:
        print(f"  - {pr['repo']}#{pr['number']} {pr['title']}  (@{pr['author']}, {pr['head_branch']} -> {pr['base']})"
              f"{' [draft]' if pr.get('draft') else ''}  <- {pr['reason']}")
    for c in run.get("pr_context") or []:
        print(f"  (linked but {c['state'].lower()}, not reviewed: {c['repo']}#{c['number']} {c['title']})")
    if run.get("pr_candidates"):
        print("POSSIBLY RELATED (not included; ask the user):")
        for c in run["pr_candidates"]:
            print(f"  ? {c['repo']}#{c['number']} {c['title']}  ({c['why']})  -> harness pr add {c['repo']}#{c['number']}")
    for w in run.get("warnings") or []:
        print(f"WARNING: {w}")


def cmd_pr_add(a, cfg):
    d, run = load_run()
    if run.get("mode") != "review":
        sys.exit("The active run isn't a PR review.")
    rname, num = parse_gh_ref(a.ref, cfg, a.repo)
    if any(p["repo"] == rname and p["number"] == num for p in run["prs"]):
        print("Already included.")
        return
    meta = pr_meta(cfg, rname, num)
    run["prs"].append(fetch_pr(cfg, meta, "added by the user"))
    run["pr_candidates"] = [c for c in run.get("pr_candidates", []) if not (c["repo"] == rname and c["number"] == num)]
    save_run(d, run)
    metas = {(p["repo"], p["number"]): pr_meta(cfg, p["repo"], p["number"]) for p in run["prs"]}
    write_pr_task(d, run, metas)
    print_pr_set(d, run)


def cmd_pr_show(a, cfg):
    d, run = load_run()
    print_pr_set(d, run)
    print("\nRead a file at a PR's head without checking it out:")
    for pr in run.get("prs", []):
        print(f"  git -C {pr['path']} show {pr['ref']}:<path>     # {pr['repo']}#{pr['number']}")
        print(f"  git -C {pr['path']} grep -n <pattern> {pr['ref']}")
    print("Search the other repos' current dev:  git -C <repo path> grep -n <pattern> origin/dev")


def pr_diff(pr: dict, context: int = 12) -> str:
    if not pr.get("merge_base"):
        return "(diff unavailable: fetch failed)"
    return git(Path(pr["path"]), "diff", f"-U{context}", "--no-color", pr["merge_base"], pr["ref"])


def right_side_lines(diff: str) -> dict:
    """{path: set of new-side line numbers that GitHub can attach a review comment to}."""
    out, path, line = {}, None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
            if path:
                out.setdefault(path, set())
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None or raw.startswith(("--- ", "diff --git", "index ", "\\")):
            continue
        if raw.startswith("+") or raw.startswith(" "):
            out[path].add(line)
            line += 1
    return out


SEV_LABEL = {"blocking": "Blocking", "should_fix": "Should fix", "nit": "Nit"}


def cmd_pr_draft(a, cfg):
    d, run = load_run()
    finals = sorted(d.glob("final-*.json"), key=lambda p: int(p.stem.split("-")[1]))
    if not finals:
        sys.exit("No verified review yet (run review, the verifier, and verdict first).")
    rnd = int(finals[-1].stem.split("-")[1])
    final = read_json(finals[-1])["findings"]
    review = read_json(d / f"review-{rnd}.json", {}) or {}
    aliases = repo_aliases(cfg)
    open_prs_ = [p for p in run.get("prs", []) if p.get("merge_base")]
    by_repo = {}
    for p in open_prs_:
        by_repo.setdefault(p["repo"], []).append(p)

    def pr_for(item):
        rname = aliases.get(str(item.get("repo", "")).lower())
        cands = by_repo.get(rname) or []
        if len(cands) > 1 and item.get("pr"):
            num = re.search(r"(\d+)\s*$", str(item["pr"]))
            cands = [p for p in cands if num and p["number"] == int(num.group(1))] or cands
        return cands[0] if cands else (open_prs_[0] if len(open_prs_) == 1 else None)

    keep = [f for f in final if f.get("verdict") in ("confirmed", "uncertain")]
    questions = review.get("questions") or []
    written = []
    for pr in open_prs_:
        lines_map = right_side_lines(pr_diff(pr, context=3))
        comments, body_items = [], []
        for f in keep:
            if pr_for(f) is not pr:
                continue
            label = SEV_LABEL.get(f["severity"], "Note")
            if f["verdict"] == "uncertain":
                text = f"**Question:** {f.get('title', '')}\n\n{f.get('detail', '')}".strip()
            else:
                text = f"**{label}:** {f.get('title', '')}\n\n{f.get('detail', '')}".strip()
            if f.get("suggestion"):
                text += f"\n\nSuggestion: {f['suggestion']}"
            fpath = str(f.get("file") or "").lstrip("./")
            try:
                line = int(f.get("line")) if f.get("line") is not None else None
            except (TypeError, ValueError):
                line = None
            if fpath in lines_map and line in lines_map[fpath]:
                comments.append({"path": fpath, "line": line, "side": "RIGHT", "body": text})
            else:
                loc = f"`{fpath}{':' + str(line) if line else ''}`" if fpath else ""
                body_items.append(f"- {loc} {text.replace(chr(10) + chr(10), ' ')}".strip())
        qs = [q for q in questions if pr_for(q) is pr]
        others = [p for p in open_prs_ if p is not pr]
        body = []
        if others:
            body += ["Reviewed together with " + ", ".join(f"{p['slug']}#{p['number']}" for p in others) + ".", ""]
        if review.get("summary"):
            body += [review["summary"].strip(), ""]
        if body_items:
            body += ["**Other notes**", *body_items, ""]
        if qs:
            body += ["**Questions**", *[f"- {q.get('question') or q.get('detail') or ''}"
                                        + (f" (`{q.get('file')}`)" if q.get("file") else "") for q in qs], ""]
        if not comments and not body_items and not qs:
            body += ["Looks good to me."]
        draft = {"repo": pr["repo"], "slug": pr["slug"], "number": pr["number"], "url": pr["url"],
                 "commit_id": pr["head_sha"], "body": "\n".join(body).strip(), "comments": comments}
        jpath = d / f"draft-{pr['repo']}-{pr['number']}.json"
        write_json(jpath, draft)
        md = [f"# Draft review: {pr['slug']}#{pr['number']} {pr['title']}", pr["url"], "", "## Review body", "",
              draft["body"], "", f"## Inline comments ({len(comments)})", ""]
        for c in comments:
            md += [f"### `{c['path']}:{c['line']}`", c["body"], ""]
        (d / f"draft-{pr['repo']}-{pr['number']}.md").write_text("\n".join(md), encoding="utf-8")
        written.append(jpath)
        print(f"{pr['repo']}#{pr['number']}: {len(comments)} inline comment(s), {len(body_items)} note(s) in the body, "
              f"{len(qs)} question(s)  -> {jpath.with_suffix('.md')}")
    if not written:
        print("No open PRs with a fetched diff to draft for.")


def cmd_pr_post(a, cfg):
    d, run = load_run()
    drafts = sorted(d.glob("draft-*.json"))
    if a.pr:
        rname, num = parse_gh_ref(a.pr, cfg)
        drafts = [p for p in drafts if p.name == f"draft-{rname}-{num}.json"]
    if not drafts:
        sys.exit("No drafts to post. Run `harness pr draft` first.")
    me = gh("api", "user", "--jq", ".login").strip()
    for path in drafts:
        dr = read_json(path)
        label = f"{dr['slug']}#{dr['number']}"
        cur = gh_json("pr", "view", str(dr["number"]), "--repo", dr["slug"], "--json", "headRefOid,state")
        if cur.get("state") != "OPEN":
            print(f"{label}: skipped, PR is {cur.get('state', '?').lower()}")
            continue
        if cur.get("headRefOid") != dr["commit_id"] and not a.force:
            print(f"{label}: skipped, the author pushed new commits since this review. Re-run the review, or pass --force "
                  f"to attach the comments to the reviewed commit anyway.")
            continue
        reviews = gh_json("api", f"repos/{dr['slug']}/pulls/{dr['number']}/reviews?per_page=100") or []
        if any(r.get("state") == "PENDING" and (r.get("user") or {}).get("login") == me for r in reviews):
            print(f"{label}: skipped, you already have a pending review there. Submit or delete it on GitHub first.")
            continue
        payload = {"commit_id": dr["commit_id"], "body": dr["body"], "comments": dr["comments"]}
        try:
            out = gh("api", f"repos/{dr['slug']}/pulls/{dr['number']}/reviews", "--method", "POST", "--input", "-",
                     input_text=json.dumps(payload))
        except RuntimeError as exc:
            if not dr["comments"]:
                print(f"{label}: failed: {exc}")
                continue
            folded = dr["body"] + "\n\n**Comments**\n" + "\n".join(
                f"- `{c['path']}:{c['line']}` {c['body'].replace(chr(10) + chr(10), ' ')}" for c in dr["comments"])
            out = gh("api", f"repos/{dr['slug']}/pulls/{dr['number']}/reviews", "--method", "POST", "--input", "-",
                     input_text=json.dumps({"commit_id": dr["commit_id"], "body": folded}))
            print(f"{label}: GitHub rejected an inline comment position ({str(exc)[:120]}); posted them in the body instead.")
        rid = (json.loads(out) if out.strip() else {}).get("id")
        print(f"{label}: pending review created (only you can see it until you submit): {dr['url']}/files")
        log_event({"kind": "pr_review_posted", "run_id": run["id"], "repo": dr["repo"], "pr": dr["number"],
                   "comments": len(dr["comments"]), "review_id": rid})


# --------------------------------------------------------------------------- summary
def cmd_summary(a, cfg):
    d, run = load_run(a.run)
    rs = repos(cfg)
    lines = [f"# Run summary: {run['task']}", "", f"- Run: `{run['id']}` ({run.get('mode', 'full')} mode)",
             f"- Git mode: {(cfg.get('git') or {}).get('mode', 'uncommitted')}", ""]
    lines.append("## Branches and changes")
    changed = changed_repos(cfg, run)
    for rname in changed:
        info = run.get("repos", {}).get(rname, {})
        _, stat = repo_diff(rname, rs[rname], run)
        lines += [f"### {rname}: `{info.get('branch', current_branch(str(rs[rname]['abs'])) or '?')}` "
                  f"(from `{info.get('base_branch', '?')}`)", "```", stat.strip("\n") or "(no diff)", "```", ""]
    if not changed:
        lines += ["No changes.", ""]
    final_rounds = sorted(d.glob("final-*.json"), key=lambda p: int(p.stem.split("-")[1]))
    if final_rounds:
        last = read_json(final_rounds[-1])
        fixed_sev = set((cfg.get("loop") or {}).get("fix_severities", []))
        open_items = [f for f in last["findings"] if f.get("verdict") in ("confirmed", "uncertain")]
        lines.append(f"## Review ({len(final_rounds)} round(s))")
        if not open_items:
            lines.append("No open findings in the last round.")
        for f in open_items:
            tag = "OPEN" if f["severity"] in fixed_sev and f["verdict"] == "confirmed" else f["verdict"]
            lines.append(f"- [{f['severity']}, {tag}] {f.get('repo', '')}:{f.get('file', '')}"
                         f"{(':' + str(f['line'])) if f.get('line') else ''} {f.get('title', '')}")
        lines.append("")
    gate_events = [e for e in read_events() if e.get("run_id") == run["id"] and e.get("kind") == "gate"]
    if gate_events:
        latest: dict[tuple, dict] = {}
        for e in gate_events:
            latest[(e["repo"], e["gate"])] = e
        lines.append("## Latest gate results")
        lines += [f"- {k[0]} / {k[1]}: {'PASS' if v['passed'] else 'FAIL'}" for k, v in latest.items()]
        lines.append("")
    t = run_totals(run["id"])
    lines += ["## Cost", f"- Claude: ${t['claude_cost_usd_est']:.2f} API-equivalent "
              f"({(cfg.get('metrics') or {}).get('anthropic_billing', 'seat')} billing; manager cost is added at run end)",
              f"- Outside models: ${t['external_cost_usd']:.3f} actual", ""]
    drafts = write_pr_descriptions(d, run, cfg, changed, gate_events)
    if drafts:
        lines += ["## PR descriptions (drafts)", *[f"- `{p}`" for p in drafts], ""]
    (d / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"(saved to {d}/summary.md)")


def _plan_section(plan: str, heading: str) -> str:
    m = re.search(rf"^## {heading}\s*$(.*?)(?=^## |\Z)", plan, re.M | re.S)
    return m.group(1).strip() if m else ""


def write_pr_descriptions(d: Path, run: dict, cfg: dict, changed: list, gate_events: list) -> list:
    """Draft a PR description per changed repo (for the user to paste when opening the PR)."""
    rs = repos(cfg)
    plan = (d / "plan.md").read_text(encoding="utf-8") if (d / "plan.md").exists() else ""
    goal = (re.search(r"^# Plan:\s*(.+)$", plan, re.M) or [None, ""])[1] if plan else ""
    issue = run.get("issue") or {}
    out = []
    for rname in changed:
        info = run.get("repos", {}).get(rname, {})
        title = issue.get("title") or goal or run["task"]
        body = [f"<!-- Draft written by the harness for {info.get('branch', '')} -> {info.get('base_branch', 'dev')} -->", ""]
        if issue:
            ref = f"#{issue['number']}" if issue.get("repo") == rname else f"{issue['slug']}#{issue['number']}"
            body += [f"{'Closes' if issue.get('repo') == rname else 'Part of'} {ref}", ""]
        approach = _plan_section(plan, "Approach")
        body += ["## Summary", goal or run["task"], ""]
        if approach:
            body += [approach, ""]
        contract = _plan_section(plan, "Contract changes")
        if contract and contract.lower().strip(". ") != "none":
            body += ["## Contract changes", contract, ""]
        _, stat = repo_diff(rname, rs[rname], run)
        body += ["## Changes", "```", stat.strip("\n"), "```", ""]
        repo_gates = {}
        for e in gate_events:
            if e.get("repo") == rname:
                repo_gates[e["gate"]] = e
        if repo_gates:
            body += ["## Testing"] + [f"- {g}: {'passing' if e['passed'] else 'FAILING'}"
                                      + (f" ({e['preexisting_failures']} pre-existing failures unchanged)"
                                         if e.get("preexisting_failures") else "") for g, e in repo_gates.items()] + [""]
        others = [o for o in changed if o != rname]
        if others:
            body += ["## Related PRs", "Must be reviewed and merged together with:"]
            body += [f"- {rs[o].get('path', o)}: `{run.get('repos', {}).get(o, {}).get('branch', '?')}` (link the PR here)"
                     for o in others] + [""]
        path = d / f"pr-{rname}.md"
        path.write_text(f"# {title}\n\n" + "\n".join(body), encoding="utf-8")
        out.append(str(path))
    return out


# --------------------------------------------------------------------------- report
def fmt_table(rows: list[list], headers: list[str]) -> str:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join([line, sep, *body])


def cmd_report(a, cfg):
    events = read_events()
    cutoff = now() - dt.timedelta(days=a.days)
    events = [e for e in events if (parse_ts(e.get("ts")) or now()) >= cutoff]
    if a.run:
        events = [e for e in events if e.get("run_id") == a.run]
    if a.csv:
        import csv
        keys = sorted({k for e in events for k in e if not isinstance(e[k], (dict, list))})
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for e in events:
                w.writerow(e)
        print(f"Wrote {len(events)} events to {a.csv}")
        return
    usage = [e for e in events if e.get("kind") in ("agent", "llm_call") and not e.get("error")]
    groups: dict[tuple, list] = {}
    for e in usage:
        groups.setdefault((e.get("stage", "?"), e.get("gen_ai.request.model", "?")), []).append(e)
    rows = []
    for (stage, model), es in sorted(groups.items(), key=lambda kv: -sum(x.get("cost_usd_est") or 0 for x in kv[1])):
        n = len(es)
        tot = sum(x.get("cost_usd_est") or 0 for x in es)
        tin = sum((x.get("gen_ai.usage.input_tokens") or 0) + (x.get("gen_ai.usage.cache_read_tokens") or 0)
                  + (x.get("gen_ai.usage.cache_creation_tokens") or 0) for x in es)
        tout = sum(x.get("gen_ai.usage.output_tokens") or 0 for x in es)
        durs = [x["duration_s"] for x in es if x.get("duration_s") is not None]
        billing = es[0].get("billing", "")
        rows.append([stage, model, n, f"{tin / n / 1000:.0f}k", f"{tout / n / 1000:.1f}k",
                     f"${tot / n:.3f}", f"${tot:.2f}", billing, f"{sum(durs) / len(durs):.0f}s" if durs else ""])
    print(f"== Agents and models (last {a.days} days) ==")
    print(fmt_table(rows, ["stage", "model", "calls", "avg in", "avg out", "avg $", "total $", "billing", "avg time"])
          if rows else "(no usage recorded yet)")

    gates = [e for e in events if e.get("kind") == "gate"]
    if gates:
        first_attempts = [e for e in gates if e.get("attempt") == 1]
        print("\n== Gates ==")
        g_rows = []
        by_repo: dict[str, list] = {}
        for e in gates:
            by_repo.setdefault(f"{e['repo']}/{e['gate']}", []).append(e)
        for k, es in sorted(by_repo.items()):
            g_rows.append([k, len(es), f"{100 * sum(1 for x in es if x['passed']) / len(es):.0f}%",
                           f"{sum(x['duration_s'] for x in es) / len(es):.0f}s"])
        print(fmt_table(g_rows, ["gate", "runs", "pass rate", "avg time"]))
        if first_attempts:
            print(f"Engineer first-attempt gate pass rate: "
                  f"{100 * sum(1 for x in first_attempts if x['passed']) / len(first_attempts):.0f}% "
                  f"({len(first_attempts)} gate runs)")

    rq = [e for e in events if e.get("kind") == "review_quality"]
    if rq:
        print("\n== Reviewer quality (verifier-judged) ==")
        by_model: dict[str, list] = {}
        for e in rq:
            by_model.setdefault(f"{e.get('provider')}/{e.get('gen_ai.request.model')}", []).append(e)
        r_rows = []
        for k, es in by_model.items():
            prop = sum(x["proposed"] for x in es)
            conf = sum(x["confirmed"] for x in es)
            judged = conf + sum(x["rejected"] + x["uncertain"] for x in es)
            blk = sum((x.get("confirmed_by_severity") or {}).get("blocking", 0) for x in es)
            unres = sum(x.get("unresolved_from_previous", 0) for x in es)
            added = sum(x.get("verifier_added", 0) for x in es)
            r_rows.append([k, len(es), prop, conf, f"{100 * conf / judged:.0f}%" if judged else "n/a", blk, unres, added])
        print(fmt_table(r_rows, ["reviewer", "rounds", "proposed", "confirmed", "precision",
                                 "blocking", "not fixed", "missed*"]))
        print("*missed = issues the verifier found that the reviewer did not")

    blocks = [e for e in events if e.get("kind") == "guard_block"]
    if blocks:
        print(f"\nGit guard blocked {len(blocks)} command(s); see `harness report --csv` for details.")

    runs = {}
    for e in events:
        if e.get("kind") == "run_end":
            runs[e["run_id"]] = e
    ratings = {e["run_id"]: e for e in events if e.get("kind") == "rating"}
    if runs:
        print("\n== Runs ==")
        r_rows = []
        for rid, e in sorted(runs.items())[-a.limit:]:
            run = read_json(RUNS_DIR / rid / "run.json", {})
            r_rows.append([rid[:15], (run.get("task") or "")[:40], run.get("mode", ""), e.get("status"), e.get("review_rounds"),
                           f"${e.get('claude_cost_usd_est', 0):.2f}", f"${e.get('external_cost_usd', 0):.3f}",
                           f"{(e.get('duration_s') or 0) / 60:.0f}m", (ratings.get(rid) or {}).get("score", "")])
        print(fmt_table(r_rows, ["run", "task", "mode", "status", "rounds", "claude $*", "outside $", "time", "rating"]))
        print("*Claude cost is an API-equivalent estimate; on a Team seat it isn't billed per token.")
    ext = sum(e.get("cost_usd_est") or 0 for e in usage if e.get("billing") == "api")
    print(f"\nActual outside-model spend in window: ${ext:.3f}")


def cmd_rate(a, cfg):
    rid = a.run or current_run_id()
    if not rid:
        runs = sorted(p.name for p in RUNS_DIR.iterdir()) if RUNS_DIR.exists() else []
        rid = runs[-1] if runs else None
    if not rid:
        sys.exit("No run to rate.")
    log_event({"kind": "rating", "run_id": rid, "score": a.score, "note": a.note or ""})
    print(f"Rated {rid}: {a.score}/5")


# --------------------------------------------------------------------------- models
def cmd_models(a, cfg):
    files = sorted(AGENTS_DIR.glob("*.md"))
    if a.action == "set" and a.project:
        require_project()
        set_toml_key(project_config_path(), "models", a.agent, a.model)
        print(f"{a.agent}: model={a.model} for this project only ({project_config_path()})")
        return
    if a.action == "set":
        f = AGENTS_DIR / f"{a.agent}.md"
        if not f.exists():
            sys.exit(f"No agent file {f}")
        text = f.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not m:
            sys.exit("Agent file has no frontmatter")
        fm = m.group(1)
        fm = re.sub(r"^model:.*$", f"model: {a.model}", fm, flags=re.M) if re.search(r"^model:", fm, re.M) \
            else fm + f"\nmodel: {a.model}"
        if a.effort:
            fm = re.sub(r"^effort:.*$", f"effort: {a.effort}", fm, flags=re.M) if re.search(r"^effort:", fm, re.M) \
                else fm + f"\neffort: {a.effort}"
        f.write_text(f"---\n{fm}\n---\n" + text[m.end():], encoding="utf-8")
        print(f"{a.agent}: model={a.model}" + (f" effort={a.effort}" if a.effort else "") + " (all projects)")
        return
    rows = []
    for f in files:
        fm = re.match(r"^---\n(.*?)\n---\n", f.read_text(encoding="utf-8"), re.S)
        meta = dict(re.findall(r"^(\w+):\s*(.*)$", fm.group(1), re.M)) if fm else {}
        override = (cfg.get("models") or {}).get(f.stem)
        rows.append([f.stem, f"{override} (project)" if override else meta.get("model", "inherit"), meta.get("effort", "")])
    rc = cfg.get("reviewer") or {}
    prov = (cfg.get("providers") or {}).get(rc.get("provider", ""), {})
    rows.append(["reviewer (outside)", f"{rc.get('provider')}: {prov.get('model', '-')}", "`.claude/harness.toml`"])
    print(fmt_table(rows, ["agent", "model", "effort / where"]))


# --------------------------------------------------------------------------- init
def _marketplace_ids() -> tuple[str, str]:
    mk = read_json(PLUGIN_DIR / ".claude-plugin" / "marketplace.json", {}) or {}
    pl = read_json(PLUGIN_DIR / ".claude-plugin" / "plugin.json", {}) or {}
    plugin = (mk.get("plugins") or [{}])[0].get("name") or pl.get("name") or "harness"
    return mk.get("name") or "personal-harness", plugin


def _repo_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


def _guess_base(path: Path) -> str:
    def has(ref):
        return run_cmd(["git", "-C", str(path), "rev-parse", "--verify", "--quiet", ref]).returncode == 0
    for b in ("dev", "develop"):
        if has(f"refs/remotes/origin/{b}"):
            return b
    head = run_cmd(["git", "-C", str(path), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]).stdout.strip()
    if head.startswith("origin/"):
        return head.split("/", 1)[1]
    for b in ("main", "master", "dev"):
        if has(f"refs/heads/{b}"):
            return b
    return git(path, "branch", "--show-current").strip() or "main"


def _describe_repo(path: Path) -> str:
    bits = []
    for pj in package_jsons(path):
        try:
            deps = {**(json.loads(pj.read_text()).get("dependencies") or {}),
                    **(json.loads(pj.read_text()).get("devDependencies") or {})}
        except (json.JSONDecodeError, OSError):
            continue
        rel = pj.parent.relative_to(path).as_posix()
        kind = ("Next.js" if "next" in deps else "Expo/React Native" if "expo" in deps or "react-native" in deps
                else "React" if "react" in deps else "Node")
        lang = "TypeScript" if "typescript" in deps else "JavaScript"
        bits.append(f"{kind} ({lang})" + ("" if rel == "." else f" in {rel}"))
    if (path / "manage.py").exists() or list(path.glob("*/manage.py")):
        bits.append("Django (Python)")
    elif (path / "pyproject.toml").exists() or (path / "requirements.txt").exists():
        bits.append("Python")
    for f, label in (("Cargo.toml", "Rust"), ("go.mod", "Go"), ("Gemfile", "Ruby"), ("pubspec.yaml", "Flutter/Dart")):
        if (path / f).exists():
            bits.append(label)
    return ", ".join(dict.fromkeys(bits)) or "unknown stack"


def scan_project(root: Path, cfg: dict) -> dict:
    repos_found = []
    if is_git_repo(root) and run_cmd(["git", "-C", str(root), "rev-parse", "--show-toplevel"]).stdout.strip() == str(root):
        cands = [(root.name, root, ".")]
    else:
        cands = [(d.name, d, d.name) for d in sorted(root.iterdir()) if d.is_dir() and (d / ".git").exists()]
    for name, path, rel in cands:
        manage = next(iter(path.glob("*/manage.py")), None)
        gate_root = manage.parent if manage and not (path / "manage.py").exists() else path
        gates = detect_gates(gate_root, cfg)
        if gate_root != path:
            sub = gate_root.relative_to(path).as_posix()
            for g in gates:
                g["cwd"] = sub if g.get("cwd", ".") == "." else f"{sub}/{g['cwd']}"
        repos_found.append({"key": _repo_key(name) if rel != "." else "app", "path": rel, "folder": name,
                            "github": repo_slug(path), "base_branch": _guess_base(path),
                            "stack": _describe_repo(path), "gates": gates,
                            "claude_md": [str(p.relative_to(root)) for p in path.rglob("CLAUDE.md")
                                          if "node_modules" not in p.parts][:10]})
    gh_users = []
    try:
        st = subprocess.run([GH, "auth", "status"], capture_output=True, text=True, timeout=20)
        gh_users = sorted(set(re.findall(r"account (\S+)", st.stdout + st.stderr)))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    dirs = (cfg.get("accounts") or {}).get("dirs") or {}
    return {"root": str(root), "repos": repos_found, "github_accounts": gh_users,
            "claude_accounts": list(dirs), "current_claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR") or "(default ~/.claude)",
            "already_configured": (root / PROJECT_CONFIG_REL).exists()}


def _toml_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_project_toml(scan: dict, a) -> str:
    name = a.name or Path(scan["root"]).name
    out = [f"# Harness config for {name}. Plugin defaults (providers, pricing, loop limits) live in",
           f"# {DEFAULTS_FILE}; anything set here overrides them for this project only.", "",
           "[project]", f"name = {_toml_str(name)}",
           f"description = {_toml_str(a.description or '; '.join(r['folder'] + ': ' + r['stack'] for r in scan['repos']))}",
           "# Extra things the reviewers should check in this codebase:", "review_focus = []", "",
           "[accounts]", f"claude = {_toml_str(a.account)}" if a.account else "# claude = \"personal\"   # which Claude account (see [accounts.dirs] in the plugin defaults)",
           f"github = {_toml_str(a.github_user)}" if a.github_user else "# github = \"your-login\"  # gh account used for issues and PR reviews",
           f"git_email = {_toml_str(a.git_email)}" if a.git_email else "# git_email = \"you@example.com\"  # doctor warns if a repo commits as someone else", "",
           "[git]", f"mode = {_toml_str(a.git_mode)}"]
    bases = {r["base_branch"] for r in scan["repos"]}
    out.append(f"base_branch = {_toml_str(a.base or (bases.pop() if len(bases) == 1 else 'main'))}")
    out.append("")
    for r in scan["repos"]:
        out += [f"[repos.{r['key']}]", f"path = {_toml_str(r['path'])}", f"description = {_toml_str(r['stack'])}"]
        if a.base is None and len({x['base_branch'] for x in scan['repos']}) > 1:
            out.append(f"base_branch = {_toml_str(r['base_branch'])}")
        if r["gates"]:
            out.append("gates = [")
            for g in r["gates"]:
                parts = [f"name = {_toml_str(g['name'])}"]
                if g.get("kind"):
                    parts.append(f"kind = {_toml_str(g['kind'])}")
                if g.get("cwd", ".") != ".":
                    parts.append(f"cwd = {_toml_str(g['cwd'])}")
                parts.append(f"cmd = {_toml_str(g['cmd'])}")
                out.append("  { " + ", ".join(parts) + " },")
            out.append("]")
        else:
            out.append("# gates = [ { name = \"tests\", kind = \"jest\", cmd = \"npx jest --ci\" } ]   # none detected")
        out.append("")
    return "\n".join(out)


def merge_project_settings(root: Path, git_mode: str = "uncommitted") -> Path:
    path = root / ".claude" / "settings.json"
    data = read_json(path, {}) or {}
    market, plugin = _marketplace_ids()
    data.setdefault("extraKnownMarketplaces", {})[market] = {"source": {"source": "directory", "path": str(PLUGIN_DIR)}}
    data.setdefault("enabledPlugins", {})[f"{plugin}@{market}"] = True
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    for rule in ("Bash(python3 */scripts/harness.py *)", "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)",
                 "Bash(git branch --show-current)") + (("Bash(git add *)", "Bash(git commit *)") if git_mode == "commit" else ()):
        if rule not in allow:
            allow.append(rule)
    deny = perms.setdefault("deny", [])
    # Push is gated by the guard hook (write_accounts only), so drop the blanket deny older versions wrote.
    for rule in ("Bash(git push *)", "Bash(git push)"):
        if rule in deny:
            deny.remove(rule)
    write_json(path, data)
    return path


CLAUDE_MD_TEMPLATE = """# {name}

<!-- Written by /harness-init. Agents read this file before planning, coding or reviewing:
     keep it short, factual and current. -->

## What this is
{description}

## Repos
{repo_lines}

## Commands
<!-- How to run, test and lint each part. -->

## Conventions and rules
<!-- Architecture notes, patterns to follow, things that must never happen. -->

## Harness
- `/ship <issue or task>`, `/review-pr <PR>`, `/harness-report`. Config: `.claude/harness.toml`.
- Run files live in `.harness/` (kept out of git).
"""


def cmd_init(a, cfg):
    root = ROOT
    scan = scan_project(root, cfg)
    if not a.write:
        print(json.dumps(scan, indent=2))
        return
    if not scan["repos"]:
        sys.exit(f"No git repos found in {root} or its immediate subfolders. Run `git init` first.")
    if a.repos:
        keep = {x.strip() for x in a.repos.split(",")}
        scan["repos"] = [r for r in scan["repos"] if r["key"] in keep or r["folder"] in keep]
    cfg_path = root / PROJECT_CONFIG_REL
    if cfg_path.exists() and not a.force:
        sys.exit(f"{cfg_path} already exists. Pass --force to replace it.")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(render_project_toml(scan, a), encoding="utf-8")
    print(f"wrote {cfg_path}")
    print(f"updated {merge_project_settings(root, a.git_mode)} (plugin enabled, permissions)")
    md = root / "CLAUDE.md"
    if not md.exists():
        md.write_text(CLAUDE_MD_TEMPLATE.format(
            name=a.name or root.name, description=a.description or "TODO: one paragraph on what this project does.",
            repo_lines="\n".join(f"- `{r['path']}` ({r['key']}): {r['stack']}" for r in scan["repos"])), encoding="utf-8")
        print(f"wrote {md} (fill in the TODO sections)")
    else:
        print(f"kept existing {md}")
    ensure_state_excluded()
    print("Done. Start a new Claude Code session here and accept the trust prompt; the plugin's commands "
          "and hooks then load for this project.")


# --------------------------------------------------------------------------- accounts
def cmd_account(a, cfg):
    """Prints '<account>|<config dir>' for the project at the current directory (dir empty = default
    ~/.claude). Prints nothing outside harness projects. Used by scripts/claude-accounts.sh."""
    if a.dir_for:
        d = account_dir(cfg, a.dir_for)
        known = (cfg.get("accounts") or {}).get("dirs") or {}
        if a.dir_for not in known:
            sys.exit(f"Unknown account '{a.dir_for}'. Known: {', '.join(known)} (edit [accounts.dirs] in "
                     f"{DEFAULTS_FILE} or the project's .claude/harness.toml)")
        print(f"{a.dir_for}|{d or ''}")
        return
    if not CONFIGURED:
        return
    name = (cfg.get("accounts") or {}).get("claude")
    if name:
        print(f"{name}|{account_dir(cfg, name) or ''}")


def set_toml_key(path: Path, section: str, key: str, value: str) -> None:
    """Minimal in-place edit: set `key = "value"` under [section], creating it if needed."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f'{key} = "{value}"'
    m = re.search(rf"^\[{re.escape(section)}\]\s*$", text, re.M)
    if not m:
        text = text.rstrip("\n") + f"\n\n[{section}]\n{line}\n"
    else:
        start = m.end()
        nxt = re.search(r"^\[", text[start:], re.M)
        end = start + nxt.start() if nxt else len(text)
        body = text[start:end]
        if re.search(rf"^{re.escape(key)}\s*=", body, re.M):
            body = re.sub(rf"^{re.escape(key)}\s*=.*$", line, body, flags=re.M)
        else:
            body = "\n" + line + body if not body.startswith("\n") else "\n" + line + body
        text = text[:start] + body + text[end:]
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- doctor
def cmd_doctor(a, cfg):
    ok = True

    def line(good, text):
        nonlocal ok
        ok &= bool(good)
        print(("  ok   " if good else "  !!   ") + text)

    print(f"Project: {ROOT}")
    print(f"Plugin:  {PLUGIN_DIR}")
    line(sys.version_info >= (3, 8), f"Python {sys.version.split()[0]}")
    line(shutil.which("git"), "git on PATH")
    line(CONFIGURED, f"project config {project_config_path()}" if CONFIGURED else
         "no .claude/harness.toml here or above; run /harness-init (or `harness init --write`)")
    if not CONFIGURED:
        sys.exit(1)
    settings = read_json(ROOT / ".claude" / "settings.json", {}) or {}
    market, plugin = _marketplace_ids()
    line(settings.get("enabledPlugins", {}).get(f"{plugin}@{market}"),
         f"plugin {plugin}@{market} enabled in .claude/settings.json")
    old_copies = [p for p in (ROOT / ".claude" / "agents" / "engineer.md", ROOT / ".claude" / "skills" / "ship" / "SKILL.md",
                              ROOT / "harness" / "harness.py") if p.exists()]
    line(not old_copies, "no leftover pre-plugin harness copies" if not old_copies else
         f"old harness copies still in the project (they shadow the plugin): {', '.join(str(p.relative_to(ROOT)) for p in old_copies)}")
    acc = cfg.get("accounts") or {}
    print("\nAccounts:")
    want = acc.get("claude")
    have = os.environ.get("CLAUDE_CONFIG_DIR")
    if want:
        wd = account_dir(cfg, want)
        same = (str(Path(have).expanduser().resolve()) if have else None) == (str(Path(wd).resolve()) if wd else None)
        print(f"  {'ok  ' if same else 'info'} Claude account: '{want}' ({wd or '~/.claude'}); this shell has "
              f"CLAUDE_CONFIG_DIR={have or '(unset)'}{'' if same else ' - the claude shell function switches it on launch'}")
    else:
        print("  info Claude account: not set (uses whatever `claude` is logged in as)")
    if acc.get("github"):
        try:
            gh_env()
            line(True, f"GitHub account '{acc['github']}' available in gh")
        except RuntimeError as exc:
            line(False, str(exc))
    for name, r in repos(cfg).items():
        if is_git_repo(r["abs"]):
            email = run_cmd(["git", "-C", str(r["abs"]), "config", "user.email"]).stdout.strip()
            if acc.get("git_email"):
                line(email == acc["git_email"], f"{name}: commits as {email or '(unset)'}"
                     + ("" if email == acc["git_email"] else f", expected {acc['git_email']} (git config user.email ...)"))
            else:
                print(f"  info {name}: commits as {email or '(unset)'}")
    print("\nRepos:")
    for name, r in repos(cfg).items():
        if not is_git_repo(r["abs"]):
            line(False, f"{name}: {r['abs']} not found or not a git repo (fix `path` in .claude/harness.toml)")
            continue
        br = current_branch(str(r["abs"]))
        line(True, f"{name}: {r['abs']} (on {br})")
        gates, auto = gates_for(name, r, cfg)
        if not gates:
            line(False, f"{name}: no gates found; add `gates = [...]` in .claude/harness.toml")
        for g in gates:
            print(f"         gate {g['name']}: {g['cmd']}" + (f"  (cwd {g['cwd']})" if g.get("cwd", ".") != "." else "")
                  + ("  [auto-detected]" if auto else ""))
    print("\nReviewer:")
    rc = cfg.get("reviewer") or {}
    prov = rc.get("provider", "deepseek")
    p = (cfg.get("providers") or {}).get(prov)
    if prov == "claude":
        line(True, "provider = claude (claude-reviewer subagent; no outside model)")
    elif not p:
        line(False, f"provider '{prov}' has no [providers.{prov}] block")
    else:
        has_key = bool(os.environ.get(p.get("api_key_env", "")))
        if has_key:
            line(True, f"{prov}: {p['model']} via {p['base_url']} (${p.get('api_key_env')} set)")
        else:
            print(f"  warn {prov}: ${p.get('api_key_env')} is not set in this shell, so reviews will fall back "
                  f"to the claude-reviewer subagent. Export it before starting `claude`.")
        if has_key and a.ping:
            try:
                r = call_chat({**p, "json_mode": True, "max_tokens": 50, "extra_body": {}}, "Reply with JSON.",
                              'Return {"ok": true}', 60)
                line(True, f"ping ok: {((r.get('choices') or [{}])[0].get('message') or {}).get('content', '')[:60]!r}")
            except RuntimeError as exc:
                line(False, f"ping failed: {exc}")
    g = (cfg.get("git") or {}).get("mode", "uncommitted")
    print(f"\nGit mode: {g}  (push only as a [github].write_accounts account)")
    print("\nAll good." if ok else "\nFix the items marked !! above.")
    sys.exit(0 if ok else 1)


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="harness", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="start, end or show a pipeline run")
    rsub = run.add_subparsers(dest="action", required=True)
    s = rsub.add_parser("start")
    s.add_argument("--task", help="task description (or extra instructions when --issue is given)")
    s.add_argument("--issue", help="GitHub issue: URL, <repo>#<n>, or #<n> with --repo")
    s.add_argument("--repo", help="default repo for a bare #<n> issue reference")
    s.add_argument("--mode", choices=["full", "quick"], default="full")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_run_start)
    s = rsub.add_parser("end")
    s.add_argument("--status", choices=["done", "escalated", "aborted"], required=True)
    s.add_argument("--note")
    s.add_argument("--run")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_run_end)
    s = rsub.add_parser("show")
    s.add_argument("--run")
    s.set_defaults(fn=cmd_run_show)

    s = sub.add_parser("hook", help="(used by .claude/settings.json)")
    s.add_argument("event", choices=["subagent-stop", "guard-bash", "guard-github", "session-start"])
    s.set_defaults(fn=cmd_hook)

    s = sub.add_parser("branch", help="fetch origin and create the work branch from origin/<base>")
    s.add_argument("--repos", required=True, help="comma-separated repo names from .claude/harness.toml")
    s.add_argument("--type", help="branch type prefix: feat, fix, bug, ui, hotfix, ... (default: from issue labels, else feat)")
    s.add_argument("--name", help="slug part of the branch name (default: from the issue title or task)")
    s.add_argument("--full-name", help="use this exact branch name")
    s.add_argument("--allow-dirty", action="store_true")
    s.set_defaults(fn=cmd_branch)

    s = sub.add_parser("baseline", help="record pre-existing gate failures before editing")
    s.add_argument("--repos", required=True, help="comma-separated repo names")
    s.set_defaults(fn=cmd_baseline)

    s = sub.add_parser("gates", help="run tests/typecheck for changed repos")
    s.add_argument("--repo", action="append", help="repeatable; default: all repos with changes")
    s.add_argument("--task", help="task id (for metrics)")
    s.add_argument("--attempt", type=int, help="engineer attempt number; 1 = first try after implementing (for metrics)")
    s.set_defaults(fn=cmd_gates)

    s = sub.add_parser("diff", help="show the run's diff across repos")
    s.add_argument("--stat", action="store_true")
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("review", help="run the cross-lab reviewer for a round")
    s.add_argument("--round", type=int, required=True)
    s.add_argument("--provider")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("verdict", help="combine reviewer + verifier output and decide DONE/FIX/ESCALATE")
    s.add_argument("--round", type=int, required=True)
    s.set_defaults(fn=cmd_verdict)

    pr = sub.add_parser("pr", help="review teammates' PRs (with linked PRs from the other repos)")
    psub = pr.add_subparsers(dest="action", required=True)
    s = psub.add_parser("start", help="start a review run: load the PR and discover linked PRs")
    s.add_argument("ref", help="PR URL or <repo>#<n>")
    s.add_argument("--repo", help="default repo for a bare #<n>")
    s.add_argument("--no-discover", action="store_true", help="review only this PR")
    s.add_argument("--max-prs", type=int, default=5)
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_pr_start)
    s = psub.add_parser("add", help="add another PR to the review set")
    s.add_argument("ref")
    s.add_argument("--repo")
    s.set_defaults(fn=cmd_pr_add)
    s = psub.add_parser("show", help="list the PRs under review and how to read their files")
    s.set_defaults(fn=cmd_pr_show)
    s = psub.add_parser("draft", help="turn verified findings into review drafts per PR")
    s.set_defaults(fn=cmd_pr_draft)
    s = psub.add_parser("post", help="create PENDING GitHub reviews from the drafts")
    s.add_argument("--pr", help="only this PR (<repo>#<n>)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_pr_post)

    s = sub.add_parser("summary", help="write summary.md for the run")
    s.add_argument("--run")
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("report", help="cost and performance report")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--run")
    s.add_argument("--limit", type=int, default=15)
    s.add_argument("--csv", help="export raw events to a CSV file")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("rate", help="record your 1-5 rating of a run's result")
    s.add_argument("score", type=int, choices=range(1, 6))
    s.add_argument("--note")
    s.add_argument("--run")
    s.set_defaults(fn=cmd_rate)

    s = sub.add_parser("models", help="list or change agent models")
    s.add_argument("action", nargs="?", choices=["list", "set"], default="list")
    s.add_argument("agent", nargs="?")
    s.add_argument("model", nargs="?")
    s.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    s.add_argument("--project", action="store_true", help="override for this project only (in .claude/harness.toml)")
    s.set_defaults(fn=cmd_models)

    s = sub.add_parser("init", help="set up the harness for the project in the current folder")
    s.add_argument("--write", action="store_true", help="write the config (default: only print what was detected)")
    s.add_argument("--name")
    s.add_argument("--description")
    s.add_argument("--account", help="Claude account name for this project (e.g. personal)")
    s.add_argument("--github-user", help="gh account to use for this project's issues/PRs")
    s.add_argument("--git-email", help="expected commit email (doctor warns on mismatch)")
    s.add_argument("--git-mode", choices=["uncommitted", "commit"], default="uncommitted")
    s.add_argument("--base", help="base branch for all repos (default: detected per repo)")
    s.add_argument("--repos", help="only these repos (keys or folder names, comma-separated)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("account", help="(used by the claude shell function) which Claude account this project uses")
    s.add_argument("--dir-for", help="print the config dir for a named account")
    s.set_defaults(fn=cmd_account)

    s = sub.add_parser("doctor", help="check the setup")
    s.add_argument("--ping", action="store_true", help="also send a tiny request to the reviewer API")
    s.set_defaults(fn=cmd_doctor)

    a = ap.parse_args(argv)
    if a.cmd == "models" and a.action == "set" and not (a.agent and a.model):
        ap.error("usage: harness models set <agent> <model> [--effort level] [--project]")
    if a.cmd not in ("hook", "account", "init", "doctor", "models", "report"):
        require_project()
    a.fn(a, load_config())


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # e.g. `harness report | head`
        sys.stderr.close()
    except RuntimeError as exc:  # expected failures (gh/git/config) -> clean message
        sys.exit(f"error: {exc}")
