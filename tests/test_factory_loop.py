import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).parent.parent / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fl = load("factory_loop")
h = load("harness")


def issue(n, labels=("todo",), state="OPEN", body="", title=None):
    return {"number": n, "title": title or f"Issue {n}", "body": body, "state": state,
            "labels": [{"name": x} for x in labels]}


class PoolTests(unittest.TestCase):
    def test_parse_depends(self):
        self.assertEqual(fl.parse_depends("Text\n\nDepends on #7\ndepends on #3\nSee #9 too"), [3, 7])
        self.assertEqual(fl.parse_depends(None), [])

    def test_pool_needs_todo_open_and_closed_dependencies(self):
        issues = [issue(1, state="CLOSED", labels=()), issue(2, body="Depends on #1"), issue(3, body="Depends on #2"),
                  issue(4, labels=()), issue(5, state="CLOSED"), issue(6, body="Depends on #99")]
        self.assertEqual([p["number"] for p in fl.compute_pool(issues)], [2])

    def test_pool_skips_other_states(self):
        for state in ("in-progress", "awaiting-merge", "blocked", "backlog"):
            self.assertEqual(fl.compute_pool([issue(1, labels=("todo", state))]), [], state)
        self.assertEqual(fl.compute_pool([issue(1, labels=("todo", "enhancement"))])[0]["labels"], ["enhancement"])

    def test_dependency_in_awaiting_merge_is_not_met(self):
        issues = [issue(1, labels=("awaiting-merge",)), issue(2, body="Depends on #1")]
        self.assertEqual(fl.compute_pool(issues), [])


class SelectorTests(unittest.TestCase):
    def test_parse_pick(self):
        self.assertEqual(fl.parse_pick("thinking\nPICK: 4 | unblocks two", {3, 4}), (4, "unblocks two"))
        self.assertEqual(fl.parse_pick("PICK: #3", {3}), (3, "no reason given"))
        self.assertIsNone(fl.parse_pick("PICK: 9 | nope", {3, 4}))
        self.assertIsNone(fl.parse_pick("no pick here", {3}))
        self.assertEqual(fl.parse_pick("PICK: 3 | a\nPICK: 4 | b", {3, 4}), (4, "b"))

    def test_choose(self):
        pool = [{"number": 3}, {"number": 8}]
        ask = mock.Mock()
        self.assertEqual(fl.choose(pool[:1], ask), (3, "only candidate"))
        ask.assert_not_called()
        self.assertEqual(fl.choose(pool, lambda p: "PICK: 8 | smaller")[0], 8)
        self.assertEqual(fl.choose(pool, lambda p: "garbage")[0], 3)
        self.assertEqual(fl.choose(pool, lambda p: 1 / 0)[0], 3)
        self.assertEqual(fl.choose(pool, lambda p: "PICK: 99 | x")[0], 3)


class GateTests(unittest.TestCase):
    def gate(self, readings, now=1000.0, **kw):
        it = iter(readings)
        sleeps, events = [], []
        out = fl.usage_gate(lambda: next(it), sleep=sleeps.append, now=lambda: now, log=events.append, **kw)
        return out, sleeps, events

    def test_go(self):
        self.assertEqual(self.gate([{"decision": "go"}])[0], "go")

    def test_pause_sleeps_until_reset_plus_buffer_then_continues(self):
        out, sleeps, events = self.gate([{"decision": "pause", "resume_at": 1600.0, "reasons": ["x"]}, {"decision": "go"}])
        self.assertEqual((out, sleeps), ("go", [660.0]))
        self.assertEqual([e["kind"] for e in events], ["usage_pause", "usage_resume"])

    def test_pause_without_reset_time_retries_later(self):
        self.assertEqual(self.gate([{"decision": "pause"}, {"decision": "go"}])[1], [60])

    def test_unknown_three_times_in_a_row_stops(self):
        out, sleeps, _ = self.gate([{"decision": "unknown"}] * 3)
        self.assertEqual((out, sleeps), ("stop", [60, 60]))

    def test_unknown_counter_resets(self):
        unknown = {"decision": "unknown"}
        out, _, _ = self.gate([unknown, unknown, {"decision": "pause", "resume_at": 1100.0}, unknown, unknown, {"decision": "go"}])
        self.assertEqual(out, "go")


class LoopTests(unittest.TestCase):
    def make(self, issues, outcomes=None, gate=lambda: "go", select=None, **kw):
        labels = {i["number"]: {x["name"] for x in i["labels"]} for i in issues}
        calls = {"states": [], "ran": [], "blockers": [], "log": []}
        outcomes = outcomes or {}

        def set_state(n, st):
            calls["states"].append((n, st))
            labels[n] = (labels[n] - set(fl.STATE_LABELS)) | {st}

        def run_issue(n):
            calls["ran"].append(n)
            if n in outcomes:
                labels[n] = (labels[n] - set(fl.STATE_LABELS)) | {outcomes[n]}
            return 0

        def fetch():
            return [{**i, "labels": [{"name": x} for x in labels[i["number"]]],
                     "state": "CLOSED" if "closed" in labels[i["number"]] else i["state"]} for i in issues]

        result = fl.run_loop(gate=gate, fetch_issues=fetch, select=select or (lambda pool: (pool[0]["number"], "first")),
                             set_state=set_state, run_issue=run_issue, fetch_labels=lambda n: labels[n],
                             raise_blocker=lambda text, n, need: calls["blockers"].append((n, text)),
                             log=calls["log"].append, **kw)
        return result, calls, labels

    def test_runs_the_pool_in_selected_order_until_empty(self):
        issues = [issue(1), issue(2), issue(3, labels=())]
        result, calls, _ = self.make(issues, {1: "awaiting-merge", 2: "blocked"}, select=lambda pool: (pool[-1]["number"], "x"))
        self.assertEqual(calls["ran"], [2, 1])
        self.assertEqual(result["results"], [{"issue": 2, "outcome": "blocked"}, {"issue": 1, "outcome": "awaiting-merge"}])
        self.assertEqual(calls["blockers"], [])
        self.assertIn(("harness_started"), [e["kind"] for e in calls["log"]])

    def test_session_that_records_nothing_becomes_blocked(self):
        result, calls, labels = self.make([issue(1)])
        self.assertEqual(result["results"], [{"issue": 1, "outcome": "blocked"}])
        self.assertIn("blocked", labels[1])
        self.assertEqual(len(calls["blockers"]), 1)

    def test_an_issue_is_not_retried_in_the_same_run(self):
        result, calls, labels = self.make([issue(1)], {1: "todo"})
        self.assertEqual(calls["ran"], [1])

    def test_dependencies_unlock_during_the_run(self):
        issues = [issue(1), issue(2, body="Depends on #1")]
        result, calls, _ = self.make(issues, {1: "closed"})
        self.assertEqual(calls["ran"], [1, 2])

    def test_orphaned_in_progress_issues_are_handed_back(self):
        result, calls, labels = self.make([issue(1, labels=("in-progress",)), issue(2)], {2: "awaiting-merge"})
        self.assertEqual(calls["states"][0], (1, "blocked"))
        self.assertEqual(calls["blockers"][0][0], 1)
        self.assertEqual(calls["ran"], [2])

    def test_usage_stop_halts_and_raises_a_blocker(self):
        result, calls, _ = self.make([issue(1)], gate=lambda: "stop")
        self.assertEqual((calls["ran"], result["stopped"]), ([], "usage unknown"))
        self.assertEqual(calls["blockers"][0][0], None)

    def test_max_issues(self):
        issues = [issue(n) for n in (1, 2, 3)]
        result, calls, _ = self.make(issues, {n: "awaiting-merge" for n in (1, 2, 3)}, max_issues=2)
        self.assertEqual(calls["ran"], [1, 2])


class HarnessWiringTests(unittest.TestCase):
    def test_set_issue_state_swaps_the_state_label(self):
        calls = []
        with mock.patch.object(h, "ensure_labels"), \
                mock.patch.object(h, "issue_labels", lambda slug, n: {"todo", "enhancement"}), \
                mock.patch.object(h, "gh", lambda *a, **k: calls.append(a)):
            h.set_issue_state("o/r", 5, "in-progress")
            h.set_issue_state("o/r", 5, "todo")  # already there: nothing to do
        self.assertEqual(calls, [("issue", "edit", "5", "--repo", "o/r", "--add-label", "in-progress", "--remove-label", "todo")])

    def test_block_requires_a_valid_message_and_labels_the_issue(self):
        a = mock.Mock(issue=7, kind="BLOCKER", situation="Gates keep failing.", need="Decide whether to skip.",
                      option=None, recommend=None, detail="/x/log", repo=None)
        states, comments = [], []
        with mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), \
                mock.patch.object(h, "set_issue_state", lambda s, n, st: states.append((n, st))), \
                mock.patch.object(h, "factory_comment", lambda s, n, t: comments.append(n)), \
                mock.patch.object(h, "factory_log"), contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_factory_block(a, {"project": {"name": "App"}})
        self.assertEqual((states, comments), ([(7, "blocked")], [7]))
        self.assertIn("[BLOCKER]  App #7", out.getvalue())
        a.need = ""
        with mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), self.assertRaises(SystemExit):
            h.cmd_factory_block(a, {})

    def test_done_labels_awaiting_merge(self):
        a = mock.Mock(issue=7, pr="https://x/pull/9", note=None, repo=None)
        states = []
        with mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), \
                mock.patch.object(h, "set_issue_state", lambda s, n, st: states.append((n, st))), \
                mock.patch.object(h, "factory_comment"), mock.patch.object(h, "factory_log"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_factory_done(a, {})
        self.assertEqual(states, [(7, "awaiting-merge")])
        self.assertIn("[DONE]", out.getvalue())

    def test_run_starts_a_fresh_marked_session_per_issue(self):
        import json
        import os
        import tempfile
        import types
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            fake = Path(tmp) / "fake-claude.sh"
            fake.write_text('#!/bin/sh\necho "args: $@"\necho "marker set: ${SOFTWARE_FACTORY_MARKER:+yes}"\n'
                            'ls "$SOFTWARE_FACTORY_HOME/.software-factory/markers" | wc -l\n')
            fake.chmod(0o755)
            labels = {4: {"todo"}}
            gh_calls = []

            def fake_json(*args):
                if args[:2] == ("issue", "list"):
                    return [issue(4, labels=tuple(labels[4]))]
                if args[:2] == ("issue", "view"):
                    return {"labels": [{"name": x} for x in labels[4]]}
                return [{"name": x} for x in fl.STATE_LABELS]

            def fake_gh(*args, **kw):
                gh_calls.append(args)
                return ""

            def set_state(slug, n, st):
                labels[n] = (labels[n] - set(fl.STATE_LABELS)) | {st}

            usage = types.SimpleNamespace(check=lambda limits: {"decision": "go"})
            a = mock.Mock(merge="manual", issues=None, dry_run=False, max_issues=5, repo=None)
            env = {"HARNESS_CLAUDE": str(fake), "SOFTWARE_FACTORY_HOME": tmp}
            with mock.patch.dict("os.environ", env), mock.patch.object(h, "ROOT", root), \
                    mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), \
                    mock.patch.object(h, "factory_modules", lambda: (fl, usage)), \
                    mock.patch.object(h, "gh_json", fake_json), mock.patch.object(h, "gh", fake_gh), \
                    mock.patch.object(h, "set_issue_state", set_state), mock.patch.object(h, "ensure_labels"), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                h.cmd_factory_run(a, {"factory": {"claude_args": ["--permission-mode", "acceptEdits"]}})
            logs = list((root / ".factory" / "sessions").glob("issue-4-*.log"))
            text = logs[0].read_text()
            events = [json.loads(x)["kind"] for x in (root / ".factory" / "log.jsonl").read_text().splitlines()]
            left = list((Path(tmp) / ".software-factory" / "markers").glob("*.json"))
        self.assertIn("args: -p /factory-issue 4 --permission-mode acceptEdits", text)
        self.assertIn("marker set: yes", text)
        self.assertEqual(text.splitlines()[-1].strip(), "1")  # exactly one marker while the session ran
        self.assertEqual(left, [])
        self.assertEqual(labels[4], {"blocked"})  # the fake session recorded nothing, so the issue is handed back
        self.assertIn("1 issue(s) run", out.getvalue())
        self.assertEqual([e for e in events if e in ("issue_picked", "harness_started", "harness_finished")],
                         ["issue_picked", "harness_started", "harness_finished"])

    def test_auto_merge_is_not_built_yet(self):
        with self.assertRaises(SystemExit):
            h.cmd_factory_run(mock.Mock(merge="auto"), {})


if __name__ == "__main__":
    unittest.main()
