import datetime as dt
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crawler.adapters.casa_italiana import CasaItalianaAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError


FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://www.casaitaliananyu.org/events/"


class Client:
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        page = int(parse_qs(urlsplit(url).query).get("page", [1])[0])
        fixture = self.fixtures[page]
        return HttpResponse(url=url, status=200, body=fixture.read_bytes(),
                            headers={"content-type": "application/json; charset=utf-8"},
                            fetched_at="2026-08-15T12:00:00-04:00")


class CasaItalianaAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "casa-italiana", "url": URL, "adapter": "casa_italiana"}

    def test_parses_paginated_api_response(self):
        client = Client({1: FIXTURES / "casa_italiana_api_page_1.json",
                         2: FIXTURES / "casa_italiana_api_page_2.json"})
        with tempfile.TemporaryDirectory() as tmp:
            result = CasaItalianaAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 2)
        first, second = result["events"]
        self.assertEqual(first["start"], "2026-08-20T18:00:00-04:00")
        self.assertEqual(first["signup_url"], "https://tickets.example/film-night")
        self.assertEqual(first["address"],
                         "24 West 12th Street, New York, NY 10011, United States")
        self.assertEqual(first["host"], "Casa Italiana Zerilli-Marimò")
        self.assertEqual(first["price"], "Free")
        self.assertIs(first["is_free"], True)
        self.assertEqual(second["status"], "cancelled")
        self.assertEqual(second["signup_url"], second["url"])
        self.assertEqual(len(result["source"]["artifacts"]), 2)
        self.assertEqual([parse_qs(urlsplit(url).query)["page"] for url in client.requests],
                         [["1"], ["2"]])
        query = parse_qs(urlsplit(client.requests[0]).query)
        self.assertEqual(query["start_date"], ["2026-08-15"])
        self.assertEqual(query["end_date"], ["2026-08-25"])
        self.assertEqual(query["per_page"], ["100"])

    def test_explicit_total_zero_is_verified_empty(self):
        result = CasaItalianaAdapter(Client({
            1: FIXTURES / "casa_italiana_api_empty.json"})).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_malformed_structure_raises_parse_error(self):
        with self.assertRaisesRegex(ParseError, "missing total_pages"):
            CasaItalianaAdapter(Client({
                1: FIXTURES / "casa_italiana_api_malformed.json"})).crawl(
                    self.source(), dt.date(2026, 8, 15), 10, "America/New_York")

    def test_only_outside_events_violate_api_date_contract(self):
        result = CasaItalianaAdapter(Client({
            1: FIXTURES / "casa_italiana_api_outside.json"})).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(result["events"], [])
        self.assertIn("outside the requested date window", result["source"]["error"])


if __name__ == "__main__":
    unittest.main()
