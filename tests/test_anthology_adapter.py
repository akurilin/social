import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.anthology import AnthologyVeeziAdapter, PARSER_VERSION, SESSIONS_URL
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Client:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        body = self.fixture.read_bytes() if isinstance(self.fixture, Path) else self.fixture.encode()
        return HttpResponse(url, 200, body, {"content-type": "text/html; charset=utf-8"},
                            "2026-06-20T12:00:00-04:00")


class AnthologyVeeziAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "anthology-film-archives",
                "url": "https://www.anthologyfilmarchives.org/film_screenings/calendar"}

    def test_reads_sessions_filters_window_and_keeps_distinct_repeated_titles(self):
        client = Client(FIXTURES / "anthology_veezi_sessions.html")
        with tempfile.TemporaryDirectory() as tmp:
            result = AnthologyVeeziAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 6, 20), 1, "America/New_York")
        self.assertEqual(client.requests, [SESSIONS_URL])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual({event["source_event_id"] for event in result["events"]}, {"101", "102"})
        event = result["events"][0]
        self.assertEqual(event["title"], "A FILM")
        self.assertEqual(event["start"], "2026-06-20T19:00:00-04:00")
        self.assertEqual(event["url"], "https://ticketing.uswest.veezi.com/purchase/101?siteToken=token")
        self.assertEqual(event["venue"], "Anthology Film Archives")
        self.assertEqual(event["address"], "32 Second Ave, New York, NY 10003")
        self.assertEqual(event["host"], "Anthology Film Archives")
        self.assertEqual(event["status"], "active")
        self.assertEqual(len(result["source"]["artifacts"]), 1)
        self.assertTrue(any(item["reason"] == "outside_date_window" for item in result["rejections"]))

    def test_empty_array_with_explicit_message_is_verified(self):
        result = AnthologyVeeziAdapter(Client(FIXTURES / "anthology_veezi_empty.html")).crawl(
            self.source(), dt.date(2026, 6, 20), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")

    def test_early_coverage_without_window_event_is_suspicious(self):
        result = AnthologyVeeziAdapter(
            Client(FIXTURES / "anthology_veezi_early_coverage.html")).crawl(
                self.source(), dt.date(2026, 6, 25), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_suspicious")

    def test_missing_or_malformed_jsonld_is_parse_failure(self):
        for body in ("<html></html>", '<script type="application/ld+json">{bad</script>'):
            with self.assertRaisesRegex(ParseError, "JSON-LD event array"):
                AnthologyVeeziAdapter(Client(body)).crawl(
                    self.source(), dt.date(2026, 6, 20), 10, "America/New_York")

    def test_bad_record_is_a_validation_failure(self):
        body = (FIXTURES / "anthology_veezi_sessions.html").read_text().replace(
            '"name":"A FILM"', '"name":""', 1)
        result = AnthologyVeeziAdapter(Client(body)).crawl(
            self.source(), dt.date(2026, 6, 20), 1, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertTrue(any(item["reason"] == "event_parse_failed" for item in result["rejections"]))


if __name__ == "__main__":
    unittest.main()
