import importlib.util
import unittest
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


if __name__ == "__main__":
    unittest.main()
