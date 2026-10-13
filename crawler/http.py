"""Small, polite HTTP client for procedural event adapters."""

from __future__ import annotations

import datetime as dt
import json
from http import HTTPStatus
import time
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urlencode, urlsplit

from .contracts import FetchError, HttpResponse, RobotsDenied, RateLimited
from . import sync_log


USER_AGENT = "NYCSocialEventCrawler/1.0"
DEFAULT_MIN_INTERVAL_SECONDS = 0.25
MILLISECONDS_PER_SECOND = 1000
RETRY_DELAY_SECONDS = 0.25


class HttpClient:
    """Download public pages with robots checks, a rate limit, and one retry."""

    def __init__(self, timeout=20, min_interval_seconds=DEFAULT_MIN_INTERVAL_SECONDS,
                 user_agent=USER_AGENT):
        self.timeout = timeout
        self.min_interval_seconds = min_interval_seconds
        self.user_agent = user_agent
        self._last_request_at = 0.0
        self._robots = {}

    def get(self, url):
        self._ensure_allowed(url)
        return self._request_with_retry(url)

    def post_json(self, url, payload):
        """POST a JSON object to a public endpoint with the normal safeguards."""

        self._ensure_allowed(url)
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

    def post_form(self, url, payload):
        """Send public form data through the same robots and retry checks."""
        self._ensure_allowed(url)
        return self._request_with_retry(
            url, method="POST", body=urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def rendered_session(self):
        """Open one rate-limited browser session for rendered public pages."""

        return RenderedBrowserSession(self)

    def _request_with_retry(self, url, method="GET", body=None, headers=None):
        last_error = None
        for attempt in range(2):
            try:
                return self._request(url, method=method, body=body, headers=headers)
            except RateLimited:
                raise
            except FetchError as error:
                last_error = error
                if attempt == 0:
                    sync_log.wait(RETRY_DELAY_SECONDS, 'retry_wait')
        raise last_error

    @sync_log.request_timing('http')
    def _request(self, url, method="GET", body=None, headers=None):
        self._wait_for_slot()
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
            if error.code == HTTPStatus.TOO_MANY_REQUESTS:
                raise RateLimited("HTTP {} for {}".format(error.code, url)) from error
            raise FetchError("HTTP {} for {}".format(error.code, url)) from error
        except urllib.error.URLError as error:
            raise FetchError("could not fetch {}: {}".format(url, error.reason)) from error
        finally:
            self._mark_request()

    def _ensure_allowed(self, url):
        if not self._can_fetch(url):
            raise RobotsDenied("robots.txt does not allow {}".format(url))

    def _wait_for_slot(self):
        wait = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
        if wait > 0:
            sync_log.wait(wait, 'throttle_wait')

    def _mark_request(self):
        self._last_request_at = time.monotonic()

    def _can_fetch(self, url):
        parsed = urlsplit(url)
        origin = "{}://{}".format(parsed.scheme, parsed.netloc)
        rules = self._robots.get(origin)
        if rules is None:
            rules = self._load_robots(origin)
            self._robots[origin] = rules
        return rules.can_fetch(self.user_agent, url)

    @sync_log.request_timing('robots')
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


class RenderedBrowserSession:
    """Render JavaScript pages while preserving the HTTP client's safeguards."""

    def __init__(self, client):
        self.client = client
        self.playwright = None
        self.browser = None
        self.page = None

    def __enter__(self):
        # Import lazily so plain HTTP crawls do not require a browser process.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise FetchError(
                "Playwright is not installed; install project requirements"
            ) from error
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=True)
            self.page = self.browser.new_page()
        except Exception as error:
            self.close()
            raise FetchError("could not start the rendered browser: {}".format(error)) from error
        return self

    def __exit__(self, _error_type, _error, _traceback):
        self.close()

    def close(self):
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        if self.playwright is not None:
            self.playwright.stop()
            self.playwright = None
        self.page = None

    @sync_log.request_timing('browser')
    def get(self, url, ready_selector):
        """Return rendered HTML after the page exposes its ready marker."""

        self.client._ensure_allowed(url)
        if self.page is None:
            raise FetchError("rendered browser session is not open")
        timeout_ms = int(self.client.timeout * MILLISECONDS_PER_SECOND)
        self.client._wait_for_slot()
        try:
            response = self.page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_ms)
            if response is None:
                raise FetchError("rendered navigation returned no response for {}".format(url))
            if response.status >= 400:
                raise FetchError("HTTP {} for {}".format(response.status, url))
            self.page.locator(ready_selector).first.wait_for(
                state="attached", timeout=timeout_ms)
            body = self.page.content().encode("utf-8")
            headers = {key.lower(): value for key, value in response.all_headers().items()}
            headers.setdefault("content-type", "text/html; charset=utf-8")
            return HttpResponse(
                url=self.page.url,
                status=response.status,
                body=body,
                headers=headers,
                fetched_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        except FetchError:
            raise
        except Exception as error:
            raise FetchError("could not render {}: {}".format(url, error)) from error
        finally:
            self.client._mark_request()
