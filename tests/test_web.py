import json
import datetime as dt
import os
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
        skill_dir = self.root / ".agents" / "skills" / "social-crawler"
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
        self.assertIn("First added", ascending.text)
        self.assertIn('sort=first_seen&amp;dir=desc', ascending.text)
        self.assertIn('aria-sort="ascending"', ascending.text)
        self.assertIn('aria-sort="descending"', descending.text)

    def test_event_list_shows_and_sorts_by_last_seen(self):
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
        self.assertIn("Last seen", ascending.text)
        self.assertIn('sort=last_seen&amp;dir=desc', ascending.text)
        self.assertIn('aria-sort="ascending"', ascending.text)
        self.assertIn('aria-sort="descending"', descending.text)

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
        self.assertIn('<option value="enabled" selected>', default_view.text)
        self.assertIn("Disabled Source", all_view.text)
        self.assertIn('<option value="all" selected>', all_view.text)

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

    def test_fit_filter_uses_selected_rank_as_minimum(self):
        low_event = events_store.normalise_incoming({
            "source_id": "source-one", "title": "Low fit gathering",
            "start": self.month + "-15T17:00:00-04:00",
            "url": "https://example.com/events/low-fit-gathering",
            "rank": "low",
        }, dt.date.today().isoformat())
        con = events_store.connect(str(self.database))
        with con:
            events_store.upsert_event(con, low_event)
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
        low_calendar = app.state.repo.calendar_events(
            month_start.isoformat(), month_end.isoformat(), rank="low")

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
            {"Low fit gathering", "A social dinner", "Zine workshop"},
            {event["title"] for event in low_calendar},
        )

    def test_fit_filter_labels_describe_minimum_rank(self):
        for path in ("/events", "/calendar?month=" + self.month):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("High or higher", response.text)
                self.assertIn("Medium or higher", response.text)
                self.assertIn("Low or higher", response.text)

    def test_auto_submit_script_handles_changes_and_debounced_search(self):
        response = self.client.get("/static/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn('form[data-auto-submit]', response.text)
        self.assertIn('input[type=\'checkbox\']', response.text)
        self.assertIn('window.setTimeout(submit, 350)', response.text)

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
            (self.root / ".agents" / "skills" / "social-crawler" / "SKILL.md")
            .read_text(encoding="utf-8"),
            "# Crawler skill\n\nUpdated\n",
        )

    def test_event_detail_is_read_only(self):
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
        self.assertNotIn("<form", response.text)
        self.assertNotIn("Save event", response.text)
        self.assertNotIn("<input", response.text)
        self.assertNotIn("<textarea", response.text)
        self.assertNotIn("<select", response.text)

        post_response = self.client.post(path, data={"title": "Changed"})
        self.assertEqual(post_response.status_code, 405)
        unchanged = self.client.get(path)
        self.assertIn("A social dinner", unchanged.text)
        self.assertNotIn(">Changed<", unchanged.text)

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
