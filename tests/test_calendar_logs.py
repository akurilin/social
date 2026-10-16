import json
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from web.calendar_coverage import REFRESH_CANCEL, run_command
from web.calendar_logs import log_path, read_logs, record, refresh_log


class CalendarLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)

    def test_rotation_bounds_history_and_keeps_private_permissions(self):
        with patch("web.calendar_logs.LOG_MAX_BYTES", 1), refresh_log(self.cache):
            for index in range(7):
                record("sample", index=index)
        rows = [json.loads(line) for line in read_logs(self.cache).splitlines()]
        self.assertEqual([row["index"] for row in rows], [3, 4, 5, 6])
        self.assertEqual(log_path(self.cache).stat().st_mode & 0o777, 0o600)

    def test_disk_failure_warns_without_stopping_work(self):
        with patch("web.calendar_logs.os.open", side_effect=OSError("private")):
            with self.assertLogs("web.calendar_logs", level="WARNING") as logs:
                with refresh_log(self.cache):
                    record("sample")
        self.assertNotIn("private", " ".join(logs.output))

    def test_cancel_context_timeout_preserves_error_signals(self):
        # Exercise the web request's polling path, including partial stderr.
        token = REFRESH_CANCEL.set(threading.Event())
        self.addCleanup(REFRESH_CANCEL.reset, token)
        with refresh_log(self.cache):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_command([sys.executable, "-c",
                             "import sys,time; print('reconnecting private', file=sys.stderr, flush=True); time.sleep(10)"],
                            timeout=0.3, capture_output=True, text=True, check=True)
        rows = [json.loads(line) for line in read_logs(self.cache).splitlines()]
        self.assertEqual(rows[-1]["event"], "command.failed")
        self.assertIn("connection", rows[-1]["signals"])
        self.assertNotIn("private", read_logs(self.cache))

    def test_real_process_reports_progress_and_safe_error_facts(self):
        # Real pipes must expose stdout-only errors before a client stalls.
        program = """
import json, sys, time
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "private-session"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "private-calendar"}}), flush=True)
print(json.dumps({"type": [], "item": {"type": {}}}), flush=True)
print(json.dumps({"type": "error", "message": "HTTP 503 server_error reconnecting request id req_12345678 private-token"}), flush=True)
print("TLS certificate verify failed private-calendar", file=sys.stderr, flush=True)
time.sleep(10)
"""
        with patch("web.calendar_logs.PROGRESS_INTERVAL_SECONDS", 0.05), refresh_log(self.cache):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_command([sys.executable, "-c", program], input="private-prompt",
                            timeout=0.35, capture_output=True, text=True, check=True)
        text = read_logs(self.cache)
        rows = [json.loads(line) for line in text.splitlines()]
        progress = next(row for row in rows if row["event"] == "command.progress")
        output = next(row for row in rows if row["event"] == "command.output")
        self.assertGreater(progress["stdout_bytes"], 0)
        self.assertEqual(output["last_client_event"], "error")
        self.assertEqual(output["client_events"], 3)
        self.assertEqual(output["http_statuses"], [503])
        self.assertEqual(output["request_ids"], ["req_12345678"])
        self.assertIn("server_error", output["failure_codes"])
        self.assertIn("tls", output["signals"])
        self.assertEqual(output["retry_messages"], 1)
        self.assertIsNotNone(output["first_output_seconds"])
        self.assertIsNotNone(output["error_fingerprint"])
        self.assertTrue(any(row["event"] == "command.terminated" for row in rows))
        self.assertEqual(rows[-1]["event"], "command.failed")
        self.assertNotIn("private-", text)

    def test_silent_timeout_reaps_descendants_after_parent_exit(self):
        # A child can keep pipes open after its parent exits; kill the full group.
        marker = self.cache / "survived"
        child = "import time; from pathlib import Path; time.sleep(0.6); Path({!r}).touch()".format(str(marker))
        program = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', {!r}])".format(child)
        with patch("web.calendar_logs.PROGRESS_INTERVAL_SECONDS", 0.05), refresh_log(self.cache):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_command([sys.executable, "-c", program], timeout=0.3,
                            capture_output=True, text=True, check=True)
        time.sleep(0.4)
        self.assertFalse(marker.exists())
        rows = [json.loads(line) for line in read_logs(self.cache).splitlines()]
        output = next(row for row in rows if row["event"] == "command.output")
        self.assertEqual(output["output_state"], "silent")
        self.assertIsNone(output["first_output_seconds"])
        self.assertTrue(any(row["event"] == "command.progress" for row in rows))

    def test_real_client_failure_and_invalid_result_have_distinct_logs(self):
        from web.calendar_coverage import refresh_coverage, client_version
        from web.repository import Repository
        client = self.cache / "codex-fixture"
        client_version.cache_clear()
        self.addCleanup(client_version.cache_clear)
        event = {"id": 0, "title": "private-calendar", "start": "2026-10-16T19:00:00-04:00",
                 "end": "2026-10-16T21:00:00-04:00"}
        cases = {
            "process_error": "print(json.dumps({'type': 'turn.failed', 'error': {'message': 'HTTP 401 invalid_api_key private-token'}})); sys.exit(1)",
            "invalid_result": "Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps({'events': []})); print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 12, 'private': 'private-token'}}))",
        }
        for name, action in cases.items():
            with self.subTest(name=name):
                root = self.cache / name
                root.mkdir()
                repository = Repository(root)
                repository.connect().close()
                client.write_text('#!' + sys.executable + "\nimport sys,json\nfrom pathlib import Path\n"
                                  "if '--version' in sys.argv:\n print('codex-cli 1.2.3'); sys.exit(0)\n"
                                  "assert '--json' in sys.argv\n"
                                  "print(json.dumps({'type': 'turn.started'}), flush=True)\n" + action + "\n")
                client.chmod(0o700)
                with patch("web.calendar_coverage.shutil.which", return_value=str(client)), patch(
                        "web.calendar_coverage.read_apple_calendar", return_value={
                            "events": [event], "timezone": "America/New_York"}):
                    result = refresh_coverage(repository)
                self.assertTrue(all(day["state"] == "unknown" for day in result["days"]))
                text = read_logs(repository.cache_path)
                rows = [json.loads(line) for line in text.splitlines()]
                configuration = next(row for row in rows if row["event"] == "client.configuration")
                self.assertEqual(configuration["version"], "1.2.3")
                if name == "process_error":
                    failed = next(row for row in rows if row["event"] == "command.failed")
                    self.assertEqual(failed["exit_code"], 1)
                    self.assertIn("invalid_api_key", failed["failure_codes"])
                else:
                    failed = next(row for row in rows if row["event"] == "classification.invalid")
                    self.assertEqual(failed["reason"], "result_count_mismatch")
                    completed = next(row for row in rows if row.get("client_event") == "turn.completed")
                    self.assertEqual(completed["usage"], {"input_tokens": 12})
                self.assertEqual(rows[-1]["event"], "refresh.failed")
                self.assertNotIn("private-", text)

    def test_noisy_client_bounds_event_records_and_skips_large_lines(self):
        from web.calendar_logs import MAX_CLIENT_EVENTS, MAX_DIAGNOSTIC_LINE_BYTES
        program = r"""
import json,sys
sys.stderr.write('private-token' * 10000 + '\n')
sys.stderr.write('HTTP 502 gateway timeout\n')
for index in range(150):
    print(json.dumps({'type': 'item.updated', 'item': {'type': 'reasoning', 'text': 'private-calendar'}}))
"""
        with refresh_log(self.cache):
            result = run_command([sys.executable, "-c", program], timeout=3,
                                 capture_output=True, text=True, check=True)
        self.assertGreater(len(result.stderr), MAX_DIAGNOSTIC_LINE_BYTES)
        text = read_logs(self.cache)
        rows = [json.loads(line) for line in text.splitlines()]
        events = [row for row in rows if row["event"] == "command.client_event"]
        self.assertEqual(len(events), MAX_CLIENT_EVENTS)
        output = next(row for row in rows if row["event"] == "command.output")
        self.assertEqual(output["client_events"], 150)
        self.assertTrue(output["client_event_limit_reached"])
        self.assertEqual(output["skipped_lines"], 1)
        self.assertEqual(output["http_statuses"], [502])
        self.assertNotIn("private-", text)

    def test_delayed_reader_receives_complete_input_across_python_versions(self):
        # Reproduce the Python 3.12 pipe stall with input larger than a pipe buffer.
        program = r"""
import sys, tempfile, threading
from pathlib import Path
from web.calendar_coverage import run_command, REFRESH_CANCEL
from web.calendar_logs import refresh_log, read_logs
payload = 'private-input-秘密' * 4000
reader = 'import sys,time; time.sleep(0.25); sys.stdout.buffer.write(sys.stdin.buffer.read())'
with tempfile.TemporaryDirectory() as folder, refresh_log(Path(folder)):
    token = REFRESH_CANCEL.set(threading.Event())
    try:
        result = run_command([sys.executable, '-c', reader], input=payload,
                             capture_output=True, text=True, check=True, timeout=3)
        assert result.stdout == payload
        result = run_command([sys.executable, '-c', reader], input='',
                             capture_output=True, text=True, check=True, timeout=3)
        assert result.stdout == ''
        assert 'private-input' not in read_logs(Path(folder))
    finally:
        REFRESH_CANCEL.reset(token)
print(sys.version.split()[0])
"""
        interpreters = dict.fromkeys(filter(None, (sys.executable, shutil.which("python3"))))
        for interpreter in interpreters:
            with self.subTest(interpreter=interpreter):
                result = subprocess.run([interpreter, "-c", program], capture_output=True,
                                        text=True, check=True, timeout=10,
                                        cwd=Path(__file__).resolve().parents[1])
                self.assertRegex(result.stdout.strip(), r"^3\.\d+\.\d+$")

    def test_context_does_not_leak_into_later_operations(self):
        with refresh_log(self.cache):
            record("sample")
        before = read_logs(self.cache)
        record("outside")
        self.assertEqual(read_logs(self.cache), before)
