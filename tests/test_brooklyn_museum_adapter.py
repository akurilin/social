import datetime as dt
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crawler.adapters.brooklyn_museum import BrooklynMuseumSearchAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError


FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://www.brooklynmuseum.org/programs"


class Client:
    def __init__(self, mode):
        self.mode = mode
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        page = parse_qs(urlsplit(url).query).get("page", ["1"])[0]
        if self.mode == "programs":
            fixture = FIXTURES / "brooklyn_museum_programs_page_{}.json".format(page)
        elif self.mode == "first_saturdays":
            fixture = FIXTURES / "brooklyn_museum_first_saturdays_page_1.json"
        else:
            fixture = FIXTURES / "brooklyn_museum_empty.json"
        return HttpResponse(url, 200, fixture.read_bytes(),
                            {"content-type": "application/json; charset=utf-8"},
                            "2026-08-15T12:00:00-04:00")


class BrooklynMuseumSearchAdapterTests(unittest.TestCase):
    def source(self, program_filter):
        return {"id": "brooklyn-museum-programs", "url": URL,
                "adapter": "brooklyn_museum_search",
                "adapter_config": {"program_filter": program_filter}}

    def test_all_programs_uses_window_and_pagination(self):
        client = Client("programs")
        with tempfile.TemporaryDirectory() as tmp:
            result = BrooklynMuseumSearchAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source("all_programs"), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][0]["start"], "2026-08-20T18:00:00-04:00")
        self.assertEqual(result["events"][0]["price"], "Free")
        self.assertEqual(len(result["source"]["artifacts"]), 2)
        query = parse_qs(urlsplit(client.requests[0]).query)
        self.assertEqual(query["type"], ["event"])
        self.assertEqual(query["startDate"], ["2026-08-15"])
        self.assertEqual(query["endDate"], ["2026-08-25"])
        self.assertEqual(query["sortField"], ["startDate"])
        self.assertEqual(query["sortOrder"], ["asc"])
        self.assertNotIn("subtype", query)

    def test_first_saturdays_uses_exact_subtype_filter(self):
        client = Client("first_saturdays")
        result = BrooklynMuseumSearchAdapter(client).crawl(
            self.source("first_saturdays"), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["source_event_id"], "bm-101")
        self.assertIn("First Saturdays", result["events"][0]["extracted_json"]["subtype"])
        self.assertEqual(parse_qs(urlsplit(client.requests[0]).query)["subtype"], ["First Saturdays"])

    def test_explicit_empty_window_is_verified(self):
        result = BrooklynMuseumSearchAdapter(Client("empty")).crawl(
            self.source("first_saturdays"), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_first_saturdays_rejects_an_api_record_without_exact_subtype(self):
        result = BrooklynMuseumSearchAdapter(Client("programs")).crawl(
            self.source("first_saturdays"), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("outside_configured_program_filter",
                      [item["reason"] for item in result["rejections"]])

    def test_missing_program_filter_is_parse_error(self):
        with self.assertRaisesRegex(ParseError, "requires adapter_config"):
            BrooklynMuseumSearchAdapter(Client("empty")).crawl(
                {"id": "brooklyn-museum-programs", "url": URL},
                dt.date(2026, 8, 15), 10, "America/New_York")


if __name__ == "__main__":
    unittest.main()
