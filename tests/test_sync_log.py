import concurrent.futures
from http import HTTPStatus
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

from crawler import sync_log
from crawler.contracts import RateLimited
from crawler.http import HttpClient


class SyncLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.dict(os.environ, {sync_log.LOG_DIRECTORY_ENV: self.temp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def test_macos_uses_user_library_logs(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sync_log.sys, 'platform', 'darwin'):
            self.assertEqual(sync_log.log_directory(), Path.home() / 'Library' / 'Logs' / 'SocialCrawler')

    def test_parallel_sources_keep_separate_metrics(self):
        client = HttpClient(min_interval_seconds=0)
        url = 'https://example.test/private?token=secret'
        error = HTTPError(url, HTTPStatus.TOO_MANY_REQUESTS, 'secret', {}, None)

        def retrieve(source):
            with sync_log.span('source', run_id='parallel', source_id=source):
                with self.assertRaises(RateLimited):
                    client._request(url)

        with mock.patch('urllib.request.urlopen', side_effect=error):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(retrieve, ['alpha', 'beta']))
        rows = sync_log.read_records('parallel')
        completed = [row for row in rows if row['event'] == 'source.completed']
        self.assertEqual({row['source_id'] for row in completed}, {'alpha', 'beta'})
        self.assertTrue(all(row['http_count'] == 1 for row in completed))
        self.assertEqual(len({row['attempt_id'] for row in completed}), 2)
        content = sync_log.log_path('parallel').read_text()
        self.assertNotIn('secret', content)
        self.assertNotIn('private', content)
        self.assertIn('RateLimited', content)
        self.assertIsNone(sync_log.ACTIVE.get())

    def test_span_uses_monotonic_duration_and_logs_failure(self):
        with mock.patch.object(sync_log.time, 'perf_counter', side_effect=[10.0, 12.5]):
            with self.assertRaisesRegex(ValueError, 'private message'):
                with sync_log.span('source', run_id='failed', source_id='alpha'):
                    raise ValueError('private message')
        row = sync_log.read_records('failed')[-1]
        self.assertEqual(row['event'], 'source.failed')
        self.assertEqual(row['duration_seconds'], 2.5)
        self.assertEqual(row['error_type'], 'ValueError')
        self.assertNotIn('private message', json.dumps(row))

    def test_rotation_is_bounded_and_retains_valid_json_lines(self):
        with mock.patch.object(sync_log, 'LOG_MAX_BYTES', 1):
            for index in range(8):
                sync_log.record('sample', run_id='rotate', index=index)
        self.assertEqual(len(list(Path(self.temp.name).glob('*.jsonl*'))), sync_log.LOG_BACKUPS + 1)
        self.assertEqual([row['index'] for row in sync_log.read_records('rotate')], [4, 5, 6, 7])
        self.assertEqual(sync_log.log_path('rotate').stat().st_mode & 0o777, 0o600)

    def test_disk_error_does_not_stop_source(self):
        with mock.patch.object(sync_log.os, 'open', side_effect=OSError('full disk')):
            with self.assertLogs('crawler.sync_log', level='WARNING'):
                with sync_log.span('source', run_id='disk', source_id='alpha'):
                    pass

    def test_run_id_cannot_escape_log_directory(self):
        with self.assertRaises(ValueError):
            sync_log.log_path('../escape')


if __name__ == '__main__':
    unittest.main()
