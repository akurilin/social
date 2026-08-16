import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.lalliance import ADAPTER_ID, LAllianceEventsAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError


FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://lallianceny.org/events/"


class Client:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(url, 200, self.fixture.read_bytes(),
                            {"content-type": "text/html; charset=utf-8"},
                            "2026-08-16T12:00:00-04:00")


class LAllianceEventsAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "lalliance-fiaf", "url": URL, "adapter": ADAPTER_ID}

    def test_reads_one_page_explicit_occurrences_and_rejects_past_and_bad_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = LAllianceEventsAdapter(
                Client(FIXTURES / "lalliance_events.html"), ArtifactRecorder(tmp)).crawl(
                    self.source(), dt.date(2026, 9, 15), 11, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 4)
        first, second, chess_one, chess_two = result["events"]
        self.assertEqual(first["start"], "2026-09-15T16:00:00-04:00")
        self.assertEqual(second["start"], "2026-09-15T19:00:00-04:00")
        self.assertEqual(first["venue"], "Florence Gould Theater")
        self.assertEqual(first["signup_url"], "https://tickets.example/a")
        self.assertIn("Happy hour", first["description"])
        self.assertEqual(chess_one["start"], "2026-09-19T12:00:00-04:00")
        self.assertEqual(chess_two["start"], "2026-09-26T12:00:00-04:00")
        self.assertTrue(chess_one["is_free"])
        self.assertEqual(
            [item["reason"] for item in result["rejections"]],
            ["past_event", "event_parse_failed", "missing_explicit_time"],
        )
        self.assertEqual(len(result["source"]["artifacts"]), 1)

    def test_future_only_cards_make_an_empty_window_verified(self):
        result = LAllianceEventsAdapter(Client(FIXTURES / "lalliance_events_future.html")).crawl(
            self.source(), dt.date(2026, 8, 16), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertTrue(all(item["reason"] == "outside_date_window"
                            for item in result["rejections"]))

    def test_explicit_empty_page_is_verified(self):
        result = LAllianceEventsAdapter(
            Client(FIXTURES / "lalliance_events_empty.html")).crawl(
                self.source(), dt.date(2026, 8, 16), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_date_only_event_makes_its_window_suspicious(self):
        result = LAllianceEventsAdapter(
            Client(FIXTURES / "lalliance_events_date_only.html")).crawl(
                self.source(), dt.date(2026, 9, 18), 0, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_suspicious")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["rejections"][0]["reason"], "missing_explicit_time")
        self.assertEqual(result["rejections"][0]["raw"]["date"], "2026-09-18")

    def test_missing_archive_structure_is_loud(self):
        class BrokenClient(Client):
            def get(self, url):
                return HttpResponse(url, 200, b"<main><h1>Events</h1></main>",
                                    {"content-type": "text/html"},
                                    "2026-08-16T12:00:00-04:00")
        with self.assertRaisesRegex(ParseError, "events-grid__main"):
            LAllianceEventsAdapter(BrokenClient(FIXTURES / "lalliance_events.html")).crawl(
                self.source(), dt.date(2026, 8, 16), 10, "America/New_York")


if __name__ == "__main__":
    unittest.main()
