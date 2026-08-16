"""Small, polite HTTP client for procedural event adapters."""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urlsplit

from .contracts import FetchError, HttpResponse, RobotsDenied


USER_AGENT = "NYCSocialEventCrawler/1.0"


class HttpClient:
    """Download public pages with robots checks, a rate limit, and one retry."""

    def __init__(self, timeout=20, min_interval_seconds=0.25, user_agent=USER_AGENT):
        self.timeout = timeout
        self.min_interval_seconds = min_interval_seconds
        self.user_agent = user_agent
        self._last_request_at = 0.0
        self._robots = {}

    def get(self, url):
        if not self._can_fetch(url):
            raise RobotsDenied("robots.txt does not allow {}".format(url))
        return self._request_with_retry(url)

    def post_json(self, url, payload):
        """POST a JSON object to a public endpoint with the normal safeguards."""

        if not self._can_fetch(url):
            raise RobotsDenied("robots.txt does not allow {}".format(url))
        try:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise FetchError("could not encode JSON request for {}".format(url)) from error
        return self._request_with_retry(
            url,
            method="POST",
            body=body,
            headers={"Content-Type": "application/json"},
        )

    def _request_with_retry(self, url, method="GET", body=None, headers=None):
        last_error = None
        for attempt in range(2):
            try:
                return self._request(url, method=method, body=body, headers=headers)
            except FetchError as error:
                last_error = error
                if attempt == 0:
                    time.sleep(0.25)
        raise last_error

    def _request(self, url, method="GET", body=None, headers=None):
        wait = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "identity",
        }
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                headers = {key.lower(): value for key, value in response.headers.items()}
                return HttpResponse(
                    url=response.geturl(),
                    status=response.status,
                    body=body,
                    headers=headers,
                    fetched_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                )
        except urllib.error.HTTPError as error:
            raise FetchError("HTTP {} for {}".format(error.code, url)) from error
        except urllib.error.URLError as error:
            raise FetchError("could not fetch {}: {}".format(url, error.reason)) from error
        finally:
            self._last_request_at = time.monotonic()

    def _can_fetch(self, url):
        parsed = urlsplit(url)
        origin = "{}://{}".format(parsed.scheme, parsed.netloc)
        rules = self._robots.get(origin)
        if rules is None:
            rules = self._load_robots(origin)
            self._robots[origin] = rules
        return rules.can_fetch(self.user_agent, url)

    def _load_robots(self, origin):
        robots_url = origin + "/robots.txt"
        request = urllib.request.Request(robots_url, headers={"User-Agent": self.user_agent})
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                parser.parse(["User-agent: *", "Disallow: /"])
                return parser
            if 400 <= error.code < 500:
                parser.parse([])
                return parser
            raise FetchError("could not fetch {}: HTTP {}".format(
                robots_url, error.code)) from error
        except urllib.error.URLError as error:
            raise FetchError("could not fetch {}: {}".format(
                robots_url, error.reason)) from error
        parser.parse(body.splitlines())
        return parser
