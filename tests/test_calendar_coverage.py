import datetime as dt
import json
import subprocess
import tempfile
import threading
import sys
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from web.app import app
from web.repository import Repository
from web.calendar_coverage import (
    COVERAGE_DAYS, MODEL, CalendarCoverageError, classify_events,
    coverage_days, read_apple_calendar, refresh_coverage,
)


TODAY = dt.date(2026, 12, 28)
SONNET_ALIAS = "sonnet"


def calendar_event(event_id=0, start="2026-12-28T19:00:00-05:00",
                   end="2026-12-28T21:00:00-05:00", **extra):
    return dict(id=event_id, title="Book club", start=start, end=end,
                all_day=False, **extra)


def decision(event_id=0, social=True, online=False, professional=False):
    return dict(id=event_id, is_social=social, is_online=online,
                is_professional=professional)


class CalendarCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = Repository(self.root)
        self.repo.connect().close()
        # Keep unit tests independent of installed command-line clients.
        available = patch("web.calendar_coverage.shutil.which",
                          side_effect=lambda name: "/bin/" + name)
        available.start()
        self.addCleanup(available.stop)

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self, events, decisions):
        with patch("web.calendar_coverage.read_apple_calendar", return_value={
                "timezone": "America/New_York", "events": events}), patch(
                "web.calendar_coverage.classify_events", return_value=decisions):
            return refresh_coverage(self.repo, TODAY)

    def log_rows(self):
        path = self.repo.cache_path / "logs" / "calendar-coverage.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_refresh_logs_stages_without_calendar_contents(self):
        self.refresh([calendar_event()], [decision()])
        rows = self.log_rows()
        self.assertEqual(rows[0]["event"], "refresh.started")
        self.assertEqual(rows[-1]["event"], "refresh.completed")
        self.assertEqual(len({row["run_id"] for row in rows}), 1)
        model = next(row for row in rows if row["event"] == "classification.started")
        self.assertEqual(model["model"], MODEL)
        self.assertEqual(model["event_count"], 1)
        self.assertNotIn("Book club", json.dumps(rows))

    def test_timeout_logs_cause_and_keeps_history(self):
        self.refresh([], [])
        with patch("web.calendar_coverage.read_apple_calendar", return_value={
                "timezone": "America/New_York", "events": [calendar_event()]}), patch(
                "web.calendar_coverage._run_command",
                side_effect=subprocess.TimeoutExpired("private command", 180,
                    stderr=b"private calendar data: reconnecting stream")):
            refresh_coverage(self.repo, TODAY)
        rows = self.log_rows()
        self.assertEqual(len({row["run_id"] for row in rows}), 2)
        failed = next(row for row in rows if row["event"] == "command.failed")
        self.assertEqual(failed["error_type"], "TimeoutExpired")
        self.assertEqual(failed["timeout_seconds"], 180)
        self.assertIn("connection", failed["signals"])
        self.assertEqual(rows[-1]["event"], "refresh.failed")
        self.assertNotIn("private", json.dumps(rows))

    def test_window_crosses_year_and_includes_empty_days(self):
        result = self.refresh([calendar_event()], [decision()])
        self.assertEqual(len(result["days"]), COVERAGE_DAYS)
        self.assertEqual(result["days"][0]["date"], "2026-12-28")
        self.assertEqual(result["days"][-1]["date"], "2027-01-10")
        self.assertEqual(result["days"][0]["state"], "planned")
        self.assertTrue(all(day["state"] == "open" for day in result["days"][1:]))
        self.assertEqual(self.repo.load_calendar_coverage()["model"], MODEL)
        self.assertNotIn("Book club", json.dumps(self.repo.load_calendar_coverage()))

    def test_online_and_work_flags_override_social_flag(self):
        result = self.refresh(
            [calendar_event(0), calendar_event(1)],
            [decision(0, online=True), decision(1, professional=True)])
        self.assertEqual(result["days"][0]["state"], "open")

    def test_midnight_end_is_exclusive_and_dates_use_local_zone(self):
        events = [calendar_event(start="2026-12-29T01:00:00Z",
                                 end="2026-12-29T05:00:00Z")]
        result = self.refresh(events, [decision()])
        self.assertEqual(result["days"][0]["state"], "planned")
        self.assertEqual(result["days"][1]["state"], "open")

    def test_multiday_event_and_window_edges(self):
        events = [calendar_event(start="2026-12-27T23:00:00-05:00",
                                 end="2026-12-30T00:00:00-05:00")]
        result = self.refresh(events, [decision()])
        self.assertEqual([d["state"] for d in result["days"][:3]],
                         ["planned", "planned", "open"])

    def test_all_day_end_and_outer_window_are_exclusive(self):
        event = calendar_event(start="2026-12-28T00:00:00-05:00",
                               end="2026-12-29T00:00:00-05:00")
        event["all_day"] = True
        outside = calendar_event(1, start="2027-01-11T00:00:00-05:00",
                                  end="2027-01-12T00:00:00-05:00")
        result = self.refresh([event, outside], [decision(), decision(1)])
        self.assertEqual([d["date"] for d in result["days"] if d["state"] == "planned"],
                         [TODAY.isoformat()])

    def test_dst_uses_each_local_midnight(self):
        events = [calendar_event(start="2026-11-01T00:30:00-04:00",
                                 end="2026-11-02T00:00:00-05:00")]
        days = coverage_days(events, [decision()], dt.date(2026, 11, 1),
                             "America/New_York")
        self.assertEqual([d["state"] for d in days[:2]], ["planned", "open"])

    def test_empty_calendar_is_red_without_model_call(self):
        with patch("web.calendar_coverage.read_apple_calendar", return_value={
                "timezone": "America/New_York", "events": []}), patch(
                "web.calendar_coverage.classify_events") as classify:
            result = refresh_coverage(self.repo, TODAY)
        classify.assert_not_called()
        self.assertTrue(all(day["state"] == "open" for day in result["days"]))

    def test_failed_refresh_clears_prior_colors(self):
        self.refresh([calendar_event()], [decision()])
        for message in ("Calendar access denied.", "Luna check failed."):
            with self.subTest(message=message), patch(
                    "web.calendar_coverage.read_apple_calendar",
                    side_effect=CalendarCoverageError(message)):
                result = refresh_coverage(self.repo, TODAY)
            self.assertEqual(result["message"], message)
            self.assertTrue(all(d["state"] == "unknown" for d in result["days"]))

    def test_model_failure_does_not_mean_empty_calendar(self):
        with patch("web.calendar_coverage.read_apple_calendar", return_value={
                "timezone": "America/New_York", "events": [calendar_event()]}), patch(
                "web.calendar_coverage.classify_events",
                side_effect=CalendarCoverageError("Luna check failed.")):
            result = refresh_coverage(self.repo, TODAY)
        self.assertTrue(all(d["state"] == "unknown" for d in result["days"]))

    def test_new_days_remain_unknown_and_saved_days_survive_restart(self):
        self.refresh([calendar_event()], [decision()])
        fresh_repo = Repository(self.root)
        result = fresh_repo.calendar_coverage(TODAY + dt.timedelta(days=1))
        self.assertEqual(result["days"][0]["state"], "open")
        self.assertEqual(result["days"][-1]["state"], "unknown")

    def test_no_saved_check_means_blue(self):
        result = self.repo.calendar_coverage(TODAY)
        self.assertEqual(len(result["days"]), COVERAGE_DAYS)
        self.assertTrue(all(d["state"] == "unknown" for d in result["days"]))

    def test_non_mac_does_not_start_calendar_process(self):
        with patch("web.calendar_coverage.sys.platform", "linux"), patch(
                "web.calendar_coverage._run_command") as run:
            with self.assertRaises(CalendarCoverageError):
                read_apple_calendar(self.repo.cache_path, TODAY)
        run.assert_not_called()

    def test_native_denial_and_no_calendars_are_unavailable(self):
        for status in ("denied", "no_calendars", "error"):
            with self.subTest(status=status), patch(
                    "web.calendar_coverage.sys.platform", "darwin"), patch(
                    "web.calendar_coverage.build_calendar_reader", return_value=Path("reader")), patch(
                    "web.calendar_coverage._run_command", return_value=subprocess.CompletedProcess(
                        [], 0, stdout=json.dumps({"status": status}))):
                with self.assertRaises(CalendarCoverageError):
                    read_apple_calendar(self.repo.cache_path, TODAY)

    def test_classifier_validates_complete_typed_output(self):
        invalid = [[], [decision(9)], [decision(), decision()],
                   [dict(decision(), is_social="true")]]
        for rows in invalid:
            with self.subTest(rows=rows), patch(
                    "web.calendar_coverage.run_luna", return_value={"events": rows}):
                with self.assertRaises(CalendarCoverageError):
                    classify_events([calendar_event()])

    def test_classifier_timeout_has_safe_error(self):
        with patch("web.calendar_coverage.shutil.which", return_value="/bin/codex"), patch(
                "web.calendar_coverage._run_command",
                side_effect=subprocess.TimeoutExpired("codex", 1)):
            with self.assertRaisesRegex(CalendarCoverageError, "timed out"):
                classify_events([calendar_event()])

    def test_claude_backup_uses_schema_and_records_its_model(self):
        payload = {"is_error": False, "subtype": "success",
                   "structured_output": {"events": [decision()]}}
        with patch("web.calendar_coverage.shutil.which", side_effect=lambda name:
                   "/bin/claude" if name == "claude" else None), patch(
                "web.calendar_coverage.read_apple_calendar", return_value={
                    "timezone": "America/New_York", "events": [calendar_event()]}), patch(
                "web.calendar_coverage._run_command", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(payload))) as run:
            result = refresh_coverage(self.repo, TODAY)

        self.assertEqual(result["days"][0]["state"], "planned")
        self.assertEqual(self.repo.load_calendar_coverage()["model"], SONNET_ALIAS)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/bin/claude")
        self.assertEqual(command[command.index("--model") + 1], SONNET_ALIAS)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--safe-mode", command)
        self.assertIn("--no-session-persistence", command)
        self.assertNotIn("--bare", command)
        self.assertIn("Book club", run.call_args.kwargs["input"])
        self.assertFalse(run.call_args.kwargs["cwd"].exists())

    def test_codex_remains_first_choice(self):
        with patch("web.calendar_coverage.run_luna", return_value={
                "events": [decision()]}) as luna, patch(
                "web.calendar_coverage._run_command") as run:
            self.assertEqual(classify_events([calendar_event()]), [decision()])
        luna.assert_called_once()
        run.assert_not_called()

    def test_missing_clients_return_blue_with_setup_message(self):
        with patch("web.calendar_coverage.shutil.which", return_value=None), patch(
                "web.calendar_coverage.read_apple_calendar", return_value={
                    "timezone": "America/New_York", "events": [calendar_event()]}):
            result = refresh_coverage(self.repo, TODAY)
        self.assertTrue(all(day["state"] == "unknown" for day in result["days"]))
        self.assertIn("Codex", result["message"])
        self.assertIn("Claude", result["message"])
        self.assertIsNone(self.repo.load_calendar_coverage()["model"])

    def test_claude_rejects_errors_and_incomplete_output(self):
        outputs = ["not JSON", "null", "[]", "{}",
                   json.dumps([{"type": "assistant", "subtype": "success", "is_error": False,
                                "structured_output": {"events": [decision()]}}]),
                   json.dumps({"is_error": True, "subtype": "success",
                               "structured_output": {"events": [decision()]}}),
                   json.dumps({"is_error": False, "subtype": "error_max_turns",
                               "structured_output": {"events": [decision()]}}),
                   json.dumps({"is_error": False, "subtype": "success",
                               "structured_output": {"events": []}}),
                   json.dumps({"is_error": False, "subtype": "success",
                               "structured_output": {"events": [decision(9)]}})]
        for output in outputs:
            with self.subTest(output=output), patch(
                    "web.calendar_coverage.shutil.which", side_effect=lambda name:
                    "/bin/claude" if name == "claude" else None), patch(
                    "web.calendar_coverage._run_command", return_value=subprocess.CompletedProcess(
                        [], 0, stdout=output)):
                with self.assertRaises(CalendarCoverageError):
                    classify_events([calendar_event()])

    def test_claude_accepts_verbose_json_result_envelope(self):
        output = [{"type": "system", "subtype": "init"},
                  {"type": "assistant", "message": {}},
                  {"type": "result", "subtype": "success", "is_error": False,
                   "structured_output": {"events": [decision()]}}]
        with patch("web.calendar_coverage.shutil.which", side_effect=lambda name:
                   "/bin/claude" if name == "claude" else None), patch(
                "web.calendar_coverage._run_command", return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(output))):
            self.assertEqual(classify_events([calendar_event()]), [decision()])

    def test_claude_timeout_does_not_reveal_calendar_contents(self):
        with patch("web.calendar_coverage.shutil.which", side_effect=lambda name:
                   "/bin/claude" if name == "claude" else None), patch(
                "web.calendar_coverage._run_command", side_effect=subprocess.TimeoutExpired(
                    "private calendar data", 1)):
            with self.assertRaisesRegex(CalendarCoverageError, "Sonnet check timed out"):
                classify_events([calendar_event()])

    def test_empty_calendar_needs_no_client(self):
        with patch("web.calendar_coverage.shutil.which", return_value=None), patch(
                "web.calendar_coverage.read_apple_calendar", return_value={
                    "timezone": "America/New_York", "events": []}):
            result = refresh_coverage(self.repo, TODAY)
        self.assertTrue(all(day["state"] == "open" for day in result["days"]))
        self.assertIsNone(self.repo.load_calendar_coverage()["model"])

    def test_schema_upgrade_keeps_crawl_data(self):
        with self.repo.connect() as con:
            con.execute("DROP TABLE calendar_coverage")
            con.execute("PRAGMA user_version = 6")
        from web.repository import events_store
        events_store.migrate_database(self.repo.db_path)
        self.assertIsNone(self.repo.load_calendar_coverage())


class CalendarCoveragePageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repository(self.temp.name)
        self.repo.connect().close()
        self.repo.catalog_path.write_text('{"sources": []}')
        self.original_repo = app.state.repo
        app.state.repo = self.repo
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.state.repo = self.original_repo
        self.temp.cleanup()

    def test_both_pages_show_all_days_and_only_manual_refresh(self):
        with patch("web.calendar_coverage.read_apple_calendar") as read:
            for path in ("/events", "/calendar"):
                page = self.client.get(path)
                self.assertEqual(page.status_code, 200)
                self.assertIn("Refresh calendar", page.text)
                self.assertEqual(page.text.count('class="coverage-date"'), COVERAGE_DAYS)
                self.assertIn("coverage-unknown", page.text)
        read.assert_not_called()

    def test_logs_are_accessible_from_calendar(self):
        response = self.client.get("/calendar-coverage/logs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No calendar checks logged", response.text)
        with patch("web.calendar_coverage.read_apple_calendar", return_value={
                "timezone": "America/New_York", "events": []}):
            self.client.post("/calendar-coverage/refresh")
        response = self.client.get("/calendar-coverage/logs")
        self.assertIn("refresh.completed", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn('/calendar-coverage/logs', self.client.get("/calendar").text)

    def test_refresh_persists_status_for_both_pages(self):
        with patch("web.calendar_coverage.read_apple_calendar", return_value={
                "timezone": "America/New_York", "events": []}):
            response = self.client.post("/calendar-coverage/refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["days"][0]["state"], "open")
        for path in ("/events", "/calendar"):
            document = BeautifulSoup(self.client.get(path).text, "html.parser")
            self.assertEqual(len(document.select(".coverage-tile.coverage-open")),
                             COVERAGE_DAYS)
            if path == "/calendar":
                self.assertIsNotNone(document.select_one(
                    '.calendar-day.coverage-open[data-date="{}"]'.format(dt.date.today())))

    def test_parallel_refresh_does_not_run_another_check(self):
        with self.repo.calendar_refresh_lock, patch(
                "web.calendar_coverage.read_apple_calendar") as read:
            response = self.client.post("/calendar-coverage/refresh")
        self.assertEqual(response.status_code, 409)
        read.assert_not_called()

    def test_stop_refresh_keeps_saved_result_and_allows_retry(self):
        started = threading.Event()
        previous = self.repo.calendar_coverage()

        processes = []
        popen = subprocess.Popen

        def start_process(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            started.set()
            return process

        def slow_read(*args):
            from web.calendar_coverage import run_command
            run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                        capture_output=True, text=True, check=True, timeout=40)

        with patch("web.calendar_coverage.read_apple_calendar", side_effect=slow_read), patch(
                "web.calendar_coverage.subprocess.Popen", side_effect=start_process):
            with ThreadPoolExecutor() as pool:
                refresh = pool.submit(self.client.post, "/calendar-coverage/refresh")
                self.assertTrue(started.wait(5))
                stopped = self.client.post("/calendar-coverage/stop")
                self.assertEqual(stopped.status_code, 200)
                result = refresh.result(timeout=5)
        self.assertTrue(result.json()["stopped"])
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(self.repo.calendar_coverage(), previous)
        from web.calendar_logs import read_logs
        self.assertIn("refresh.stopped", read_logs(self.repo.cache_path))
        with patch("web.calendar_coverage.read_apple_calendar", return_value={
                "timezone": "America/New_York", "events": []}):
            self.assertEqual(self.client.post("/calendar-coverage/refresh").status_code, 200)

    def test_stop_rejects_cross_site_requests(self):
        response = self.client.post("/calendar-coverage/stop", headers={
            "Origin": "https://example.com", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)

    def test_refresh_rejects_cross_site_requests(self):
        with patch("web.calendar_coverage.read_apple_calendar") as read:
            response = self.client.post("/calendar-coverage/refresh", headers={
                "Origin": "https://example.com", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        read.assert_not_called()
