import json
import subprocess
import sys
import tempfile
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

    def test_context_does_not_leak_into_later_operations(self):
        with refresh_log(self.cache):
            record("sample")
        before = read_logs(self.cache)
        record("outside")
        self.assertEqual(read_logs(self.cache), before)
