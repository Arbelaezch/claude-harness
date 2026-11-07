import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

_spec = importlib.util.spec_from_file_location("harness", Path(__file__).parent.parent / "scripts" / "harness.py")
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)

PLAN = """# Plan: add export
## Approach
why
## Contract changes
GET /export returns {rows}
## Tasks
### T1: endpoint  (repo: backend, model: sonnet)
- Files: a.py
- Do: lots of steps
- Done when: endpoint returns 200
"""


class HarnessTests(unittest.TestCase):
    def test_split_diff_files(self):
        d = "diff --git a/x b/x\n+1\ndiff --git a/y b/y\n+2\n"
        self.assertEqual(list(h.split_diff_files(d)), ["diff --git a/x b/x", "diff --git a/y b/y"])

    def test_plan_digest_keeps_criteria_drops_steps(self):
        out = h.plan_digest(PLAN)
        self.assertIn("Done when: endpoint returns 200", out)
        self.assertIn("GET /export", out)
        self.assertNotIn("lots of steps", out)

    def test_review_prompt_modes(self):
        self.assertIn('"questions"', h.review_system_prompt({"mode": "review"}, {}))
        self.assertNotIn('"questions"', h.review_system_prompt({"mode": "ship"}, {}))


REAL_CURRENT_BRANCH = h.current_branch
NONCE = "a" * 32
CFG = {"git": {"mode": "uncommitted", "protected_branches": ["main"]}, "github": {"write_accounts": ["me"]}}


class FactoryGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.project = os.path.join(self.home, "p", "app")
        os.makedirs(self.project)
        os.makedirs(os.path.join(self.home, "p", "app2"))
        self.branch = "feat/x"
        self.account = "writer"
        for patcher in (
            mock.patch.dict(os.environ, {"SOFTWARE_FACTORY_HOME": self.home, h.MARKER_ENV: NONCE}),
            mock.patch.object(h, "current_branch", lambda path: self.branch),
            mock.patch.object(h, "github_account_state", lambda cmd, cfg: (self.account, "stub")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def marker(self, **kw):
        m = {"project": self.project, "pid": os.getpid(), "started": time.time(), "role": "manager",
             "pr_branch": "feat/x", "issue_branch": "feat/x", **kw}
        d = h.marker_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{NONCE}.json"
        f.write_text(json.dumps(m))
        os.chmod(f, 0o600)
        return m

    def ctx(self, agent, cwd=None):
        return h.factory_context({"cwd": cwd or self.project, "agent_type": agent, "agent_id": "a1" if agent else None})

    def bash(self, cmd, agent="harness:ci-fixer", cwd=None):
        return h.check_bash(cmd, CFG, cwd or self.project, subagent=bool(agent), factory=self.ctx(agent, cwd))

    def test_no_marker_means_todays_behaviour(self):
        self.assertIsNone(self.ctx("harness:ci-fixer"))
        self.assertIn("blocked for subagents", self.bash("git push origin feat/x"))

    def test_env_without_file_and_file_without_env(self):
        self.assertIsNone(self.ctx(None))
        self.marker()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(h.MARKER_ENV)
            self.assertIsNone(self.ctx(None))

    def test_bad_marker_is_rejected(self):
        for kw in ({"pid": 2 ** 22 + 12345}, {"started": time.time() - 10 ** 6}, {"project": "/nope"}):
            self.marker(**kw)
            self.assertIsNone(h.load_marker(self.project), kw)
        self.marker()
        os.chmod(h.marker_dir() / f"{NONCE}.json", 0o666)
        self.assertIsNone(h.load_marker(self.project))
        with mock.patch.dict(os.environ, {h.MARKER_ENV: "../../etc/passwd"}):
            self.assertIsNone(h.load_marker(self.project))

    def test_marker_for_other_or_sibling_directory(self):
        self.marker()
        self.assertIsNotNone(h.load_marker(self.project))
        self.assertIsNone(h.load_marker(os.path.join(self.home, "p", "app2")))
        self.assertIsNone(h.load_marker(self.home))
        link = os.path.join(self.home, "link")
        os.symlink(os.path.join(self.home, "p", "app2"), link)
        self.assertIsNone(h.load_marker(link))

    def test_ci_fixer_pushes_only_its_pr_branch(self):
        self.marker()
        self.assertIsNone(self.bash("git push origin feat/x"))
        self.assertIsNone(self.bash("git push"))
        for cmd in ("git push origin other", "git push origin HEAD:other", "git push --force origin feat/x",
                    "git push origin feat/x --tags", "git push https://x/y.git feat/x", "git push --all"):
            self.assertIsNotNone(self.bash(cmd), cmd)
        self.branch = "other"
        self.assertIn("blocked for subagents", self.bash("git push"))

    def test_push_outside_project_via_cd_or_C(self):
        self.marker()
        out = os.path.join(self.home, "p", "app2")
        self.assertIsNotNone(self.bash(f"cd {out} && git push origin feat/x"))
        self.assertIsNotNone(self.bash(f"git -C {out} push origin feat/x"))

    def test_protected_branch_never_lifted(self):
        self.marker(pr_branch="main")
        self.branch = "main"
        self.assertIn("protected", self.bash("git push origin main"))

    def test_work_account_with_valid_marker_still_denied(self):
        self.marker()
        self.account = "other"
        self.assertIn("isn't in [github].write_accounts", self.bash("git push origin feat/x"))
        self.assertIn("isn't in [github].write_accounts", self.bash("git commit -m x", "harness:engineer"))

    def test_engineer_commits_on_issue_branch_only(self):
        self.marker()
        self.assertIsNone(self.bash("git add a.py && git commit -m x", "harness:engineer"))
        for cmd in ("git add -A", "git add .", "git commit --no-verify -m x", "git commit -nm x",
                    "git commit --amend -m x", "git push origin feat/x"):
            self.assertIsNotNone(self.bash(cmd, "harness:engineer"), cmd)
        self.branch = "main"
        self.assertIsNotNone(self.bash("git commit -m x", "harness:engineer"))

    def test_conflict_resolver_merges_base_into_pr_branch(self):
        self.marker(base_branch="develop")
        r = "harness:conflict-resolver"
        for cmd in ("git merge origin/develop", "git merge --no-edit origin/develop", "git merge --abort",
                    "git add a.py", "git commit --no-edit", "git push origin feat/x"):
            self.assertIsNone(self.bash(cmd, r), cmd)
        for cmd in ("git merge origin/other", "git merge --squash origin/develop", "git merge -X theirs origin/develop",
                    "git merge feat/y", "git rebase origin/develop", "git add -A", "git push --force origin feat/x"):
            self.assertIsNotNone(self.bash(cmd, r), cmd)
        self.branch = "other"
        self.assertIsNotNone(self.bash("git merge origin/develop", r))
        self.branch = "feat/x"
        self.assertIsNotNone(self.bash("git merge origin/develop", "harness:engineer"))
        self.account = "other"
        self.assertIn("isn't in [github].write_accounts", self.bash("git merge origin/develop", r))

    def test_other_roles_get_nothing(self):
        self.marker()
        for agent in ("harness:scout", "harness:planner", "harness:claude-reviewer", "harness:verifier", "harness:unknown"):
            for cmd in ("git push origin feat/x", "git commit -m x", "git add a.py"):
                self.assertIsNotNone(self.bash(cmd, agent), (agent, cmd))
        self.assertIsNotNone(self.bash("git push origin feat/x", "harness:engineer"))
        self.assertIsNotNone(self.bash("git merge origin/develop", "harness:ci-fixer"))

    def test_manager_needs_explicit_role(self):
        m = self.marker()
        self.assertTrue(h.factory_permission(m, None, self.project, "pr_merge"))
        self.assertFalse(h.factory_permission(m, None, self.project, "commit"))
        self.assertFalse(h.factory_permission({**m, "role": "worker"}, None, self.project, "push"))
        self.assertFalse(h.factory_permission(m, None, "/elsewhere", "push"))

    def test_gh_stays_blocked_for_subagents(self):
        self.marker()
        self.assertIn("blocked for subagents", self.bash("gh pr merge 3", "harness:ci-fixer"))

    def test_launcher_writes_and_removes_marker(self):
        probe = ("import os,sys,json;"
                 "p=os.environ['SOFTWARE_FACTORY_HOME']+'/.software-factory/markers/'+os.environ['SOFTWARE_FACTORY_MARKER']+'.json';"
                 "m=json.load(open(p));"
                 "sys.exit(7 if m['role']=='ci-fixer' and m['pr_branch']=='feat/x' and (os.stat(p).st_mode&0o777)==0o600 else 3)")
        os.environ.pop(h.MARKER_ENV)
        rc = h.run_factory(self.project, "ci-fixer", {"pr_branch": "feat/x", "issue_branch": None}, ["python3", "-c", probe])
        self.assertEqual(rc, 7)
        self.assertEqual(list(h.marker_dir().glob("*.json")), [])

    def test_launcher_marker_is_accepted_by_the_guard(self):
        nonce, path = h.write_marker(self.project, "engineer", {"issue_branch": "feat/x"})
        self.addCleanup(path.unlink, missing_ok=True)
        with mock.patch.dict(os.environ, {h.MARKER_ENV: nonce}):
            self.assertEqual(h.load_marker(self.project)["role"], "engineer")
            self.assertIsNone(self.bash("git commit -m x", "harness:engineer"))
            self.assertIsNone(h.load_marker(os.path.join(self.home, "p", "app2")))

    def test_launcher_removes_marker_when_command_fails(self):
        self.assertEqual(h.run_factory(self.project, "manager", {}, ["python3", "-c", "raise SystemExit(5)"]), 5)
        self.assertEqual(list(h.marker_dir().glob("*.json")), [])

    def test_prune_removes_only_stale_markers(self):
        live, lp = h.write_marker(self.project, "manager", {})
        old = {"project": self.project, "pid": os.getpid(), "started": time.time() - 10 ** 6, "role": "manager"}
        dead = {**old, "started": time.time(), "pid": 2 ** 22 + 12345}
        for name, body in (("b" * 32, old), ("c" * 32, dead)):
            (h.marker_dir() / f"{name}.json").write_text(json.dumps(body))
        (h.marker_dir() / ("d" * 32 + ".json")).write_text("not json")
        (h.marker_dir() / "notes.json").write_text("{}")
        self.assertEqual(h.prune_markers(), 3)
        self.assertEqual(sorted(f.name for f in h.marker_dir().glob("*.json")), sorted([lp.name, "notes.json"]))

    def test_launcher_needs_branch_fields_per_role(self):
        self.assertEqual(h.FACTORY_ROLE_FIELDS["conflict-resolver"], ("pr_branch", "base_branch"))
        self.assertEqual(set(h.FACTORY_ROLE_FIELDS), set(h.FACTORY_ROLES))

    def test_worker_session_is_held_to_its_role(self):
        """A `claude -p` worker launched with its own marker has no agent_type, but must not get main-session powers."""
        self.marker(role="ci-fixer", base_branch="develop")
        def deny(cmd, cwd=None):
            out = self.hook(h.hook_guard_bash, {"command": cmd}, None, cwd)
            return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"
        self.assertFalse(deny("git push origin feat/x"))
        self.assertFalse(deny("git add a.py && git commit -m fix"))
        for cmd in ("git push origin other", "git push --force origin feat/x", "gh pr merge 3", "gh pr create",
                    "git merge origin/develop", "git add -A"):
            self.assertTrue(deny(cmd), cmd)
        self.branch = "other"
        self.assertTrue(deny("git push"))
        self.assertTrue(deny("git commit -m x"))
        self.branch = "feat/x"
        self.assertTrue(deny("git push origin feat/x", os.path.join(self.home, "p", "app2")))  # outside the project

    def test_engineer_worker_session_cannot_push(self):
        self.marker(role="engineer")
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "git commit -m x"}))
        out = self.hook(h.hook_guard_github, {"command": "git push origin feat/x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_manager_session_keeps_main_session_powers(self):
        self.marker(role="manager")
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "git push origin feat/x"}))
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "gh pr merge 3"}))

    def test_subagent_is_decided_by_agent_id_not_agent_type(self):
        self.assertFalse(h.is_subagent({"agent_type": "probe"}, None))  # main session started with --agent
        self.assertFalse(h.is_subagent({}, None))
        self.assertTrue(h.is_subagent({"agent_type": "harness:scout", "agent_id": "a1"}, None))
        self.marker()
        for agent_type in ("probe", "harness:ci-fixer"):  # no agent_id: a main session, so the manager rules apply
            f = h.factory_context({"cwd": self.project, "agent_type": agent_type})
            self.assertFalse(h.is_subagent({"agent_type": agent_type}, f))
        f = h.factory_context({"cwd": self.project, "agent_type": "harness:ci-fixer", "agent_id": "a1"})
        self.assertTrue(h.is_subagent({}, f))

    def hook(self, fn, tool_input, agent=None, cwd=None):
        payload = json.dumps({"cwd": cwd or self.project, "agent_type": agent, "agent_id": "a1" if agent else None,
                              "tool_input": tool_input})
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(out), \
                mock.patch.object(h, "log_event", lambda e: None):
            fn(CFG)
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_marker_files_are_off_limits(self):
        self.marker()
        for tool_input in ({"command": f"echo x > {self.home}/.software-factory/markers/a.json"},
                           {"command": "cd ~/.software-factory && echo x > markers/a.json"},
                           {"file_path": str(h.marker_dir() / "a.json")},
                           {"notebook_path": str(h.marker_dir() / "a.ipynb")}):
            for fn in (h.hook_guard_bash, h.hook_guard_github):
                for agent in (None, "harness:engineer"):
                    out = self.hook(fn, tool_input, agent)
                    self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", (tool_input, fn, agent))
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "ls", "file_path": "a.py"}))

    def test_no_marker_leaves_dot_dir_alone(self):
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "ls ~/.software-factory"}))

    def test_git_redirection_blocked_for_factory_subagents(self):
        self.marker()
        other = os.path.join(self.home, "p", "app2")
        for cmd in (f"git --git-dir={other}/.git push origin feat/x", f"git --git-dir {other}/.git push",
                    "git --work-tree=/x push origin feat/x", "git -c remote.origin.pushurl=u push origin feat/x",
                    "git config remote.origin.pushurl u", "git config url.u.insteadOf v"):
            self.assertIsNotNone(self.bash(cmd), cmd)
            self.assertIsNotNone(h.check_github_global(cmd, CFG, self.project, True, self.ctx("harness:ci-fixer")), cmd)
        self.assertIsNone(self.bash(f"git -C {self.project} push origin feat/x"))
        self.assertIsNone(self.bash("git config --get remote.origin.url"))

    def test_hooks_with_a_real_repo(self):
        self.marker()
        subprocess.run(["git", "init", "-q", "-b", "feat/x", self.project], check=True)
        with mock.patch.object(h, "current_branch", REAL_CURRENT_BRANCH):
            denied = lambda fn, cmd, agent: (self.hook(fn, {"command": cmd}, agent) or {}).get(
                "hookSpecificOutput", {}).get("permissionDecision") == "deny"
            for fn in (h.hook_guard_bash, h.hook_guard_github):
                self.assertFalse(denied(fn, "git push origin feat/x", "harness:ci-fixer"), fn)
                self.assertTrue(denied(fn, "git push origin feat/x", "harness:scout"), fn)
                self.assertTrue(denied(fn, "git push origin other", "harness:ci-fixer"), fn)
            subprocess.run(["git", "-C", self.project, "checkout", "-q", "-b", "other"], check=True)
            self.assertTrue(denied(h.hook_guard_bash, "git push", "harness:ci-fixer"))
            self.assertFalse(denied(h.hook_guard_bash, "git push", None))  # the main session is not a factory subagent


class ShellParsingTests(unittest.TestCase):
    """Text that only mentions a command is data; things that run are still caught (issues #3 and #18)."""

    def cmds(self, command):
        return [t[0] for t in h.split_segments(command)]

    def guard(self, command, **kw):
        with mock.patch.object(h, "github_account_state", lambda c, cfg: ("writer", "stub")), \
                mock.patch.object(h, "current_branch", lambda p: "feat/x"):
            return h.check_bash(command, CFG, "/p", **kw), h.check_github_global(command, CFG, "/p", **kw)

    def test_quoted_operators_do_not_split(self):
        self.assertEqual(self.cmds("grep -E 'git push|git add' f"), ["grep"])
        self.assertEqual(self.cmds('echo "a; git push && b"'), ["echo"])
        self.assertEqual(self.cmds("echo a\\; git push"), ["echo"])

    def test_heredoc_bodies_are_data(self):
        for cmd in ("cat > f.md <<'EOF'\nuse gh auth switch and GH_TOKEN=x\ngit push origin main\nEOF\nls",
                    "cat <<-EOF\n\tgit add .\n\tEOF\nls", 'cat <<"X"\ngit push origin main\nX'):
            self.assertEqual(self.guard(cmd), (None, None), cmd)
        self.assertEqual(self.cmds("cat <<EOF && ls\nbody\nEOF"), ["cat", "ls"])

    def test_things_that_run_are_still_checked(self):
        for cmd in ("echo ok && git push origin main", 'echo "$(git push origin main)"', "echo `git push origin main`",
                    'bash -c "git push origin main"', "sh -lc 'git push --force origin x'",
                    "eval 'git push origin main'", "cat <<EOF\n$(git push origin main)\nEOF",
                    "echo 'unterminated && git push origin main", "(git push origin main)", "git push origin main &"):
            self.assertIn("git", " ".join(sum(h.split_segments(cmd), [])), cmd)
            self.assertTrue(any(self.guard(cmd)), cmd)
        self.assertTrue(any(self.guard("gh auth switch")))
        self.assertTrue(any(self.guard("echo x; gh auth switch -u other")))

    def test_token_overrides_only_count_as_env_assignments(self):
        self.assertEqual(self.guard("echo 'GH_TOKEN is documented here'"), (None, None))
        self.assertEqual(self.guard("cat <<'EOF'\nGH_TOKEN=x gh pr create\nEOF"), (None, None))
        for cmd in ("GH_TOKEN=x gh pr create", "export GH_HOST=h; gh pr create", "env GITHUB_TOKEN=x gh pr create",
                    'bash -c "GH_TOKEN=x gh pr create"'):
            self.assertTrue(any(self.guard(cmd)), cmd)


class NotifyTests(unittest.TestCase):
    def msg(self, **kw):
        base = dict(kind="BLOCKER", project="Callback", issue=12, situation="CI failed twice on the PR. The fix did not help.",
                    need="Decide whether to retry or close the PR.", options=["Retry once more", "Close the PR"],
                    recommended=2, detail="https://example.test/log")
        return h.format_message(**{**base, **kw})

    def test_standard_shape(self):
        out = self.msg()
        self.assertEqual(out.splitlines(), [
            "[BLOCKER]  Callback #12",
            "What is going on:   CI failed twice on the PR. The fix did not help.",
            "What I need:        Decide whether to retry or close the PR.",
            "Options:", "  1. Retry once more", "  2. Close the PR  (recommended)",
            "Detail:             https://example.test/log"])

    def test_done_needs_no_decision(self):
        out = self.msg(kind="done", need="", options=None, recommended=None, detail="", issue="#7")
        self.assertIn("[DONE]  Callback #7", out)
        self.assertIn("Nothing, for your information.", out)
        self.assertNotIn("Options", out)

    def test_bad_messages_are_rejected(self):
        for kw in ({"kind": "FYI"}, {"situation": "One. Two. Three. Four."}, {"situation": " "}, {"need": ""},
                   {"need": "a\nb"}, {"options": ["only one"], "recommended": 1}, {"options": list("abcd"), "recommended": 1},
                   {"recommended": None}, {"recommended": 3}, {"kind": "DONE", "need": ""}):
            with self.assertRaises(ValueError, msg=kw):
                self.msg(**kw)
        with self.assertRaises(ValueError):
            self.msg(options=None, recommended=2)

    def test_sentence_count(self):
        self.assertEqual(h.count_sentences("One. Two! Three? Four."), 4)
        self.assertEqual(h.count_sentences("Version 1.2 failed in step 3."), 1)

    def test_notify_terminal_and_unknown_channels(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            h.notify("hello")
        self.assertIn("hello", out.getvalue())
        with self.assertRaises(ValueError):
            h.notify("x", ("pager",))

    def test_push_is_scaffolding_and_never_loses_the_message(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            h.notify("hello", ("push",))
        self.assertIn("hello", out.getvalue())
        self.assertIn("not built", err.getvalue())

    def test_a_new_channel_is_one_class(self):
        got = []

        class Memory(h.Channel):
            name = "memory"

            def send(self, text):
                got.append(text)

        with mock.patch.dict(h.CHANNELS, {"memory": Memory}), contextlib.redirect_stdout(io.StringIO()):
            h.notify("hi", ("memory",))
        self.assertEqual(got, ["hi"])


def issue(key, deps=(), **kw):
    return {"key": key, "title": f"Do {key}", "body": f"Body of {key}.", "acceptance": [f"{key} works"],
            "depends_on": list(deps), **kw}


class IssuePlanTests(unittest.TestCase):
    def test_valid_plan_and_order(self):
        plan = {"issues": [issue("c", ["b"]), issue("a"), issue("b", ["a"])]}
        self.assertEqual(h.validate_issue_plan(plan), [])
        self.assertEqual(h.issue_order(plan), ["a", "b", "c"])

    def test_problems_are_reported(self):
        def errs(*issues):
            return " | ".join(h.validate_issue_plan({"issues": list(issues)}))
        self.assertIn("at least one", errs())
        self.assertIn("duplicate key", errs(issue("a"), issue("a", title="Other")))
        self.assertIn("share a title", errs(issue("a", title="Same"), issue("b", title="Same")))
        self.assertIn("unknown key", errs(issue("a", ["zzz"])))
        self.assertIn("depends on itself", errs(issue("a", ["a"])))
        self.assertIn("cycle", errs(issue("a", ["b"]), issue("b", ["a"])))
        self.assertIn("acceptance", errs(issue("a", acceptance=[])))
        self.assertIn("acceptance", errs(issue("a", acceptance=["ok", " "])))
        self.assertIn("body", errs(issue("a", body=" ")))
        self.assertIn("title", errs(issue("a", title="x")))
        self.assertIn("key", errs(issue("Bad Key")))
        self.assertIn("labels", errs(issue("a", labels="enhancement")))
        self.assertIn("must be an object", errs("nope"))
        self.assertIn("at least one", " ".join(h.validate_issue_plan([])))

    def test_body_has_criteria_and_dependencies(self):
        body = h.render_issue_body(issue("b", ["a"]), {"a": 7})
        self.assertIn("## Acceptance criteria\n- [ ] b works", body)
        self.assertTrue(body.rstrip().endswith("Depends on #7"))
        self.assertNotIn("Depends on", h.render_issue_body(issue("a"), {}))

    def run_create(self, plan, existing=(), todo=False, dry_run=False, have_labels=("enhancement", "bug")):
        calls = []

        def fake_gh(*args, input_text=None, **kw):
            calls.append((args, input_text))
            return f"https://github.com/o/r/issues/{100 + sum(1 for c, _ in calls if c[:2] == ('issue', 'create'))}\n"

        def fake_json(*args):
            if args[:2] == ("label", "list"):
                return [{"name": n} for n in have_labels]
            return [{"title": t, "number": n} for t, n in existing]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(h, "ROOT", Path(tmp)), \
                mock.patch.object(h, "gh", fake_gh), mock.patch.object(h, "gh_json", fake_json), \
                mock.patch.object(h, "slug_of", lambda cfg, r: "o/r"), mock.patch.object(h, "repos", lambda cfg: {"app": {}}), \
                mock.patch.object(h, "load_issue_plan", lambda f: plan), contextlib.redirect_stdout(io.StringIO()):
            a = mock.Mock(file="x", repo=None, todo=todo, dry_run=dry_run)
            h.cmd_factory_issues_create(a, {})
            log = (Path(tmp) / ".factory" / "log.jsonl")
            logged = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return calls, logged

    def test_create_makes_dependencies_first_and_links_numbers(self):
        plan = {"issues": [issue("b", ["a"]), issue("a")]}
        calls, logged = self.run_create(plan)
        creates = [(c, body) for c, body in calls if c[:2] == ("issue", "create")]
        self.assertEqual([c[c.index("--title") + 1] for c, _ in creates], ["Do a", "Do b"])
        self.assertIn("Depends on #101", creates[1][1])
        self.assertNotIn("todo", [c[i + 1] for c, _ in creates for i, x in enumerate(c) if x == "--label"])
        self.assertEqual([(e["key"], e["number"]) for e in logged], [("a", 101), ("b", 102)])

    def test_create_skips_existing_titles_and_adds_todo_when_asked(self):
        plan = {"issues": [issue("b", ["a"]), issue("a")]}
        calls, logged = self.run_create(plan, existing=[("Do a", 5)], todo=True)
        creates = [(c, body) for c, body in calls if c[:2] == ("issue", "create")]
        self.assertEqual(len(creates), 1)
        self.assertIn("Depends on #5", creates[0][1])
        self.assertIn("todo", creates[0][0])
        self.assertIn(("label", "create", "todo"), [c[:3] for c, _ in calls])  # factory label made on demand

    def test_missing_non_factory_label_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.run_create({"issues": [issue("a", labels=["wizardry"])]})

    def test_dry_run_creates_nothing(self):
        calls, logged = self.run_create({"issues": [issue("a")]}, dry_run=True)
        self.assertEqual((calls, logged), ([], []))


if __name__ == "__main__":
    unittest.main()
