import unittest
from unittest.mock import patch

from crawler.contracts import HttpResponse, RobotsDenied, RateLimited
from crawler.http import HttpClient, RenderedBrowserSession


class HttpClientTests(unittest.TestCase):
    def test_rate_limit_is_not_retried(self):
        client = HttpClient()
        with patch.object(client, "_request", side_effect=RateLimited("HTTP 429")) as request:
            with self.assertRaises(RateLimited):
                client._request_with_retry("https://example.com/events")
        request.assert_called_once()

    def test_http_429_has_a_typed_error(self):
        from http import HTTPStatus
        from urllib.error import HTTPError
        client = HttpClient(min_interval_seconds=0)
        url = "https://example.com/events"
        error = HTTPError(url, HTTPStatus.TOO_MANY_REQUESTS, "Rate limited", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RateLimited):
                client._request(url)

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

    def test_rendered_get_respects_robots_denial(self):
        client = HttpClient()
        session = RenderedBrowserSession(client)
        session.page = object()
        with patch.object(client, "_can_fetch", return_value=False):
            with self.assertRaises(RobotsDenied):
                session.get("https://example.com/events", ".event")


if __name__ == "__main__":
    unittest.main()
