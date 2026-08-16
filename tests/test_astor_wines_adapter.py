import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.astor_wines import AstorWinesAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import FetchError, HttpResponse, ParseError


FIXTURES = Path(__file__).resolve().parent / "fixtures"
LISTING = "https://www.astorwines.com/tastingevents.aspx"
DETAIL = "https://www.astorwines.com/event.aspx?eid=ABC-123"
OLD = "https://www.astorwines.com/event.aspx?eid=OLD-456"


class Client:
    def __init__(self, pages, fail_detail=False):
        self.pages, self.fail_detail, self.requests = pages, fail_detail, []

    def get(self, url):
        self.requests.append(url)
        if self.fail_detail and url != LISTING:
            raise FetchError("blocked")
        return HttpResponse(url=url, status=200, body=self.pages[url].read_bytes(),
                            headers={"content-type": "text/html; charset=utf-8"},
                            fetched_at="2026-08-15T12:00:00-04:00")


class AstorWinesAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "astor-wines-tastings", "url": LISTING, "adapter": "astor_wines_tastings"}

    def test_parses_detail_and_filters_window(self):
        client = Client({LISTING: FIXTURES / "astor_wines_tastings_listing.html",
                         DETAIL: FIXTURES / "astor_wines_tastings_detail.html", OLD: FIXTURES / "astor_wines_tastings_old_detail.html"})
        with tempfile.TemporaryDirectory() as tmp:
            result = AstorWinesAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["start"], "2026-08-18T17:00:00-04:00")
        self.assertEqual(event["price"], "Free")
        self.assertEqual(event["address"], "399 Lafayette St, New York, NY 10003")
        self.assertEqual(event["source_event_id"], "ABC-123")
        self.assertEqual(len(result["source"]["artifacts"]), 2)

    def test_detail_fetch_errors_propagate(self):
        client = Client({LISTING: FIXTURES / "astor_wines_tastings_listing.html"}, fail_detail=True)
        with self.assertRaises(FetchError):
            AstorWinesAdapter(client).crawl(self.source(), dt.date(2026, 8, 15), 10, "America/New_York")

    def test_no_links_without_empty_signal_is_parse_failure(self):
        client = Client({LISTING: FIXTURES / "astor_wines_tastings_detail.html"})
        with self.assertRaises(ParseError):
            AstorWinesAdapter(client).crawl(self.source(), dt.date(2026, 8, 15), 10, "America/New_York")

    def test_explicit_empty_calendar_is_verified(self):
        client = Client({LISTING: FIXTURES / "astor_wines_tastings_empty.html"})
        result = AstorWinesAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_malformed_detail_preserves_valid_events(self):
        bad = "https://www.astorwines.com/event.aspx?eid=BAD-999"
        client = Client({LISTING: FIXTURES / "astor_wines_tastings_mixed_listing.html",
                         DETAIL: FIXTURES / "astor_wines_tastings_detail.html",
                         bad: FIXTURES / "astor_wines_tastings_malformed_detail.html"})
        result = AstorWinesAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["source_event_id"], "ABC-123")
        self.assertTrue(any(item["reason"] == "event_parse_failed"
                            for item in result["rejections"]))

    def test_yearless_dates_infer_year_and_end_time(self):
        january = "https://www.astorwines.com/event.aspx?eid=YEARLESS-JAN"
        client = Client({LISTING: FIXTURES / "astor_wines_tastings_yearless_listing.html",
                         "https://www.astorwines.com/event.aspx?eid=YEARLESS-15": FIXTURES / "astor_wines_tastings_yearless_detail.html",
                         january: FIXTURES / "astor_wines_tastings_yearless_january_detail.html"})
        result = AstorWinesAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 15), 2, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["start"], "2026-08-15T15:00:00-04:00")
        self.assertEqual(event["end"], "2026-08-15T18:00:00-04:00")
        self.assertTrue(any(item["reason"] == "outside_date_window"
                            for item in result["rejections"]))
        self.assertNotIn(january, client.requests)


if __name__ == "__main__":
    unittest.main()
