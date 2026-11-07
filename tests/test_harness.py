import importlib.util
import json
import os
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
        return h.factory_context({"cwd": cwd or self.project, "agent_type": agent})

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
            self.assertIsNone(self.ctx(None), kw)
        self.marker()
        os.chmod(h.marker_dir() / f"{NONCE}.json", 0o666)
        self.assertIsNone(self.ctx(None))
        with mock.patch.dict(os.environ, {h.MARKER_ENV: "../../etc/passwd"}):
            self.assertIsNone(self.ctx(None))

    def test_marker_for_other_or_sibling_directory(self):
        self.marker()
        self.assertIsNotNone(self.ctx(None))
        self.assertIsNone(self.ctx(None, os.path.join(self.home, "p", "app2")))
        self.assertIsNone(self.ctx(None, self.home))
        link = os.path.join(self.home, "link")
        os.symlink(os.path.join(self.home, "p", "app2"), link)
        self.assertIsNone(self.ctx(None, link))

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
        self.assertIsNotNone(self.bash("git commit -m x", "harness:ci-fixer"))

    def test_manager_needs_explicit_role(self):
        m = self.marker()
        self.assertTrue(h.factory_permission(m, None, self.project, "pr_merge"))
        self.assertFalse(h.factory_permission(m, None, self.project, "commit"))
        self.assertFalse(h.factory_permission({**m, "role": "worker"}, None, self.project, "push"))
        self.assertFalse(h.factory_permission(m, None, "/elsewhere", "push"))

    def test_gh_stays_blocked_for_subagents(self):
        self.marker()
        self.assertIn("blocked for subagents", self.bash("gh pr merge 3", "harness:ci-fixer"))

    def test_marker_files_are_off_limits(self):
        self.assertIsNotNone(h.marker_tamper_denial(f"echo x > {self.home}/.software-factory/markers/a.json", None, None))
        self.assertIsNotNone(h.marker_tamper_denial("", str(h.marker_dir() / "a.json"), self.project))
        self.assertIsNone(h.marker_tamper_denial("ls", "a.py", self.project))


if __name__ == "__main__":
    unittest.main()
