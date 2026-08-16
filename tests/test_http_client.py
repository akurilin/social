import unittest
from unittest.mock import patch

from crawler.contracts import HttpResponse, RobotsDenied
from crawler.http import HttpClient


class HttpClientTests(unittest.TestCase):
    def test_post_json_uses_the_normal_guard_and_request_path(self):
        client = HttpClient()
        response = HttpResponse(
            url="https://example.com/api/events", status=200, body=b"{}",
            headers={"content-type": "application/json"},
            fetched_at="2026-08-16T12:00:00-04:00",
        )
        with patch.object(client, "_can_fetch", return_value=True) as can_fetch, \
                patch.object(client, "_request_with_retry", return_value=response) as request:
            self.assertIs(client.post_json("https://example.com/api/events", {"page": 1}), response)
        can_fetch.assert_called_once_with("https://example.com/api/events")
        args, kwargs = request.call_args
        self.assertEqual(args, ("https://example.com/api/events",))
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["body"], b'{"page":1}')
        self.assertEqual(kwargs["headers"], {"Content-Type": "application/json"})

    def test_post_json_respects_robots_denial(self):
        client = HttpClient()
        with patch.object(client, "_can_fetch", return_value=False):
            with self.assertRaises(RobotsDenied):
                client.post_json("https://example.com/api/events", {"page": 1})


if __name__ == "__main__":
    unittest.main()
