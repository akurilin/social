import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from crawler.adapters.momence_host import MomenceHostAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse


FIXTURES = Path(__file__).parent / "fixtures"


class Client:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def get(self, url):
        self.requests.append(url)
        page = int(parse_qs(urlsplit(url).query)["page"][0])
        fixture = self.pages[page]
        body = fixture.read_bytes() if isinstance(fixture, Path) else fixture
        return HttpResponse(url, 200, body,
                            {"content-type": "application/json"},
                            "2026-08-16T12:00:00-04:00")


class MomenceHostAdapterTests(unittest.TestCase):
    def source(self):
        return {"id": "drawing-room", "title": "Drawing Room",
                "url": "https://www.nycdrawingroom.com/",
                "momence_host_id": 49694}

    def test_paginates_public_host_api_and_preserves_api_provenance(self):
        client = Client({0: FIXTURES / "momence_host_page_0.json",
                         1: FIXTURES / "momence_host_page_1.json"})
        with tempfile.TemporaryDirectory() as tmp:
            result = MomenceHostAdapter(client, ArtifactRecorder(tmp)).crawl(
                self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["source_event_id"], "141759470")
        self.assertEqual(event["start"], "2026-08-20T10:00:00-04:00")
        self.assertEqual(event["price"], "$25")
        self.assertEqual(event["host"], "Drawing Room")
        self.assertIn("Facilitator: Zoe Wallace", event["description"])
        self.assertEqual(event["capacity_flag"], "12 capacity; 3 sold; 9 remaining")
        self.assertEqual(len(result["source"]["artifacts"]), 2)
        self.assertEqual([parse_qs(urlsplit(url).query)["page"] for url in client.requests],
                         [["0"], ["1"]])
        query = parse_qs(urlsplit(client.requests[0]).query)
        self.assertEqual(query["fromDate"], ["2026-08-15"])
        self.assertEqual(query["toDate"], ["2026-08-25"])
        self.assertEqual(query["timeZone"], ["America/New_York"])
        self.assertTrue(any(item["reason"] == "cancelled" for item in result["rejections"]))

    def test_explicit_empty_pagination_is_verified(self):
        result = MomenceHostAdapter(Client({0: FIXTURES / "momence_host_empty.json"})).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])

    def test_enforces_date_window_even_when_api_returns_an_outside_session(self):
        payload = json.loads((FIXTURES / "momence_host_page_0.json").read_text())
        payload["payload"][0]["startsAt"] = "2026-09-20T14:00:00.000Z"
        payload["payload"][0]["endsAt"] = "2026-09-20T17:00:00.000Z"
        payload["pagination"]["totalCount"] = 1
        result = MomenceHostAdapter(Client({0: json.dumps(payload).encode()})).crawl(
            self.source(), dt.date(2026, 8, 15), 10, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["rejections"][0]["reason"], "outside_date_window")
        self.assertIn("outside the requested date window", result["source"]["error"])


if __name__ == "__main__":
    unittest.main()
