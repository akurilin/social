import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.luma import LumaCalendarAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse


FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://lu.ma/readingrhythms-manhattan"


class FakeClient:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        body = (self.fixture.read_bytes() if isinstance(self.fixture, Path)
                else self.fixture.encode("utf-8"))
        return HttpResponse(url=url, status=200, body=body,
                            headers={"content-type": "text/html; charset=utf-8"},
                            fetched_at="2026-08-15T12:00:00-04:00")


class LumaAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "reading-rhythms-manhattan", "url": URL,
                "adapter": "luma_calendar"}

    def test_extracts_jsonld_and_filters_date_window(self):
        client = FakeClient(FIXTURES / "luma_calendar.html")
        with tempfile.TemporaryDirectory() as tmp:
            result = LumaCalendarAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(len(result["rejections"]), 2)
        event = result["events"][0]
        self.assertEqual(event["start"], "2026-08-18T18:30:00-04:00")
        self.assertEqual(event["end"], "2026-08-18T20:30:00-04:00")
        self.assertEqual(event["signup_url"], "https://lu.ma/luma-writing-2026-08-18/tickets")
        self.assertEqual(event["price"], "$18")
        self.assertEqual(event["capacity_flag"], "limited")
        self.assertEqual(event["address"], "123 Avenue A, New York, NY 10009")
        self.assertEqual(event["host"], "Luma Writers")
        self.assertNotIn("orientation_scope", event)
        self.assertNotIn("explicit_age_min", event)
        self.assertEqual(len(result["source"]["artifacts"]), 1)
        self.assertEqual(client.requests, [URL])

    def test_explicit_empty_itemlist_is_verified(self):
        client = FakeClient(FIXTURES / "luma_calendar_empty.html")
        result = LumaCalendarAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_stale_only_calendar_is_suspicious(self):
        client = FakeClient(FIXTURES / "luma_calendar.html")
        result = LumaCalendarAdapter(client).crawl(
            self.source(), dt.date(2026, 9, 2), 2, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_suspicious")

    def test_partial_event_parse_failure_is_validation_failed(self):
        body = (FIXTURES / "luma_calendar.html").read_text(encoding="utf-8")
        body = body.replace(
            '"startDate":"2026-08-01T18:30:00-04:00"',
            '"startDate":""',
        )
        result = LumaCalendarAdapter(FakeClient(body)).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(len(result["events"]), 1)
        self.assertTrue(any(item["reason"] == "event_parse_failed"
                            for item in result["rejections"]))


if __name__ == "__main__":
    unittest.main()
