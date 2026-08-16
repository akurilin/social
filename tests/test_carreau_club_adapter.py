import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.carreau_club import (
    ADAPTER_ID,
    PARSER_VERSION,
    CarreauClubFridayMeleeAdapter,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError
from crawler.registry import get_adapter


FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://www.carreauclub.com/"


class Client:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(
            url, 200, self.fixture.read_bytes(),
            {"content-type": "text/html; charset=utf-8"},
            "2026-08-16T12:00:00-04:00",
        )


class CarreauClubFridayMeleeAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "carreau-club", "url": URL, "adapter": ADAPTER_ID}

    def test_generates_only_fridays_inside_the_date_window(self):
        client = Client(FIXTURES / "carreau_club_home.html")
        with tempfile.TemporaryDirectory() as tmp:
            result = CarreauClubFridayMeleeAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 16), 13, "America/New_York")
        self.assertEqual(client.requests, [URL])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual([event["start"] for event in result["events"]], [
            "2026-08-21T19:00:00-04:00", "2026-08-28T19:00:00-04:00",
        ])
        self.assertEqual([event["end"] for event in result["events"]], [
            "2026-08-21T22:00:00-04:00", "2026-08-28T22:00:00-04:00",
        ])
        self.assertEqual(len({event["source_event_id"] for event in result["events"]}), 2)
        event = result["events"][0]
        self.assertEqual(
            event["title"], "Friday Night Pétanque — Free Open Melee Tournament")
        self.assertEqual(event["venue"], "Carreau Club")
        self.assertEqual(event["neighborhood"], "Industry City")
        self.assertEqual(event["address"], "68 34th Street, Brooklyn, NY 11232")
        self.assertEqual(event["price"], "Free")
        self.assertTrue(event["is_free"])
        self.assertEqual(event["signup_url"], URL)
        self.assertEqual(len(result["source"]["artifacts"]), 1)

    def test_a_window_without_friday_is_verified_empty_after_schedule_validation(self):
        result = CarreauClubFridayMeleeAdapter(
            Client(FIXTURES / "carreau_club_home.html")).crawl(
                self.source(), dt.date(2026, 8, 17), 3, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_missing_schedule_evidence_fails_loudly(self):
        with self.assertRaisesRegex(ParseError, "required schedule evidence is missing"):
            CarreauClubFridayMeleeAdapter(
                Client(FIXTURES / "carreau_club_home_missing_schedule.html")).crawl(
                    self.source(), dt.date(2026, 8, 16), 10, "America/New_York")

    def test_adapter_is_registered(self):
        self.assertIs(get_adapter(ADAPTER_ID), CarreauClubFridayMeleeAdapter)


if __name__ == "__main__":
    unittest.main()
