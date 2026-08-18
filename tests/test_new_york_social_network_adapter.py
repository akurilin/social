import datetime as dt
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.new_york_social_network import (
    ADAPTER_ID,
    PARSER_VERSION,
    NewYorkSocialNetworkAdapter,
)
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError
from crawler.registry import get_adapter


FIXTURES = Path(__file__).resolve().parent / "fixtures"
LISTING = "https://newyorksocialnetwork.com/simple-events-links-list/"
SALSA = "https://newyorksocialnetwork.com/events/salsa-at-sunset-on-the-hudson-3/"
CONEY = (
    "https://newyorksocialnetwork.com/events/"
    "coney-island-scavenger-hunt-fireworks-season-finale/"
)


class Client:
    def __init__(self, pages=None):
        self.pages = pages or {
            LISTING: FIXTURES / "new_york_social_network_quick_list.html",
            SALSA: FIXTURES / "new_york_social_network_salsa.html",
            CONEY: FIXTURES / "new_york_social_network_coney.html",
        }
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        value = self.pages[url]
        body = value.read_bytes() if isinstance(value, Path) else value.encode("utf-8")
        return HttpResponse(
            url=url,
            status=200,
            body=body,
            headers={"content-type": "text/html; charset=utf-8"},
            fetched_at="2026-08-18T10:00:00-04:00",
        )


class NewYorkSocialNetworkAdapterTests(unittest.TestCase):
    def source(self):
        return {
            "id": "new-york-social-network",
            "url": LISTING,
        }

    def test_reads_quick_list_and_event_espresso_jsonld(self):
        client = Client()
        with tempfile.TemporaryDirectory() as tmp:
            result = NewYorkSocialNetworkAdapter(
                client, ArtifactRecorder(tmp)).crawl(
                    self.source(), dt.date(2026, 8, 18), 3, "America/New_York")

        self.assertEqual(client.requests, [LISTING, SALSA, CONEY])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["source"]["artifacts"]), 3)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(len(result["rejections"]), 1)
        self.assertEqual(result["rejections"][0]["reason"], "outside_date_window")

        salsa = result["events"][0]
        self.assertEqual(salsa["title"], "Salsa at Sunset on the Hudson")
        self.assertEqual(salsa["start"], "2026-08-20T18:00:00-04:00")
        self.assertEqual(salsa["end"], "2026-08-20T20:30:00-04:00")
        self.assertEqual(salsa["host"], "New York Social Network")
        self.assertEqual(salsa["venue"], "Hudson River Pier 76")
        self.assertEqual(
            salsa["address"], "408 12th Avenue West, New York, NY, 10018")
        self.assertEqual(salsa["price"], "$15")
        self.assertFalse(salsa["is_free"])
        self.assertEqual(salsa["source_event_id"], "4729")
        self.assertEqual(salsa["explicit_age_min"], 28)
        self.assertIsNone(salsa["explicit_age_max"])

        coney = result["events"][1]
        self.assertEqual(coney["venue"], "Ruby’s Boardwalk Cafe")
        self.assertEqual(coney["price"], "$20–$25")
        self.assertEqual(coney["explicit_age_min"], 21)

    def test_empty_upcoming_section_is_verified(self):
        client = Client({
            LISTING: FIXTURES / "new_york_social_network_empty.html",
        })
        result = NewYorkSocialNetworkAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 18), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_future_coverage_verifies_an_empty_window_without_detail_fetches(self):
        client = Client()
        result = NewYorkSocialNetworkAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 18), 0, "America/New_York")
        self.assertEqual(client.requests, [LISTING])
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(len(result["rejections"]), 3)

    def test_missing_quick_list_section_is_a_parse_error(self):
        client = Client({LISTING: "<html><body>No calendar</body></html>"})
        with self.assertRaisesRegex(ParseError, "Upcoming Events section"):
            NewYorkSocialNetworkAdapter(client).crawl(
                self.source(), dt.date(2026, 8, 18), 10, "America/New_York")

    def test_detail_date_disagreement_is_a_validation_failure(self):
        body = (FIXTURES / "new_york_social_network_salsa.html").read_text()
        body = body.replace(
            '"startDate": "2026-08-20T18:00:00-04:00"',
            '"startDate": "2026-08-19T18:00:00-04:00"',
        )
        client = Client({
            LISTING: FIXTURES / "new_york_social_network_quick_list.html",
            SALSA: body,
            CONEY: FIXTURES / "new_york_social_network_coney.html",
        })
        result = NewYorkSocialNetworkAdapter(client).crawl(
            self.source(), dt.date(2026, 8, 18), 3, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertTrue(any(
            item["reason"] == "event_parse_failed" for item in result["rejections"]))

    def test_adapter_is_registered(self):
        self.assertIs(get_adapter(ADAPTER_ID), NewYorkSocialNetworkAdapter)


if __name__ == "__main__":
    unittest.main()
