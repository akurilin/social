import datetime as dt
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import events_store


def source_result(state, events=()):
    return {
        "source": {"state": state, "method": "http_json", "recipe_version": "test-v1"},
        "events": list(events),
        "rejections": [],
    }


EVENT = {
    "title": "Social", "start": "2026-08-02T19:00:00-04:00",
    "url": "https://example.com/events/social", "venue": "Hall",
}


class CriticalSourceTests(unittest.TestCase):
    def setUp(self):
        profile = {
            "primary": {"method": "http_json", "recipe": "test-v1"},
            "fallbacks": [{"method": "browser_dom", "recipe": "dom-v1"}],
            "required_fields": ["title", "start_datetime", "url"],
            "empty_signal": "an explicit empty list",
        }
        self.catalog = {
            "retrieval_profiles": {"test": profile},
            "sources": [
                {"id": "must-have", "title": "Must", "url": "https://example.com",
                 "retrieval_profile": "test", "critical": True},
                {"id": "nice-to-have", "title": "Nice", "url": "https://example.org",
                 "retrieval_profile": "test"},
            ],
        }
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        plan = events_store.build_run_plan(
            self.catalog, [], dt.date(2026, 8, 1), run_id="critical-run")
        self.work = {row["source_id"]: row for row in plan["work"]}
        self.con = events_store.connect(os.path.join(self.temp.name, "social.db"))
        self.addCleanup(self.con.close)
        with self.con:
            events_store.persist_run_plan(self.con, plan)

    def stage(self, source_id, state, events=()):
        with self.con:
            events_store.stage_source_result(
                self.con, "critical-run", source_id, source_result(state, events))

    def finalize(self, **kwargs):
        with self.con:
            return events_store.finalize_database_run(self.con, "critical-run", **kwargs)

    def test_plan_marks_only_critical_sources(self):
        self.assertIs(self.work["must-have"]["critical"], True)
        self.assertIs(self.work["nice-to-have"]["critical"], False)

    def test_finalize_refuses_unrecovered_critical_source(self):
        self.stage("must-have", "blocked")
        self.stage("nice-to-have", "ok", [EVENT])
        with self.assertRaisesRegex(ValueError, r"must-have \(blocked\)"):
            self.finalize()
        state = self.con.execute(
            "SELECT state FROM runs WHERE id='critical-run'").fetchone()[0]
        self.assertEqual(state, "running")

    def test_fallback_recovery_lets_run_finalize(self):
        self.stage("must-have", "fetch_failed")
        self.stage("nice-to-have", "ok")
        self.stage("must-have", "ok_via_fallback", [EVENT])
        self.assertEqual(self.finalize()["new"], 1)
        self.assertEqual(
            events_store.critical_source_states(self.con, "critical-run"),
            [("must-have", "ok_via_fallback")])

    def test_non_critical_failure_does_not_block(self):
        self.stage("must-have", "empty_verified")
        self.stage("nice-to-have", "parse_failed")
        self.finalize()
        self.assertEqual(
            events_store.critical_source_failures(self.con, "critical-run"), [])

    def test_explicit_override_finalizes_with_failure(self):
        self.stage("must-have", "auth_required")
        self.stage("nice-to-have", "ok")
        self.finalize(allow_critical_failures=True)
        state = self.con.execute(
            "SELECT state FROM runs WHERE id='critical-run'").fetchone()[0]
        self.assertNotEqual(state, "running")

    def test_catalog_check_requires_boolean_flag(self):
        self.catalog["sources"][0]["critical"] = "yes"
        self.assertIn("must-have has non-boolean critical flag",
                      events_store.validate_catalog(self.catalog))


if __name__ == "__main__":
    unittest.main()
