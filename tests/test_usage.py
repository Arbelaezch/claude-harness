import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("usage", Path(__file__).parent.parent / "scripts" / "usage.py")
u = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(u)

NOW = 1_000_000.0
LIMITS = {"five_hour": 80, "seven_day": 90}


def reading(five=10, week=10, five_reset=NOW + 100, week_reset=NOW + 1000, **extra):
    return {"windows": {"five_hour": {"used_pct": five, "resets_at": five_reset, "status": None},
                        "seven_day": {"used_pct": week, "resets_at": week_reset, "status": None}},
            "using_overage": False, **extra}


class UsageTests(unittest.TestCase):
    def test_stream_normalizes_fraction_to_percent(self):
        ev = {"type": "rate_limit_event", "rate_limit_info": {"isUsingOverage": False, "unifiedWindows": {
            "five_hour": {"utilization": 0.01, "resetsAt": 5, "status": "allowed"},
            "seven_day": {"utilization": 0.02, "resetsAt": 9, "status": "allowed"}}}}
        r = u.from_stream('{"type":"system"}\nnot json\n' + json.dumps(ev))
        self.assertAlmostEqual(r["windows"]["five_hour"]["used_pct"], 1.0)
        self.assertEqual(r["windows"]["seven_day"]["resets_at"], 9)

    def test_stream_without_event_is_empty(self):
        self.assertEqual(u.from_stream('{"type":"result"}')["windows"], {})

    def test_statusline_keeps_percent(self):
        r = u.from_statusline({"rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": 7}}})
        self.assertEqual(r["windows"]["five_hour"]["used_pct"], 42.0)
        self.assertNotIn("seven_day", r["windows"])

    def test_go_below_limits(self):
        self.assertEqual(u.decide(reading(), LIMITS, NOW)["decision"], "go")

    def test_pause_on_five_hour_resumes_at_its_reset(self):
        d = u.decide(reading(five=85), LIMITS, NOW)
        self.assertEqual((d["decision"], d["resume_at"]), ("pause", NOW + 100))

    def test_pause_on_both_waits_for_latest_reset(self):
        d = u.decide(reading(five=85, week=95), LIMITS, NOW)
        self.assertEqual(d["resume_at"], NOW + 1000)

    def test_limit_is_inclusive(self):
        self.assertEqual(u.decide(reading(five=80), LIMITS, NOW)["decision"], "pause")

    def test_overage_pauses(self):
        self.assertEqual(u.decide(reading(using_overage=True), LIMITS, NOW)["decision"], "pause")

    def test_non_allowed_status_pauses(self):
        r = reading()
        r["windows"]["five_hour"]["status"] = "rejected"
        self.assertEqual(u.decide(r, LIMITS, NOW)["decision"], "pause")

    def test_unknown_when_no_data_or_missing_window_or_expired(self):
        self.assertEqual(u.decide(None, LIMITS, NOW)["decision"], "unknown")
        r = reading()
        del r["windows"]["seven_day"]
        self.assertEqual(u.decide(r, LIMITS, NOW)["decision"], "unknown")
        self.assertEqual(u.decide(reading(five_reset=NOW - 1), LIMITS, NOW)["decision"], "unknown")

    def test_snapshot_age_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snap.json"
            self.assertIsNone(u.read_snapshot(p))
            p.write_text(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 3, "resets_at": 1}}}))
            mtime = p.stat().st_mtime
            self.assertEqual(u.read_snapshot(p, 300, mtime + 10)["source"], "snapshot")
            self.assertIsNone(u.read_snapshot(p, 300, mtime + 301))
            p.write_text("{bad")
            os.utime(p, (mtime, mtime))
            self.assertIsNone(u.read_snapshot(p, 300, mtime + 1))


if __name__ == "__main__":
    unittest.main()
