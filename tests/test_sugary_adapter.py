import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.sugary import DEFAULT_API_URL, PARSER_VERSION, SugaryAdapter
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse


FIXTURES = Path(__file__).parent / "fixtures"


class Client:
    def __init__(self, body=None):
        self.body = body or (FIXTURES / "sugary_events_api.json").read_bytes()
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(url, 200, self.body, {"content-type": "application/json"},
                            "2026-08-16T12:00:00-04:00")


class SugaryAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "sugary-site", "url": "https://www.sugarynyc.com", "api_url": DEFAULT_API_URL}

    def test_parses_public_api_and_filters_date_locally(self):
        client = Client()
        with tempfile.TemporaryDirectory() as tmp:
            result = SugaryAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(client.requests, [DEFAULT_API_URL + "?page=1&limit=100"])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["source_event_id"], "810")
        self.assertEqual(event["start"], "2026-08-20T18:30:00-04:00")
        self.assertEqual(event["address"], "Community Table, East Village, Manhattan")
        self.assertTrue(event["is_free"])
        self.assertEqual(len(result["source"]["artifacts"]), 1)
        self.assertEqual({item["reason"] for item in result["rejections"]},
                         {"outside_date_window", "inactive"})

    def test_explicit_empty_api_collection_is_verified(self):
        result = SugaryAdapter(Client(json.dumps({"events": [], "pagination": {
            "page": 1, "limit": 100, "total": 0, "totalPages": 1,
            "hasMore": False}}).encode())).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")


if __name__ == "__main__":
    unittest.main()
