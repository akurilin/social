import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.analog_film_nyc import ADAPTER_ID, AnalogFilmNYCAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError


FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCE_URL = "https://analogfilmnyc.org/upcoming-screenings/"


class Client:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(
            url=url, status=200, body=self.fixture.read_bytes(),
            headers={"content-type": "text/html; charset=utf-8"},
            fetched_at="2026-08-16T12:00:00-04:00",
        )


class AnalogFilmNYCAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "analog-film-nyc", "url": SOURCE_URL, "adapter": ADAPTER_ID}

    def test_parses_one_page_multi_film_screenings_and_rejects_missing_time(self):
        client = Client(FIXTURES / "analog_film_nyc_listing.html")
        with tempfile.TemporaryDirectory() as tmp:
            result = AnalogFilmNYCAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 16), 2, "America/New_York")
        self.assertEqual(client.requests, [SOURCE_URL])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 2)
        first, program = result["events"]
        self.assertEqual(first["title"], "Les Bonnes Femmes")
        self.assertEqual(first["start"], "2026-08-16T19:30:00-04:00")
        self.assertEqual(first["host"], "Metrograph")
        self.assertEqual(first["venue"], "Metrograph")
        self.assertEqual(first["url"], "https://metrograph.com/tickets/les-bonnes-femmes")
        self.assertEqual(first["source_id"], "analog-film-nyc")
        self.assertEqual(first["source_listing_url"], SOURCE_URL)
        self.assertEqual(
            program["title"],
            "House Orders / Inventur – Metzstrasse 11 / Ali: Fear Eats the Soul",
        )
        self.assertIn("Introduced by the programmer", program["description"])
        self.assertEqual(program["host"], "The Museum of Modern Art")
        self.assertEqual(len(result["source"]["artifacts"]), 1)
        self.assertEqual(
            [item["reason"] for item in result["rejections"]],
            ["missing_explicit_time"],
        )

    def test_full_empty_day_coverage_is_verified(self):
        result = AnalogFilmNYCAdapter(
            Client(FIXTURES / "analog_film_nyc_empty.html")).crawl(
                self.source(), dt.date(2026, 8, 16), 2, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_short_day_coverage_is_suspicious(self):
        result = AnalogFilmNYCAdapter(
            Client(FIXTURES / "analog_film_nyc_short_coverage.html")).crawl(
                self.source(), dt.date(2026, 8, 16), 2, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_suspicious")

    def test_missing_day_sections_is_a_parse_error(self):
        fixture = FIXTURES / "analog_film_nyc_short_coverage.html"
        class UndatedClient(Client):
            def get(self, url):
                return HttpResponse(
                    url=url, status=200, body=b"<main><h2>Screenings</h2></main>",
                    headers={"content-type": "text/html; charset=utf-8"},
                    fetched_at="2026-08-16T12:00:00-04:00",
                )
        with self.assertRaisesRegex(ParseError, "no dated screening sections"):
            AnalogFilmNYCAdapter(UndatedClient(fixture)).crawl(
                self.source(), dt.date(2026, 8, 16), 2, "America/New_York")

    def test_december_january_rollover(self):
        result = AnalogFilmNYCAdapter(
            Client(FIXTURES / "analog_film_nyc_rollover.html")).crawl(
                self.source(), dt.date(2026, 12, 31), 1, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(
            [event["start"] for event in result["events"]],
            ["2026-12-31T23:30:00-05:00", "2027-01-01T12:30:00-05:00"],
        )


if __name__ == "__main__":
    unittest.main()
