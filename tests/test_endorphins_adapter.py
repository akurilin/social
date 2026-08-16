import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from crawler.adapters.endorphins import EndorphinsAdapter, PARSER_VERSION
from crawler.artifacts import ArtifactRecorder
from crawler.contracts import HttpResponse, ParseError


FIXTURE = Path(__file__).parent / "fixtures" / "endorphins_city_api_v1.json"
CITY_URL = "https://app.endorphinsrunning.com/city/city-nyc"
API_URL = "https://app.endorphinsrunning.com/api/city/city-nyc"


class FakeClient:
    def __init__(self, body=None):
        self.requests = []
        self.body = body or FIXTURE.read_bytes()

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(url, 200, self.body, {"content-type": "application/json"}, "2026-08-15T12:00:00-04:00")


class EndorphinsAdapterTests(unittest.TestCase):
    def test_filters_city_window_and_inactive_events(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            result = EndorphinsAdapter(client, ArtifactRecorder(tmp)).crawl(
                {"id": "endorphins-nyc", "url": CITY_URL}, dt.date(2026, 8, 15), 1, "America/New_York")
        self.assertEqual(client.requests, [API_URL])
        self.assertEqual(result["source"]["state"], "ok")
        self.assertEqual(result["source"]["recipe_version"], PARSER_VERSION)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["start"], "2026-08-15T06:45:00-04:00")
        self.assertEqual(result["events"][0]["url"], "https://app.endorphinsrunning.com/event/evt-nyc-1")
        self.assertEqual(result["events"][0]["venue"], "Pier 45 at Hudson River Park")
        self.assertEqual(result["events"][0]["capacity_flag"], "4 confirmed; 3 maybe; 0 waitlist")
        self.assertIn("Friendly run", result["events"][0]["description"])
        reasons = [item["reason"] for item in result["rejections"]]
        self.assertIn("cancelled_or_deleted", reasons)
        self.assertIn("outside_configured_city", reasons)
        self.assertIn("outside_date_window", reasons)
        self.assertEqual(len(result["source"]["artifacts"]), 1)

    def test_rejects_payload_for_a_different_configured_city(self):
        payload = json.loads(FIXTURE.read_text())
        payload["city"]["_id"] = "city-boston"
        with self.assertRaisesRegex(ParseError, "does not match configured city id"):
            EndorphinsAdapter(FakeClient(json.dumps(payload).encode())).crawl(
                {"id": "endorphins-nyc", "url": CITY_URL}, dt.date(2026, 8, 15), 1, "America/New_York")

    def test_parse_only_failure_is_validation_failed(self):
        payload = json.loads(FIXTURE.read_text())
        payload["city"]["events"] = [{"_id": "bad", "title": "Missing start", "city": {"_id": "city-nyc"}}]
        result = EndorphinsAdapter(FakeClient(json.dumps(payload).encode())).crawl(
            {"id": "endorphins-nyc", "url": CITY_URL}, dt.date(2026, 8, 15), 1, "America/New_York")
        self.assertEqual(result["source"]["state"], "validation_failed")
        self.assertEqual(result["events"], [])
        self.assertIn("1 event(s) failed structural parsing", result["source"]["error"])

    def test_excludes_event_with_missing_city_and_keeps_unknown_capacity(self):
        payload = json.loads(FIXTURE.read_text())
        payload["city"]["events"].append({
            "_id": "evt-no-city", "title": "Unscoped Run",
            "startTime": "2026-08-15T10:45:00.000Z",
            "locationString": "New York, NY", "cancelled": False, "deleted": False,
        })
        payload["city"]["events"][0].pop("rsvpCounts")
        result = EndorphinsAdapter(FakeClient(json.dumps(payload).encode())).crawl(
            {"id": "endorphins-nyc", "url": CITY_URL}, dt.date(2026, 8, 15), 1, "America/New_York")
        self.assertEqual(len(result["events"]), 1)
        self.assertIsNone(result["events"][0]["capacity_flag"])
        self.assertTrue(any(item["reason"] == "outside_configured_city" for item in result["rejections"]))

    def test_explicit_empty_window_is_verified(self):
        client = FakeClient()
        result = EndorphinsAdapter(client).crawl(
            {"id": "endorphins-nyc", "url": CITY_URL}, dt.date(2027, 1, 1), 1, "America/New_York")
        self.assertEqual(result["source"]["state"], "empty_verified")
        self.assertEqual(result["events"], [])


if __name__ == "__main__":
    unittest.main()
