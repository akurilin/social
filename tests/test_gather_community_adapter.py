import datetime as dt
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler.adapters.gather_community import (
    ADAPTER_ID,
    DEFAULT_API_URL,
    GROUP_ID,
    GatherCommunityCalendarAdapter,
    PARSER_VERSION,
    _public_content,
    _preview_title,
)
from crawler.contracts import HttpResponse, ParseError
from crawler.registry import get_adapter


FIXTURES = Path(__file__).parent / "fixtures"


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def post_json(self, url, payload):
        self.requests.append((url, payload))
        cursor = payload.get("cursor")
        body = self.pages[cursor].encode("utf-8")
        return HttpResponse(
            url=url,
            status=200,
            body=body,
            headers={"content-type": "application/json"},
            fetched_at="2026-08-16T12:00:00-04:00",
        )


class GatherCommunityCalendarAdapterTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "id": "57-nyc",
            "url": "https://www.57.nyc/calendar",
            "api_url": DEFAULT_API_URL,
            "gather_group_id": GROUP_ID,
        }

    def pages(self):
        return {
            None: (FIXTURES / "gather_community_page_1.json").read_text(),
            "cursor-page-2": (FIXTURES / "gather_community_page_2.json").read_text(),
        }

    def test_paginates_whitelists_and_filters_date_window(self):
        client = Client(self.pages())
        with patch("crawler.adapters.gather_community.PAGE_SIZE", 2):
            result = GatherCommunityCalendarAdapter(client).crawl(
                self.source, dt.date(2026, 8, 16), 10, "America/New_York")

        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(result["source"]["artifacts"], [])
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[0][0], DEFAULT_API_URL)
        self.assertEqual(client.requests[0][1]["groupId"], GROUP_ID)
        self.assertEqual(client.requests[1][1]["cursor"], "cursor-page-2")
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual([item["reason"] for item in result["rejections"]],
                         ["outside_date_window"])

        first, second = result["events"]
        self.assertEqual(first["source_event_id"], "entity-1")
        self.assertEqual(first["url"], "https://events.example/first")
        self.assertEqual(first["start"], "2026-08-18T19:30:00-04:00")
        self.assertEqual(first["end"], "2026-08-18T21:00:00-04:00")
        self.assertEqual(first["host"], "")
        self.assertEqual(first["venue"], "")
        self.assertEqual(first["address"], "100 Example Street, Brooklyn, NY")
        self.assertEqual(second["url"], "https://tickets.example/second")
        self.assertEqual(second["source_event_id"], "entity-2")
        self.assertEqual(set(first["extracted_json"]), {
            "post_id", "content", "entity_id", "entity_url", "start_time",
            "entity_type", "end_time", "address", "link_url", "link_title",
        })
        serialized = json.dumps(result)
        for forbidden in (
            "user", "reactions", "email", "phone", "bio",
            "not-retained@example.test", "212-555-0199", "private fixture text",
            "synthetic-member-id",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_normalizes_only_known_link_preview_wrappers(self):
        self.assertEqual(_preview_title("Re:source Practice · Luma"), "Re:source Practice")
        self.assertEqual(_preview_title("Viewcy | Bass Temple"), "Bass Temple")
        self.assertEqual(_preview_title("Artist | Set · Other"), "Artist | Set · Other")

    def test_rejects_missing_title_or_time_without_retaining_post_fields(self):
        page = json.loads((FIXTURES / "gather_community_page_2.json").read_text())
        page["posts"] = [
            {"id": "no-title", "content": "", "entity": {
                "id": "entity-no-title", "type": "EVENT", "startTime": "2026-08-20T22:00:00Z"}, "links": []},
            {"id": "no-time", "content": "No time event", "entity": {
                "id": "entity-no-time", "type": "EVENT", "url": "https://events.example/no-time"}, "links": []},
        ]
        page["endCursor"] = "unused"
        client = Client({None: json.dumps(page)})
        with patch("crawler.adapters.gather_community.PAGE_SIZE", 3):
            result = GatherCommunityCalendarAdapter(client).crawl(
                self.source, dt.date(2026, 8, 16), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(len(result["events"]), 0)
        self.assertEqual([item["reason"] for item in result["rejections"]],
                         ["event_parse_failed", "event_parse_failed"])
        self.assertEqual(set(result["rejections"][0]["raw"]), {
            "post_id", "content", "entity_id", "entity_url", "start_time",
            "entity_type", "end_time", "address", "link_url", "link_title", "error",
        })

    def test_rejects_non_event_entity_and_invalid_end_time(self):
        payload = {"posts": [
            {"id": "non-event", "content": "An announcement", "entity": {
                "id": "entity-note", "type": "NOTE", "startTime": "2026-08-20T22:00:00Z"}, "links": []},
            {"id": "end-before-start", "content": "Time check", "entity": {
                "id": "entity-time", "type": "EVENT", "startTime": "2026-08-20T22:00:00Z",
                "endTime": "2026-08-20T21:00:00Z"}, "links": []},
        ], "endCursor": None}
        client = Client({None: json.dumps(payload)})
        with patch("crawler.adapters.gather_community.PAGE_SIZE", 3):
            result = GatherCommunityCalendarAdapter(client).crawl(
                self.source, dt.date(2026, 8, 16), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual([item["reason"] for item in result["rejections"]],
                         ["event_parse_failed", "event_parse_failed"])
        self.assertEqual(result["rejections"][0]["raw"]["entity_type"], "NOTE")
        self.assertNotIn("user", json.dumps(result["rejections"]))

    def test_rejects_repeated_cursor_on_full_page(self):
        page = json.loads((FIXTURES / "gather_community_page_1.json").read_text())
        client = Client({None: json.dumps(page), "cursor-page-2": json.dumps(page)})
        with patch("crawler.adapters.gather_community.PAGE_SIZE", 2):
            with self.assertRaisesRegex(ParseError, "cursor"):
                GatherCommunityCalendarAdapter(client).crawl(
                    self.source, dt.date(2026, 8, 16), 10, "America/New_York")

    def test_terminal_empty_page_is_verified(self):
        client = Client({None: json.dumps({"posts": [], "endCursor": None})})
        result = GatherCommunityCalendarAdapter(client).crawl(
            self.source, dt.date(2026, 8, 16), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")

    def test_redacts_direct_contact_details_from_event_copy(self):
        text = _public_content("Email hello@example.com or call 212-555-0100")
        self.assertEqual(text, "Email [email removed] or call [phone removed]")

    def test_registry_exposes_adapter(self):
        self.assertIs(get_adapter(ADAPTER_ID), GatherCommunityCalendarAdapter)


if __name__ == "__main__":
    unittest.main()
