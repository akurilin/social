import json
import datetime as dt
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from fastapi.testclient import TestClient

import events_store
from web.app import app
from web.calendar_write import CalendarWriteError
from web.__main__ import PROJECT_ROOT, RELOAD_PATTERNS, main
from web.event_theme import event_theme_emoji
from web.repository import Repository


class WebControlCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".cache").mkdir()
        (self.root / "EVENT_RANKING_CRITERIA.md").write_text(
            "# Event Ranking Criteria\n\nOriginal\n", encoding="utf-8")
        (self.root / "EVENT_RANKING_CRITERIA.example.md").write_text(
            "# Event Ranking Criteria\n\nExample\n", encoding="utf-8")
        skill_dir = self.root / ".agents" / "skills" / "sync-events"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Crawler skill\n", encoding="utf-8")
        (self.root / "sources.json").write_text(json.dumps({
            "retrieval_profiles": {
                "test-v1": {
                    "primary": {"method": "http_json", "recipe": "test-v1"},
                    "fallbacks": [],
                    "required_fields": ["title", "start_datetime", "url"],
                    "empty_signal": "Empty array",
                }
            },
            "sources": [{
                "id": "source-one", "title": "Source One",
                "url": "https://example.com/events", "priority": 1,
                "geo": "NYC", "parse_hint": "Test",
                "retrieval_profile": "test-v1",
            }],
            "inbox_sources": {"items": []},
        }, indent=2), encoding="utf-8")
        self.database = self.root / "social.db"
        con = events_store.connect(str(self.database))
        # Dates are anchored to today: the events view defaults to hiding
        # past events, and the calendar tests assume both events share a
        # month, so near a month end both fixtures shift into the next month.
        today = dt.date.today()
        dinner_day = today + dt.timedelta(days=1)
        zine_day = today + dt.timedelta(days=4)
        if dinner_day.month != zine_day.month:
            month_start = zine_day.replace(day=1)
            dinner_day = month_start
            zine_day = month_start + dt.timedelta(days=3)
        self.month = zine_day.strftime("%Y-%m")
        self.month_name = zine_day.strftime("%B %Y")
        self.dinner_day = dinner_day
        self.zine_day = zine_day
        run_date = (today - dt.timedelta(days=6)).isoformat()
        later_seen_date = (today - dt.timedelta(days=2)).isoformat()
        event = events_store.normalise_incoming({
            "source_id": "source-one", "title": "A social dinner",
            "host": "Dinner Host", "start": dinner_day.isoformat() + "T19:00:00-04:00",
            "url": "https://example.com/events/dinner", "venue": "The Room",
        }, run_date)
        later_event = events_store.normalise_incoming({
            "source_id": "source-one", "title": "Zine workshop",
            "host": "Workshop Host", "start": zine_day.isoformat() + "T18:00:00-04:00",
            "url": "https://example.com/events/zine", "venue": "Print Room",
        }, later_seen_date)
        cancelled_event = events_store.normalise_incoming({
            "source_id": "source-one", "title": "Cancelled gathering",
            "start": (today - dt.timedelta(days=2)).isoformat() + "T18:00:00-04:00",
            "url": "https://example.com/events/cancelled", "status": "cancelled",
        }, run_date)
        with con:
            events_store.ensure_run(con, "run-one", run_date, {
                "started_at": run_date + "T09:00:00-04:00",
            })
            events_store.upsert_source_run(con, "run-one", {
                "source_id": "source-one", "method": "http_json", "state": "ok",
                "found_count": 1, "parsed_count": 1, "qualified_count": 1,
                "new_count": 1,
            })
            events_store.upsert_event(con, event)
            events_store.upsert_event(con, later_event)
            events_store.upsert_event(con, cancelled_event)
            events_store.record_discovery(
                con, "run-one", "source-one", event, "new", event["id"]
            )
            con.execute(
                "UPDATE runs SET state='completed', finished_at=? WHERE id='run-one'",
                (run_date + "T09:05:00-04:00",),
            )
        con.close()
        self.event_id = event["id"]
        self.cancelled_event_id = cancelled_event["id"]
        self.original_repo = app.state.repo
        app.state.repo = Repository(self.root, self.database)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.state.repo = self.original_repo
        self.temp.cleanup()

    def test_main_pages_show_linked_operational_data(self):
        for path, text in [
            ("/", "What the crawler knows"),
            ("/sources", "Source One"),
            ("/sources/source-one", "Recent source runs"),
            ("/runs", "run-one"),
            ("/runs/run-one", "A social dinner"),
            ("/events", "A social dinner"),
            ("/calendar?month=" + self.month, "Event calendar"),
            ("/events/{}".format(self.event_id), "Discovery history"),
        ]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(text, response.text, path)

        run_response = self.client.get("/runs/run-one")
        self.assertIn("Existing events", run_response.text)

    def test_old_database_shows_migration_instructions_until_updated(self):
        old_version = events_store.SCHEMA_VERSION - 1
        con = sqlite3.connect(str(self.database))
        con.execute("PRAGMA user_version = {}".format(old_version))
        con.close()

        blocked = self.client.get("/")
        with sqlite3.connect(str(self.database)) as con:
            version_while_blocked = con.execute(
                "PRAGMA user_version").fetchone()[0]

        self.assertEqual(blocked.status_code, 503)
        self.assertIn("Database update required", blocked.text)
        self.assertIn("schema {}".format(old_version), blocked.text)
        self.assertIn(
            "schema {}".format(events_store.SCHEMA_VERSION), blocked.text)
        self.assertIn("python3 social.py migrate", blocked.text)
        self.assertEqual(version_while_blocked, old_version)

        events_store.migrate_database(str(self.database))
        updated = self.client.get("/")

        self.assertEqual(updated.status_code, 200)
        self.assertIn("What the crawler knows", updated.text)

    def test_active_run_page_shows_staged_database_candidates(self):
        con = events_store.connect(str(self.database))
        raw = {
            "source_id": "source-one", "title": "Staged workshop",
            "start": (dt.date.today() + dt.timedelta(days=2)).isoformat()
                     + "T19:00:00-04:00",
            "url": "https://example.com/events/staged-workshop",
        }
        with con:
            events_store.ensure_run(con, "run-active", dt.date.today().isoformat())
            events_store.upsert_source_run(con, "run-active", {
                "source_id": "source-one", "state": "ok",
                "method": "browser_dom",
                "work": {"source_id": "source-one"},
            })
            events_store.record_discovery(
                con, "run-active", "source-one", raw, "staged")
        con.close()

        response = self.client.get("/runs/run-active")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Staged workshop", response.text)
        self.assertIn("1 staged", response.text)
        self.assertIn("badge-staged", response.text)

    def test_local_server_reloads_web_file_changes(self):
        with patch("web.__main__.uvicorn.run") as run:
            main()

        run.assert_called_once_with(
            "web.app:app",
            host="127.0.0.1",
            port=8000,
            reload=True,
            reload_dirs=[str(PROJECT_ROOT)],
            reload_includes=RELOAD_PATTERNS,
        )

    def test_event_list_sorts_in_both_directions(self):
        ascending = self.client.get("/events?sort=title&dir=asc")
        descending = self.client.get("/events?sort=title&dir=desc")
        self.assertEqual(ascending.status_code, 200)
        self.assertEqual(descending.status_code, 200)
        self.assertLess(
            ascending.text.index("A social dinner"),
            ascending.text.index("Zine workshop"),
        )
        self.assertLess(
            descending.text.index("Zine workshop"),
            descending.text.index("A social dinner"),
        )
        self.assertIn('aria-sort="ascending"', ascending.text)
        self.assertIn('aria-sort="descending"', descending.text)

    def test_event_list_defaults_to_start_ascending(self):
        response = self.client.get("/events?period=all")

        self.assertLess(
            response.text.index("A social dinner"),
            response.text.index("Zine workshop"),
        )
        self.assertIn('aria-sort="ascending"', response.text)

    def test_event_list_groups_start_sort_by_day_and_uses_12_hour_times(self):
        chronological = self.client.get("/events?period=all&sort=start&dir=asc")
        alphabetical = self.client.get("/events?period=all&sort=title&dir=asc")

        dinner_label = "{} {}, {}".format(
            self.dinner_day.strftime("%A, %B"),
            self.dinner_day.day,
            self.dinner_day.year,
        )
        self.assertEqual(
            2, chronological.text.count('class="event-day-divider'))
        self.assertIn(
            '<time datetime="{}">{}</time>'.format(
                self.dinner_day.isoformat(), dinner_label),
            chronological.text,
        )
        self.assertIn(
            '<time datetime="{}T19:00:00-04:00">7:00 p.m.</time>'.format(
                self.dinner_day.isoformat()),
            chronological.text,
        )
        self.assertNotIn(
            self.dinner_day.isoformat() + " 19:00", chronological.text)
        self.assertNotIn('class="event-day-divider', alphabetical.text)
        self.assertIn(
            ">{} 19:00</time>".format(self.dinner_day.isoformat()),
            alphabetical.text,
        )

    def test_event_day_divider_differs_from_the_sort_header(self):
        response = self.client.get("/static/app.css")

        self.assertEqual(response.status_code, 200)
        self.assertIn("border-top: 3px solid var(--line-strong)", response.text)
        self.assertIn("background: var(--coverage-bg)", response.text)

    def test_pages_do_not_cache_the_stylesheet(self):
        page_response = self.client.get("/events")
        css_response = self.client.get("/static/app.css")

        self.assertNotIn("/static/app.css?v=", page_response.text)
        self.assertEqual(
            "no-store", css_response.headers.get("cache-control"))

    def test_event_list_sorts_by_first_seen_in_both_directions(self):
        ascending = self.client.get("/events?sort=first_seen&dir=asc")
        descending = self.client.get("/events?sort=first_seen&dir=desc")

        self.assertEqual(ascending.status_code, 200)
        self.assertEqual(descending.status_code, 200)
        self.assertLess(
            ascending.text.index("A social dinner"),
            ascending.text.index("Zine workshop"),
        )
        self.assertLess(
            descending.text.index("Zine workshop"),
            descending.text.index("A social dinner"),
        )
        self.assertNotIn("First added", ascending.text)
        self.assertNotIn('sort=first_seen&amp;dir=desc', ascending.text)

    def test_event_list_sorts_by_last_seen_in_both_directions(self):
        ascending = self.client.get("/events?sort=last_seen&dir=asc")
        descending = self.client.get("/events?sort=last_seen&dir=desc")

        self.assertEqual(ascending.status_code, 200)
        self.assertEqual(descending.status_code, 200)
        self.assertLess(
            ascending.text.index("A social dinner"),
            ascending.text.index("Zine workshop"),
        )
        self.assertLess(
            descending.text.index("Zine workshop"),
            descending.text.index("A social dinner"),
        )
        self.assertNotIn("Last seen", ascending.text)
        self.assertNotIn('sort=last_seen&amp;dir=desc', ascending.text)

    def test_event_list_hides_past_events_by_default(self):
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        old_event = events_store.normalise_incoming({
            "source_id": "source-one", "title": "Past gathering",
            # After 20:00 Eastern, SQLite's date() converts this to the next
            # UTC date. The filter must use the event's stored local date.
            "start": yesterday + "T20:15:00-04:00",
            "url": "https://example.com/events/past-gathering",
        }, yesterday)
        con = events_store.connect(str(self.database))
        with con:
            events_store.upsert_event(con, old_event)
        con.close()

        default_view = self.client.get("/events")
        checked_view = self.client.get(
            "/events", params=[("period", "all"), ("period", "upcoming")]
        )
        unchecked_view = self.client.get("/events", params=[("period", "all")])
        self.assertNotIn("Past gathering", default_view.text)
        self.assertNotIn("Past gathering", checked_view.text)
        self.assertIn("Past gathering", unchecked_view.text)
        self.assertIn('name="period" value="upcoming" checked',
                      checked_view.text)
        self.assertNotIn('name="period" value="upcoming" checked', unchecked_view.text)

    def test_dashboard_uses_stored_local_event_date(self):
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        old_event = events_store.normalise_incoming({
            "source_id": "source-one", "title": "Late past gathering",
            "start": yesterday + "T20:15:00-04:00",
            "url": "https://example.com/events/late-past-gathering",
        }, yesterday)
        before = app.state.repo.dashboard()["counts"]
        con = events_store.connect(str(self.database))
        with con:
            events_store.upsert_event(con, old_event)
        con.close()

        after = app.state.repo.dashboard()["counts"]

        self.assertEqual(after["upcoming"], before["upcoming"])
        self.assertEqual(after["unranked"], before["unranked"])

    def test_calendar_uses_stored_local_event_date(self):
        first_next_month = (dt.date.today().replace(day=28) + dt.timedelta(days=4)) \
            .replace(day=1)
        month_end = first_next_month - dt.timedelta(days=1)
        event = events_store.normalise_incoming({
            "source_id": "source-one", "title": "Late month-end gathering",
            "start": month_end.isoformat() + "T20:15:00-04:00",
            "url": "https://example.com/events/late-month-end-gathering",
        }, dt.date.today().isoformat())
        con = events_store.connect(str(self.database))
        with con:
            events_store.upsert_event(con, event)
        con.close()

        events = app.state.repo.calendar_events(
            month_end.isoformat(), month_end.isoformat())

        self.assertIn("Late month-end gathering", [item["title"] for item in events])

    def test_event_views_only_show_active_events(self):
        for path in (
            "/events?period=all",
            "/events?period=all&status=cancelled",
            "/calendar?month=" + self.month,
            "/calendar?month=" + self.month + "&status=cancelled",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Cancelled gathering", response.text)
                self.assertNotIn('name="status"', response.text)

    def test_event_list_omits_price_and_status_columns(self):
        response = self.client.get("/events?period=all")
        self.assertNotIn(">Price<", response.text)
        self.assertNotIn(">Status<", response.text)
        self.assertNotIn("sort=price", response.text)
        self.assertNotIn("sort=status", response.text)

    def test_event_list_links_directly_to_original_event_pages(self):
        response = self.client.get("/events?period=all")

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="events-table"', response.text)
        self.assertIn('class="event-link-column">Link</th>', response.text)
        self.assertIn(
            'class="event-list-link" href="https://example.com/events/dinner" '
            'target="_blank" rel="noopener" '
            'aria-label="View original event for A social dinner"',
            response.text,
        )
        self.assertLess(
            response.text.index('class="event-link-column">Link</th>'),
            response.text.index('sort=start'),
        )

    def test_result_filters_submit_without_apply_buttons(self):
        pages = {
            "/events?sort=title&dir=desc": True,
            "/calendar?month=" + self.month: False,
            "/sources": True,
        }
        for path, has_search in pages.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("data-auto-submit", response.text)
                self.assertNotIn(">Apply</button>", response.text)
                if has_search:
                    self.assertIn("data-submit-on-input", response.text)

        events = self.client.get("/events?sort=title&dir=desc")
        self.assertIn('name="sort" value="title"', events.text)
        self.assertIn('name="dir" value="desc"', events.text)

    def test_source_search_preserves_text_and_filters_case_insensitively(self):
        response = self.client.get("/sources", params={"q": "Source ONE"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="q" value="Source ONE"', response.text)
        self.assertIn("Source One", response.text)

    def test_source_list_sorts_each_data_column(self):
        # Use distinct values to check text, number, date, and missing-value order.
        rows = [
            dict(id="high", title="Zulu", retrieval_profile="z-profile",
                 added_on="2026-08-10", health=dict(state="success", found_count=10,
                 new_count=0, seen_date="2026-08-10")),
            dict(id="low", title="alpha", retrieval_profile="a-profile",
                 added_on="2026-08-01", health=dict(state="error", found_count=2,
                 new_count=0, seen_date="2026-08-01")),
            dict(id="missing", title="Missing", retrieval_profile="m-profile",
                 added_on=None, health=None),
        ]
        for field in ("title", "retrieval_profile", "health", "found_count",
                      "added_on", "seen_date"):
            for direction in ("asc", "desc"):
                with self.subTest(field=field, direction=direction), patch.object(
                        app.state.repo, "list_sources", return_value=rows):
                    response = self.client.get("/sources", params={
                        "sort": field, "dir": direction, "q": "", "state": "all"})
                    first, second = ("low", "high") if direction == "asc" else ("high", "low")
                    self.assertLess(response.text.index(f'source_id={first}"'),
                                    response.text.index(f'source_id={second}"'))
                    self.assertIn(f'sort={field}&amp;dir={"desc" if direction == "asc" else "asc"}', response.text)
                    self.assertIn('state=all', response.text)
                    if field in ("found_count", "added_on", "seen_date"):
                        self.assertLess(response.text.index(f'source_id={second}"'),
                                        response.text.index('source_id=missing"'))

    def test_source_list_shows_and_sorts_date_added(self):
        catalog = app.state.repo.load_catalog()
        catalog["sources"][0]["added_on"] = "2026-08-01"
        catalog["sources"].append({
            "id": "source-two", "title": "Source Two",
            "url": "https://example.com/two", "priority": 2,
            "geo": "NYC", "parse_hint": "Test",
            "retrieval_profile": "test-v1", "added_on": "2026-08-10",
        })
        app.state.repo.save_catalog(catalog)

        ascending = self.client.get("/sources?sort=added_on&dir=asc")
        descending = self.client.get("/sources?sort=added_on&dir=desc")

        self.assertLess(
            ascending.text.index("Source One"),
            ascending.text.index("Source Two"),
        )
        self.assertLess(
            descending.text.index("Source Two"),
            descending.text.index("Source One"),
        )
        self.assertIn("2026-08-01", ascending.text)
        self.assertIn("2026-08-10", ascending.text)
        self.assertIn('aria-sort="ascending"', ascending.text)
        self.assertIn('aria-sort="descending"', descending.text)

    def test_source_pages_do_not_show_or_edit_a_schedule(self):
        source_list = self.client.get("/sources")
        source_detail = self.client.get("/sources/source-one")
        source_edit = self.client.get("/sources/source-one/edit")

        for response in (source_list, source_detail, source_edit):
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Crawl frequency", response.text)
            self.assertNotIn('name="cadence"', response.text)
        self.assertNotIn(">Schedule<", source_list.text)
        self.assertNotIn("<dt>Schedule</dt>", source_detail.text)
        self.assertIn("test-v1", source_list.text)

    def test_source_list_shows_only_enabled_sources_by_default(self):
        catalog_path = self.root / "sources.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["sources"].append({
            "id": "source-disabled", "title": "Disabled Source",
            "url": "https://example.com/disabled", "priority": 2,
            "geo": "NYC", "parse_hint": "Test",
            "retrieval_profile": "test-v1", "enabled": False,
            "disabled_reason": "Disabled for this test",
        })
        catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

        default_view = self.client.get("/sources")
        all_view = self.client.get("/sources", params={"state": "all"})

        self.assertIn("Source One", default_view.text)
        self.assertNotIn("Disabled Source", default_view.text)
        self.assertIn('type="checkbox" name="state" value="all"', default_view.text)
        self.assertNotIn('value="all" checked', default_view.text)
        self.assertIn("Disabled Source", all_view.text)
        self.assertIn('value="all" checked', all_view.text)
        self.assertIn('Show disabled sources', all_view.text)

    def test_filter_queries_change_their_page_content(self):
        event_search = self.client.get("/events", params={"q": "Zine"})
        self.assertIn("Zine workshop", event_search.text)
        self.assertNotIn("A social dinner", event_search.text)

        disabled_sources = self.client.get("/sources", params={"state": "disabled"})
        self.assertNotIn('href="/source?source_id=source-one"', disabled_sources.text)

        high_fit_calendar = self.client.get(
            "/calendar", params={"month": self.month, "rank": "high"}
        )
        self.assertNotIn('href="/events/{}"'.format(self.event_id), high_fit_calendar.text)

    def test_event_views_share_a_multi_source_filter(self):
        catalog_path = self.root / "sources.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["sources"].append({
            "id": "source-two", "title": "Source Two",
            "url": "https://example.com/source-two", "priority": 2,
            "geo": "NYC", "parse_hint": "Test two",
            "retrieval_profile": "test-v1",
        })
        catalog_path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
        second_event = events_store.normalise_incoming({
            "source_id": "source-two", "title": "Second source gathering",
            "start": self.month + "-10T19:00:00-04:00",
            "url": "https://example.com/source-two/gathering",
        }, dt.date.today().isoformat())
        con = events_store.connect(str(self.database))
        with con:
            events_store.upsert_event(con, second_event)
        con.close()

        params = [
            ("period", "all"), ("sources_filtered", "1"),
            ("source_id", "source-two"),
        ]
        event_list = self.client.get("/events", params=params)
        calendar = self.client.get("/calendar", params=[
            ("month", self.month), ("sources_filtered", "1"),
            ("source_id", "source-two"),
        ])
        no_sources = self.client.get("/events", params={
            "period": "all", "sources_filtered": "1",
        })

        self.assertIn("Second source gathering", event_list.text)
        self.assertNotIn("A social dinner", event_list.text)
        self.assertIn("Second source gathering", calendar.text)
        self.assertNotIn("A social dinner", calendar.text)
        self.assertNotIn("A social dinner", no_sources.text)
        self.assertNotIn("Second source gathering", no_sources.text)
        self.assertIn("1 of 2 sources", event_list.text)
        self.assertIn('data-source-filter', event_list.text)
        self.assertIn('data-source-select-all', event_list.text)
        self.assertIn('data-source-clear-all', event_list.text)
        self.assertIn(
            '/calendar?sources_filtered=1&amp;source_id=source-two',
            event_list.text,
        )
        self.assertEqual(
            2,
            event_list.text.count(
                '/calendar?sources_filtered=1&amp;source_id=source-two'),
        )
        self.assertIn(
            '/events?sources_filtered=1&amp;source_id=source-two&amp;period=all',
            calendar.text,
        )
        self.assertEqual(
            2,
            calendar.text.count(
                '/events?sources_filtered=1&amp;source_id=source-two&amp;period=all'),
        )

    def test_repository_filters_events_by_multiple_sources(self):
        both = app.state.repo.list_events(source_ids=["source-one", "missing"])
        none = app.state.repo.list_events(source_ids=[])

        self.assertEqual(
            {"A social dinner", "Zine workshop"},
            {event["title"] for event in both},
        )
        self.assertEqual([], none)

    def test_fit_filter_uses_selected_rank_as_minimum(self):
        low_event = events_store.normalise_incoming({
            "source_id": "source-one", "title": "Low fit gathering",
            "start": self.month + "-15T17:00:00-04:00",
            "url": "https://example.com/events/low-fit-gathering",
            "rank": "low",
        }, dt.date.today().isoformat())
        ineligible_event = events_store.normalise_incoming({
            "source_id": "source-one", "title": "Members-only gathering",
            "start": self.month + "-16T17:00:00-04:00",
            "url": "https://example.com/events/members-only-gathering",
            "rank": events_store.INELIGIBLE_RANK,
        }, dt.date.today().isoformat())
        con = events_store.connect(str(self.database))
        with con:
            events_store.upsert_event(con, low_event)
            events_store.upsert_event(con, ineligible_event)
            con.execute(
                "UPDATE events SET rank = 'medium' WHERE title = 'A social dinner'")
            con.execute(
                "UPDATE events SET rank = 'high' WHERE title = 'Zine workshop'")
        con.close()

        month_start = dt.date.fromisoformat(self.month + "-01")
        next_month = (month_start + dt.timedelta(days=32)).replace(day=1)
        month_end = next_month - dt.timedelta(days=1)
        low_list = app.state.repo.list_events(rank="low")
        medium_list = app.state.repo.list_events(rank="medium")
        high_list = app.state.repo.list_events(rank="high")
        ineligible_list = app.state.repo.list_events(
            rank=events_store.INELIGIBLE_RANK)
        low_calendar = app.state.repo.calendar_events(
            month_start.isoformat(), month_end.isoformat(), rank="low")
        ineligible_calendar = app.state.repo.calendar_events(
            month_start.isoformat(), month_end.isoformat(),
            rank=events_store.INELIGIBLE_RANK)

        self.assertEqual(
            {"Low fit gathering", "A social dinner", "Zine workshop"},
            {event["title"] for event in low_list},
        )
        self.assertEqual(
            {"A social dinner", "Zine workshop"},
            {event["title"] for event in medium_list},
        )
        self.assertEqual(
            {"Zine workshop"},
            {event["title"] for event in high_list},
        )
        self.assertEqual(
            {"Members-only gathering"},
            {event["title"] for event in ineligible_list},
        )
        self.assertEqual(
            {"Low fit gathering", "A social dinner", "Zine workshop"},
            {event["title"] for event in low_calendar},
        )
        self.assertEqual(
            {"Members-only gathering"},
            {event["title"] for event in ineligible_calendar},
        )

    def test_super_fit_filters_sorts_and_renders(self):
        # Super must survive storage and lead the fit order in both views.
        with events_store.connect(str(self.database)) as con:
            con.execute("UPDATE events SET rank='high' WHERE title='A social dinner'")
            con.execute("UPDATE events SET rank='super' WHERE title='Zine workshop'")
        ranked = app.state.repo.list_events(sort="rank", direction="asc")
        self.assertEqual(["super", "high"], [event["rank"] for event in ranked])
        self.assertEqual(2, len(app.state.repo.list_events(rank="high")))
        self.assertEqual(1, len(app.state.repo.list_events(rank="super")))
        for path in ("/events?rank=super", "/calendar?rank=super&month=" + self.month):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Zine workshop", response.text)
            self.assertNotIn("A social dinner", response.text)
            self.assertIn('value="super" selected', response.text)

    def test_fit_filter_labels_describe_minimum_rank(self):
        for path in ("/events", "/calendar?month=" + self.month):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("High or higher", response.text)
                self.assertIn("Medium or higher", response.text)
                self.assertIn("Low or higher", response.text)
                self.assertIn("Ineligible only", response.text)

    def test_auto_submit_script_handles_changes_and_debounced_search(self):
        response = self.client.get("/static/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn('form[data-auto-submit]', response.text)
        self.assertIn('input[type=\'checkbox\']', response.text)
        self.assertIn('window.setTimeout(submit, 350)', response.text)
        self.assertIn('[data-source-filter]', response.text)
        self.assertIn('data-source-select-all', response.text)

    def test_calendar_grid_groups_events_by_day(self):
        response = self.client.get("/calendar?month=" + self.month)
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.month_name, response.text)
        self.assertIn("A social dinner", response.text)
        self.assertIn("Zine workshop", response.text)
        self.assertIn('class="calendar-grid calendar-week"', response.text)

    def test_calendar_marks_dates_before_today_as_past(self):
        today = dt.date.today()
        past_day = today - dt.timedelta(days=1)
        past_response = self.client.get(
            "/calendar", params={"month": past_day.strftime("%Y-%m")})
        today_response = self.client.get(
            "/calendar", params={"month": today.strftime("%Y-%m")})

        def calendar_day_classes(page, day):
            marker = 'data-date="{}"'.format(day.isoformat())
            marker_at = page.index(marker)
            class_at = page.rfind('class="', 0, marker_at) + len('class="')
            class_end = page.index('"', class_at)
            return page[class_at:class_end].split()

        self.assertIn("past", calendar_day_classes(past_response.text, past_day))
        self.assertNotIn("past", calendar_day_classes(today_response.text, today))

    def test_event_theme_emoji_covers_common_event_types(self):
        examples = {
            "Morning run club": "🏃",
            "Beginner powerlifting class": "🏋️",
            "Street photography walk": "📸",
            "West African dance": "💃",
            "Neighborhood book club": "📚",
            "Pickup volleyball": "⚽",
        }
        for title, emoji in examples.items():
            with self.subTest(title=title):
                self.assertEqual(event_theme_emoji({"title": title}), emoji)
        self.assertEqual(event_theme_emoji({"title": "Open gathering"}), "✨")

    def test_event_theme_emoji_uses_tags_and_appears_in_event_views(self):
        self.assertEqual(event_theme_emoji({
            "title": "Make something together",
            "format_tags": ["photography", "shared activity"],
        }), "📸")

        events = self.client.get("/events", params={"period": "all"})
        calendar = self.client.get("/calendar?month=" + self.month)
        self.assertIn('<span class="event-theme" aria-hidden="true">🍽️</span>A social dinner', events.text)
        self.assertIn('<span class="event-theme" aria-hidden="true">📚</span>Zine workshop', events.text)
        self.assertIn('<span class="event-theme" aria-hidden="true">🍽️</span>A social dinner', calendar.text)
        self.assertIn('<span class="event-theme" aria-hidden="true">📚</span>Zine workshop', calendar.text)

    def test_event_ranking_criteria_editor_saves_without_command_line_work(self):
        response = self.client.post(
            "/settings/event-ranking-criteria",
            data={"content": "# Event Ranking Criteria\n\nUpdated"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            (self.root / "EVENT_RANKING_CRITERIA.md").read_text(
                encoding="utf-8"),
            "# Event Ranking Criteria\n\nUpdated\n",
        )

    def test_event_ranking_editor_uses_example_until_local_file_is_saved(self):
        criteria = self.root / "EVENT_RANKING_CRITERIA.md"
        criteria.unlink()

        response = self.client.get("/settings/event-ranking-criteria")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Example", response.text)
        self.assertFalse(criteria.exists())

    def test_crawler_skill_editor_saves_without_command_line_work(self):
        response = self.client.post(
            "/settings/workflow", data={"content": "# Crawler skill\n\nUpdated"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            (self.root / ".agents" / "skills" / "sync-events" / "SKILL.md")
            .read_text(encoding="utf-8"),
            "# Crawler skill\n\nUpdated\n",
        )

    def test_event_detail_shows_read_only_record_and_disabled_calendar(self):
        con = events_store.connect(str(self.database))
        with con:
            events_store.upsert_event_alias(
                con, self.event_id,
                "https://aggregator.example/event/social-dinner",
                "event-aggregator", dt.date.today().isoformat(),
                dt.date.today().isoformat(),
            )
        con.close()
        path = "/events/{}".format(self.event_id)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Event details", response.text)
        self.assertIn("Fit assessment", response.text)
        self.assertIn('class="button secondary original-event-link"', response.text)
        self.assertIn('>View original event <span aria-hidden="true">↗</span></a>',
                      response.text)
        self.assertIn('target="_blank" rel="noopener"', response.text)
        self.assertIn("Original links", response.text)
        self.assertIn("event-aggregator", response.text)
        self.assertIn("https://aggregator.example/event/social-dinner", response.text)
        self.assertIn("source-one · preferred", response.text)
        self.assertNotIn("Back to events", response.text)
        self.assertNotIn("detail-rail", response.text)
        self.assertIn("Set SOCIAL_APPLE_CALENDAR", response.text)
        self.assertIn('type="submit" disabled aria-disabled="true"', response.text)
        post_response = self.client.post(path, data={"title": "Changed"})
        self.assertEqual(post_response.status_code, 405)
        unchanged = self.client.get(path)
        self.assertIn("A social dinner", unchanged.text)
        self.assertNotIn(">Changed<", unchanged.text)

    def test_event_calendar_save_uses_local_setting_and_end_time(self):
        (self.root / ".env").write_text(
            "SOCIAL_APPLE_CALENDAR=Example Account/main\n", encoding="utf-8")
        path = "/events/{}".format(self.event_id)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOCIAL_APPLE_CALENDAR", None)
            ready = {"ready": True, "message": "Calendar ready: Example Account/main."}
            with patch("web.app.calendar_event_status", return_value=dict(
                    ready, present_ids=set(), ambiguous_ids=set())), \
                    patch("web.app.calendar_readiness", return_value=ready):
                detail = self.client.get(path)
                self.assertIn("Calendar ready: Example Account/main.", detail.text)
                self.assertNotIn('type="submit" disabled', detail.text)
                token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
                end = self.dinner_day.isoformat() + "T21:00"
                with patch("web.app.save_event", return_value="saved") as save:
                    response = self.client.post(
                        path + "/calendar", data={"csrf_token": token, "end": end},
                        follow_redirects=False,
                    )
                    self.assertEqual(response.status_code, 303)
                    self.assertEqual(response.headers["location"], path + "?calendar=saved")
                    self.assertEqual(save.call_args.args[1], "Example Account/main")
                    self.assertEqual(save.call_args.args[3][:16], end)
                    list_response = self.client.post(
                        path + "/calendar", data={
                            "csrf_token": token, "end": end,
                            "return_to": "/events?period=upcoming",
                        }, follow_redirects=False,
                    )
                    self.assertEqual(list_response.headers["location"],
                                     "/events?period=upcoming")
                    json_response = self.client.post(
                        path + "/calendar", data={"csrf_token": token, "end": end},
                        headers={"accept": "application/json"},
                    )
                    self.assertEqual(json_response.json()["state"], "saved")
                    invalid = self.client.post(
                        path + "/calendar", data={"csrf_token": token, "end": "2020-01-01T09:00"},
                    )
                    self.assertEqual(invalid.status_code, 400)
                    self.assertEqual(save.call_count, 3)
                self.assertEqual(self.client.post(path + "/calendar", data={"end": end}).status_code, 403)

    def test_calendar_add_updates_coverage_without_scan(self):
        repository = app.state.repo
        event = repository.get_event(self.event_id)
        date = event["start"][:10]
        path = "/events/{}/calendar".format(self.event_id)
        data = {"csrf_token": app.state.calendar_csrf_token,
                "end": date + "T21:00"}
        headers = {"accept": "application/json"}
        ready = {"ready": True, "message": "Ready"}
        with patch("web.app.calendar_readiness", return_value=ready), \
                patch("web.app.save_event", return_value="saved") as save, \
                patch("web.app.remove_event", return_value="removed"), \
                patch("web.calendar_coverage.read_apple_calendar") as scan:
            # A failed save must not change unchecked dates.
            save.side_effect = CalendarWriteError("Save failed")
            self.assertEqual(self.client.post(path, data=data, headers=headers).status_code, 400)
            self.assertIsNone(repository.load_calendar_coverage())
            save.side_effect = None
            for state in ("saved", "already_added"):
                save.return_value = state
                response = self.client.post(path, data=data, headers=headers)
                self.assertEqual(response.status_code, 200)
                days = {day["date"]: day for day in response.json()["coverage"]["days"]}
                self.assertEqual(days[date]["state"], "planned")
                self.assertIsNone(response.json()["coverage"]["checked_at"])
            # The persisted result must reach both pages without a calendar scan.
            fresh = Repository(self.root, self.database)
            self.assertEqual(next(day for day in fresh.calendar_coverage()["days"]
                                  if day["date"] == date)["state"], "planned")
            for page in ("/events", "/calendar?month=" + self.month):
                html = self.client.get(page).text
                self.assertRegex(html, r'coverage-tile coverage-planned" data-coverage-date="' + date)
            # Keep the day green when another confirmed event is still present.
            other = dict(event, id="other-confirmed-event")
            repository.update_calendar_plan(other, added=True)
            removed = self.client.post(path + "/remove", data=data, headers=headers)
            self.assertEqual(next(day for day in removed.json()["coverage"]["days"]
                                  if day["date"] == date)["state"], "planned")
            repository.update_calendar_plan(other, added=False)
            self.assertEqual(next(day for day in repository.calendar_coverage()["days"]
                                  if day["date"] == date)["state"], "unknown")
            scan.assert_not_called()
            # A scan failure keeps confirmed adds; a completed scan replaces them.
            from web.calendar_coverage import CalendarCoverageError, refresh_coverage
            repository.update_calendar_plan(event, added=True)
            scan.side_effect = CalendarCoverageError("Read failed")
            failed = refresh_coverage(repository)
            self.assertEqual(next(day for day in failed["days"]
                                  if day["date"] == date)["state"], "planned")

            def add_during_scan(*args):
                repository.update_calendar_plan(event, added=True)
                return {"events": [], "timezone": "America/New_York"}

            scan.side_effect = add_during_scan
            concurrent = refresh_coverage(repository)
            self.assertEqual(next(day for day in concurrent["days"]
                                  if day["date"] == date)["state"], "planned")
            scan.side_effect = None
            scan.return_value = {"events": [], "timezone": "America/New_York"}
            completed = refresh_coverage(repository)
            self.assertEqual(next(day for day in completed["days"]
                                  if day["date"] == date)["state"], "open")

    def test_calendar_save_stays_disabled_for_unverified_target(self):
        (self.root / ".env").write_text(
            "SOCIAL_APPLE_CALENDAR=Example Account/missing\n", encoding="utf-8")
        path = "/events/{}".format(self.event_id)
        unavailable = {"ready": False, "message": "This calendar is missing or cannot be changed."}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOCIAL_APPLE_CALENDAR", None)
            with patch("web.app.calendar_event_status", return_value=dict(
                    unavailable, present_ids=set(), ambiguous_ids=set())), \
                    patch("web.app.calendar_readiness", return_value=unavailable):
                detail = self.client.get(path)
                self.assertIn(unavailable["message"], detail.text)
                self.assertIn('type="submit" disabled aria-disabled="true"', detail.text)
                token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
                with patch("web.app.save_event") as save:
                    response = self.client.post(
                        path + "/calendar", data={
                            "csrf_token": token,
                            "end": self.dinner_day.isoformat() + "T21:00",
                        },
                    )
                    self.assertEqual(response.status_code, 409)
                    save.assert_not_called()

    def test_event_list_marks_events_already_on_calendar(self):
        state = {"ready": True, "message": "Ready to save.",
                 "present_ids": {self.event_id}, "ambiguous_ids": set()}
        with patch("web.app.calendar_event_status", return_value=state):
            response = self.client.get("/events?period=upcoming")
        cell = re.search(
            r'<td class="event-calendar-cell" id="calendar-cell-{}">(.*?)</td>'
            .format(self.event_id), response.text, re.S)
        self.assertIsNotNone(cell)
        self.assertIn("✓ Added", cell.group(1))
        self.assertIn("/calendar/remove", cell.group(1))
        self.assertIn(">Remove</button>", cell.group(1))
        state["ambiguous_ids"] = {self.event_id}
        with patch("web.app.calendar_event_status", return_value=state):
            ambiguous = self.client.get("/events?period=upcoming")
        self.assertIn(
            'disabled title="Several calendar events match.', ambiguous.text)

    def test_calendar_remove_checks_target_and_match(self):
        (self.root / ".env").write_text(
            "SOCIAL_APPLE_CALENDAR=Example Account/main\n", encoding="utf-8")
        path = "/events/{}".format(self.event_id)
        ready = {"ready": True, "message": "Calendar ready: Example Account/main."}
        state = dict(ready, present_ids={self.event_id}, ambiguous_ids=set())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOCIAL_APPLE_CALENDAR", None)
            with patch("web.app.calendar_event_status", return_value=state), \
                    patch("web.app.calendar_readiness", return_value=ready):
                detail = self.client.get(path)
                self.assertIn("Remove from Calendar", detail.text)
                token = re.search(r'name="csrf_token" value="([^"]+)"', detail.text).group(1)
                with patch("web.app.remove_event", return_value="removed") as remove:
                    response = self.client.post(
                        path + "/calendar/remove", data={"csrf_token": token},
                        headers={"accept": "application/json"},
                    )
                    self.assertEqual(response.json()["state"], "removed")
                    self.assertEqual(remove.call_args.args[1], "Example Account/main")
                    self.assertEqual(remove.call_args.args[2]["id"], self.event_id)
                    redirected = self.client.post(
                        path + "/calendar/remove", data={
                            "csrf_token": token, "return_to": "/events?period=upcoming",
                        }, follow_redirects=False,
                    )
                    self.assertEqual(redirected.status_code, 303)
                    self.assertEqual(redirected.headers["location"],
                                     "/events?period=upcoming")
                with patch("web.app.remove_event", side_effect=CalendarWriteError(
                        "Several calendar events match this record.")):
                    conflict = self.client.post(
                        path + "/calendar/remove", data={"csrf_token": token},
                        headers={"accept": "application/json"},
                    )
                    self.assertEqual(conflict.status_code, 409)
                self.assertEqual(self.client.post(
                    path + "/calendar/remove", data={}).status_code, 403)

    def test_catalog_page_no_longer_exists(self):
        self.assertEqual(self.client.get("/catalog").status_code, 404)
        self.assertEqual(self.client.post("/catalog", data={"content": "{bad json"}).status_code, 404)
        catalog = json.loads((self.root / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual(catalog["sources"][0]["id"], "source-one")

    def test_repository_removes_sources_in_one_validated_catalog_write(self):
        repository = Repository(self.root, self.database)
        catalog = repository.load_catalog()
        catalog["retrieval_profiles"].update({
            "rss-v1": dict(catalog["retrieval_profiles"]["test-v1"]),
            "mail-v1": dict(catalog["retrieval_profiles"]["test-v1"]),
        })
        catalog["sources"].append({
            "id": "mailing-list", "title": "Mailing List",
            "url": "https://example.com/archive", "priority": 2,
            "geo": "NYC", "parse_hint": "Public archive",
            "retrieval_profile": "rss-v1",
        })
        catalog["inbox_sources"] = {
            "retrieval_profile": "mail-v1",
            "items": [{"id": "inbox-one", "title": "Inbox One"}],
        }
        repository.save_catalog(catalog)

        removed = repository.remove_catalog_sources(
            ["mailing-list"], remove_inbox=True,
            profile_ids=["rss-v1", "mail-v1"],
        )

        updated = repository.load_catalog()
        self.assertEqual([source["id"] for source in updated["sources"]], ["source-one"])
        self.assertNotIn("inbox_sources", updated)
        self.assertNotIn("rss-v1", updated["retrieval_profiles"])
        self.assertNotIn("mail-v1", updated["retrieval_profiles"])
        self.assertEqual(removed["sources"], ["mailing-list"])
        self.assertEqual(removed["inbox_sources"], ["inbox-one"])

    def test_repository_dates_new_sources_and_keeps_date_on_edit(self):
        repository = Repository(self.root, self.database)
        source = {
            "id": "source-two", "title": "Source Two",
            "url": "https://example.com/two", "priority": 2,
            "geo": "NYC", "parse_hint": "Test",
            "retrieval_profile": "test-v1",
        }

        repository.upsert_source(source)
        created = repository.get_source("source-two")
        source["title"] = "Renamed Source"
        repository.upsert_source(source, original_id="source-two")
        updated = repository.get_source("source-two")

        self.assertEqual(created["added_on"], dt.date.today().isoformat())
        self.assertEqual(updated["added_on"], created["added_on"])

    def test_source_list_excludes_sources_that_exist_only_in_history(self):
        repository = Repository(self.root, self.database)
        with repository.connect() as con:
            events_store.upsert_source_run(con, "run-one", {
                "source_id": "removed-source", "method": "http_json",
                "state": "blocked",
            })

        listed_ids = {source["id"] for source in repository.list_sources()}

        self.assertNotIn("removed-source", listed_ids)
        self.assertEqual(
            repository.get_source("removed-source")["catalog_kind"],
            "historical",
        )


if __name__ == "__main__":
    unittest.main()
