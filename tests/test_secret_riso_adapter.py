import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.secret_riso import (
    ADAPTER_ID,
    PARSER_VERSION,
    SecretRisoCalendarAdapter,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError


FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://secretrisoclub.com/Calendar"


class Client:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(
            url, 200, self.fixture.read_bytes(),
            {"content-type": "text/html; charset=utf-8"},
            "2026-08-16T12:00:00-04:00")


class SecretRisoCalendarAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "secret-riso-club", "url": URL, "adapter": ADAPTER_ID}

    def test_reads_one_calendar_page_and_filters_explicit_occurrences(self):
        client = Client(FIXTURES / "secret_riso_calendar.html")
        with tempfile.TemporaryDirectory() as tmp:
            result = SecretRisoCalendarAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 16), 10, "America/New_York")
        self.assertEqual(client.requests, [URL])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 3)
        screening, gig, zine = result["events"]
        self.assertEqual(screening["start"], "2026-08-16T18:00:00-04:00")
        self.assertEqual(screening["price"], "$5 at the door")
        self.assertEqual(gig["title"], "Inventing the Gig")
        self.assertEqual(gig["start"], "2026-08-19T19:00:00-04:00")
        self.assertEqual(gig["venue"], "SRC")
        self.assertEqual(gig["address"], "122 Central Ave, Brooklyn")
        self.assertEqual(zine["venue"], "Poster House")
        self.assertEqual(zine["signup_url"],
                         "https://posterhouse.org/event/community-in-print")
        self.assertEqual(len(result["source"]["artifacts"]), 1)
        self.assertEqual(
            {item["reason"] for item in result["rejections"]},
            {"outside_date_window", "missing_explicit_time"})

    def test_empty_delimited_upcoming_section_is_verified(self):
        result = SecretRisoCalendarAdapter(
            Client(FIXTURES / "secret_riso_calendar_empty.html")).crawl(
                self.source(), dt.date(2026, 8, 16), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_reordered_calendar_markers_fail_loudly(self):
        with self.assertRaisesRegex(ParseError, "markers are missing or reordered"):
            SecretRisoCalendarAdapter(
                Client(FIXTURES / "secret_riso_calendar_broken.html")).crawl(
                    self.source(), dt.date(2026, 8, 16), 10, "America/New_York")

    def test_accepts_relative_project_url_when_direct_link_is_absent(self):
        fixture = (FIXTURES / "secret_riso_calendar_empty.html").read_text()
        fixture = fixture.replace(
            ',"direct_link":"https://secretrisoclub.com/Calendar"', "")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calendar.html"
            path.write_text(fixture)
            result = SecretRisoCalendarAdapter(Client(path)).crawl(
                self.source(), dt.date(2026, 8, 16), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")


if __name__ == "__main__":
    unittest.main()
