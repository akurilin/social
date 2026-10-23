"""Browser checks for filter state across page loads and navigation."""

import datetime as dt
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import events_store
from web.app import app
from web.repository import Repository
import uvicorn


SERVER_START_TIMEOUT_SECONDS = 10
SERVER_POLL_INTERVAL_SECONDS = 0.05


class FilterPreferencesBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        (cls.root / ".cache").mkdir()
        (cls.root / "EVENT_RANKING_CRITERIA.example.md").write_text(
            "# Example\n", encoding="utf-8")
        (cls.root / ".agents/skills/sync-events").mkdir(parents=True)
        (cls.root / ".agents/skills/sync-events/SKILL.md").write_text(
            "# Test\n", encoding="utf-8")
        catalog = {
            "retrieval_profiles": {},
            "sources": [
                {"id": "source-one", "title": "Source One", "url": "https://one.example"},
                {"id": "source-two", "title": "Source Two", "url": "https://two.example"},
            ],
            "inbox_sources": {"items": []},
        }
        (cls.root / "sources.json").write_text(
            json.dumps(catalog), encoding="utf-8")

        cls.database = cls.root / "social.db"
        con = events_store.connect(str(cls.database))
        today = dt.date.today()
        cls.month = today.strftime("%Y-%m")
        events = [
            {
                "source_id": "source-one", "title": "Dinner event",
                "start": (today + dt.timedelta(days=1)).isoformat() + "T19:00:00-04:00",
                "url": "https://one.example/dinner", "rank": "super",
            },
            {
                "source_id": "source-two", "title": "Zine workshop",
                "start": (today + dt.timedelta(days=5)).isoformat() + "T18:00:00-04:00",
                "url": "https://two.example/zine", "rank": "low",
            },
        ]
        with con:
            for item in events:
                events_store.upsert_event(
                    con, events_store.normalise_incoming(item, today.isoformat()))
        con.close()

        cls.original_repo = app.state.repo
        app.state.repo = Repository(cls.root, cls.database)
        cls.socket = socket.socket()
        cls.socket.bind(("127.0.0.1", 0))
        cls.socket.listen(128)
        cls.base_url = "http://127.0.0.1:{}".format(cls.socket.getsockname()[1])
        cls.server = uvicorn.Server(uvicorn.Config(
            app, log_level="error", lifespan="off"))
        cls.server_thread = threading.Thread(
            target=cls.server.run, kwargs={"sockets": [cls.socket]}, daemon=True)
        cls.server_thread.start()
        deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.socket.getsockname()[1]), timeout=0.2):
                    break
            except OSError:
                time.sleep(SERVER_POLL_INTERVAL_SECONDS)
        else:
            raise RuntimeError("The test web server did not start")

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.server_thread.join(timeout=SERVER_START_TIMEOUT_SECONDS)
        cls.socket.close()
        app.state.repo = cls.original_repo
        cls.temp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)

    def tearDown(self):
        self.context.close()

    def open(self, path):
        response = self.page.goto(self.base_url + path, wait_until="networkidle")
        self.assertEqual(HTTPStatus.OK, response.status, self.page.locator("body").inner_text())

    def checked_sources(self):
        return self.page.locator(
            'form [name="source_id"]:checked').evaluate_all(
                "items => items.map(item => item.value)")

    def assert_reset_fits_form(self, width):
        self.page.set_viewport_size({"width": width, "height": 900})
        bounds = self.page.locator("[data-reset-filters]").evaluate("""element => {
          const linkBounds = element.getBoundingClientRect();
          const form = element.closest('form').getBoundingClientRect();
          return {linkLeft: linkBounds.left, linkRight: linkBounds.right,
                  formLeft: form.left, formRight: form.right};
        }""")
        self.assertGreaterEqual(bounds["linkLeft"], bounds["formLeft"])
        self.assertLessEqual(bounds["linkRight"], bounds["formRight"])

    def test_filters_survive_bare_loads_and_switch_pages_independently(self):
        self.open("/events")
        self.assert_reset_fits_form(1200)
        self.assert_reset_fits_form(390)
        self.page.locator('form.event-filters [name="q"]').fill("Dinner")
        self.page.locator('form.event-filters [name="q"]').press("Enter")
        self.page.wait_for_url("**/events?*")
        self.page.locator('form.event-filters [name="rank"]').select_option("super")
        self.page.wait_for_url("**rank=super**")
        self.page.locator("[data-source-filter] summary").click()
        self.page.locator("[data-source-clear-all]").click()
        self.page.wait_for_url("**sources_filtered=1**")
        self.assertEqual([], self.checked_sources())
        self.page.locator("[data-source-filter] summary").click()
        self.page.locator(
            'form.event-filters [name="source_id"][value="source-two"]').check()
        self.page.wait_for_url("**source_id=source-two**")
        self.assertEqual(["source-two"], self.checked_sources())

        # A new tab restores this page's saved search and source selection.
        other_tab = self.context.new_page()
        other_tab.goto(self.base_url + "/events", wait_until="networkidle")
        self.assertEqual("Dinner", other_tab.locator('form.event-filters [name="q"]').input_value())
        self.assertEqual("super", other_tab.locator('form.event-filters [name="rank"]').input_value())
        self.assertEqual(["source-two"], other_tab.locator(
            'form.event-filters [name="source_id"]:checked').evaluate_all(
                "items => items.map(item => item.value)"))
        other_tab.close()

        # Select all sources to check the canonical unfiltered state.
        self.page.locator("[data-source-filter] summary").click()
        self.page.locator("[data-source-select-all]").click()
        self.assertEqual({"source-one", "source-two"}, set(self.checked_sources()))

        # A bare load restores the full saved state, including all sources.
        self.open("/events")
        self.assertEqual("Dinner", self.page.locator('form.event-filters [name="q"]').input_value())
        self.assertEqual("super", self.page.locator('form.event-filters [name="rank"]').input_value())
        self.assertEqual({"source-one", "source-two"}, set(self.checked_sources()))

        # Calendar has its own saved state. Set a month and return to its bare URL.
        self.page.locator('[aria-label="Event view"] a:has-text("Calendar")').click()
        self.page.wait_for_url("**/calendar**")
        self.assertEqual(self.month, self.page.locator('form.calendar-filters [name="month"]').input_value())
        self.page.locator('[aria-label="Next month"]').click()
        next_month = self.page.locator('form.calendar-filters [name="month"]').input_value()
        self.open("/calendar")
        self.assertEqual(next_month, self.page.locator('form.calendar-filters [name="month"]').input_value())

        # Switching back uses the event page's independent saved filters.
        self.page.locator('[aria-label="Event view"] a:has-text("List")').click()
        self.page.wait_for_url("**/events**")
        self.assertEqual("Dinner", self.page.locator('form.event-filters [name="q"]').input_value())
        self.assertEqual("super", self.page.locator('form.event-filters [name="rank"]').input_value())
        self.assertEqual({"source-one", "source-two"}, set(self.checked_sources()))

    def test_explicit_query_replaces_saved_state_and_reset_clears_only_current_page(self):
        self.open("/events?q=Dinner&rank=super&period=all&period=upcoming&sort=title&dir=desc")
        self.assertEqual("Dinner", self.page.locator('form.event-filters [name="q"]').input_value())
        self.assertEqual("title", self.page.locator('form.event-filters [name="sort"]').input_value())
        self.assertEqual("desc", self.page.locator('form.event-filters [name="dir"]').input_value())
        self.assertTrue(self.page.locator('input[name="period"][value="upcoming"]').is_checked())
        self.open("/calendar?month={}&rank=low".format(self.month))
        self.page.locator('[aria-label="Next month"]').click()
        saved_month = self.page.locator('form.calendar-filters [name="month"]').input_value()

        # Explicit query state replaces every saved event filter.
        self.open("/events?rank=unranked")
        self.assertEqual("", self.page.locator('form.event-filters [name="q"]').input_value())
        self.assertEqual("unranked", self.page.locator('form.event-filters [name="rank"]').input_value())
        self.assertEqual("start", self.page.locator('form.event-filters [name="sort"]').input_value())
        self.assertEqual("asc", self.page.locator('form.event-filters [name="dir"]').input_value())
        self.assertTrue(self.page.locator('input[name="period"][value="upcoming"]').is_checked())
        self.open("/events")
        self.assertEqual("unranked", self.page.locator('form.event-filters [name="rank"]').input_value())

        self.open("/calendar")
        self.assertEqual(saved_month, self.page.locator('form.calendar-filters [name="month"]').input_value())
        self.page.locator('[data-reset-filters]').click()
        self.page.wait_for_url(self.base_url + "/calendar")
        self.assertEqual(dt.date.today().strftime("%Y-%m"), self.page.locator(
            'form.calendar-filters [name="month"]').input_value())
        self.assertEqual("", self.page.locator('form.calendar-filters [name="rank"]').input_value())
        self.assertEqual({"source-one", "source-two"}, set(self.checked_sources()))
        self.open("/events")
        self.assertEqual("unranked", self.page.locator('form.event-filters [name="rank"]').input_value())
        self.page.locator('[data-reset-filters]').click()
        self.page.wait_for_url(self.base_url + "/events")
        self.assertEqual("", self.page.locator('form.event-filters [name="q"]').input_value())
        self.assertEqual("", self.page.locator('form.event-filters [name="rank"]').input_value())
        self.assertTrue(self.page.locator('input[name="period"][value="upcoming"]').is_checked())
        self.assertEqual({"source-one", "source-two"}, set(self.checked_sources()))
        self.open("/calendar")
        self.assertEqual(dt.date.today().strftime("%Y-%m"), self.page.locator(
            'form.calendar-filters [name="month"]').input_value())

    def test_storage_errors_do_not_break_filters_or_reset(self):
        self.context.add_init_script("""
            for (const method of ['getItem', 'setItem', 'removeItem']) {
              Object.defineProperty(Storage.prototype, method, {
                configurable: true,
                value() { throw new DOMException('blocked', 'SecurityError'); }
              });
            }
        """)
        errors = []
        self.page.on("pageerror", lambda error: errors.append(str(error)))
        self.open("/events")
        self.page.locator('form.event-filters [name="rank"]').select_option("super")
        self.page.wait_for_url("**rank=super**")
        self.page.locator('[data-reset-filters]').click()
        self.page.wait_for_url(self.base_url + "/events")
        self.assertEqual("", self.page.locator('form.event-filters [name="rank"]').input_value())
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
