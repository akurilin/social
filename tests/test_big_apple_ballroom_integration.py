import datetime as dt
import json
import re
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path

from crawler.contracts import HttpResponse
from crawler.runner import execute_source


FIXTURE = Path(__file__).parent / "fixtures" / "big_apple_ballroom.html"
SOURCE_URL = "https://bigappleballroom.com/schedule"
FETCHED_AT = "2026-10-13T12:00:00-04:00"


class FixtureClient:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return HttpResponse(url, HTTPStatus.OK, self.body,
                            {"content-type": "text/html; charset=utf-8"},
                            FETCHED_AT)


def schedule_props(html):
    for script in re.findall(r"<script>(.*?)</script>", html, re.S):
        marker = "self.__next_f.push("
        if marker not in script:
            continue
        call_start = script.index(marker) + len(marker)
        call, _ = json.JSONDecoder().raw_decode(script[call_start:])
        if len(call) < 2:
            continue
        stream = call[1]
        if not stream.startswith("5:"):
            continue
        return json.loads(stream[2:])[3]
    raise AssertionError("fixture has no schedule record")


def replace_schedule_stream(html, stream):
    def replace_script(match):
        script = match.group(1)
        marker = "self.__next_f.push("
        if marker not in script:
            return match.group(0)
        start = script.index(marker) + len(marker)
        call, end = json.JSONDecoder().raw_decode(script[start:])
        if len(call) < 2:
            return match.group(0)
        if not call[1].startswith("5:"):
            return match.group(0)
        replacement = json.dumps([1, stream], separators=(",", ":"))
        return "<script>" + script[:start] + replacement + script[start + end:] + "</script>"

    return re.sub(r"<script>(.*?)</script>", replace_script, html, flags=re.S)


def replace_schedule(html, props):
    schedule = ["$", "$L1e", None, props]
    return replace_schedule_stream(
        html, "5:" + json.dumps(schedule, separators=(",", ":")))


class BigAppleBallroomIntegrationTests(unittest.TestCase):
    def source(self):
        return {
            "id": "big-apple-ballroom",
            "title": "Big Apple Ballroom",
            "url": SOURCE_URL,
            "adapter": "big_apple_ballroom",
        }

    def run_source(self, html, seen=dt.date(2026, 10, 13), lookahead=14):
        client = FixtureClient(html.encode("utf-8"))
        with tempfile.TemporaryDirectory() as cache:
            result = execute_source(
                self.source(),
                {"run_config": {"lookahead_days": lookahead,
                                "timezone": "America/New_York"}},
                seen, client=client, artifact_root=cache,
            )
            artifacts = result["source"]["artifacts"]
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(Path(artifacts[0]["path"]).read_bytes(),
                             html.encode("utf-8"))
        self.assertEqual(client.requests, [SOURCE_URL])
        return result

    def test_calendar_recurrence_overrides_events_and_evidence(self):
        html = FIXTURE.read_text(encoding="utf-8")
        props = schedule_props(html)
        result = self.run_source(html)

        self.assertEqual(result["source"]["state"], "ok")
        events = result["events"]
        self.assertTrue(events)

        # The dated calendar is the oracle for each class occurrence in range.
        expected = {}
        for week in props["month"]["weeks"]:
            for day in week:
                if not "2026-10-13" <= day["key"] <= "2026-10-27":
                    continue
                for item in day["items"]:
                    if item.get("scheduleType") in {"recurring", "monthly"}:
                        expected[(day["key"], item["_id"])] = item

        actual = {}
        for event in events:
            raw = event["extracted_json"]
            if isinstance(raw, dict) and raw.get("scheduleType"):
                key = (event["start"][:10], raw["_id"])
                actual[key] = event
        self.assertEqual(set(actual), set(expected))
        for key, item in expected.items():
            event = actual[key]
            self.assertEqual(event["start"][11:16], item["startTime"])
            self.assertTrue(event["title"].startswith(item["title"]))
            self.assertEqual(event["status"],
                             "cancelled" if item.get("cancelled") else "active")
            self.assertEqual(event["extracted_json"].get("instructor"),
                             item.get("instructor"))
            self.assertEqual(event["extracted_json"].get("dance"), item.get("dance"))

        self.assertTrue(any("Performance Group" in rejection["raw"].get("row", {}).get("title", "")
                            for rejection in result["rejections"]
                            if isinstance(rejection["raw"], dict)))
        rental = [event for event in events if event["title"] == "Belly Dance"]
        self.assertTrue(rental)
        self.assertTrue(any(event["status"] == "active" for event in rental))
        self.assertTrue(all(event["is_free"] is not True for event in rental))

        rounds = next(event for event in events if event["title"] == "Rounds at Big Apple")
        self.assertFalse(rounds["is_free"])
        self.assertIn("$30", rounds["price"])
        repeated = [event for event in events if event["title"] == "Bachata"
                    and event["start"].startswith("2026-10-")]
        self.assertGreaterEqual(len(repeated), 2)
        self.assertEqual(len({event["source_event_id"] for event in repeated}),
                         len(repeated))

        boundary = self.run_source(html, seen=dt.date(2026, 10, 31), lookahead=2)
        november_classes = [event for event in boundary["events"]
                            if event["start"].startswith("2026-11-")]
        self.assertTrue(november_classes)
        self.assertTrue(all(event["start"].endswith("-05:00")
                            for event in november_classes))
        self.assertFalse(any(event["title"] == "Ballroom and More with Gene Eagle"
                             for event in november_classes))
        self.assertTrue(any(event["start"].startswith("2026-11-02")
                            and event["extracted_json"].get("instructor") == "Jen"
                            for event in november_classes))

    def test_empty_complete_schedule_and_broken_records_are_distinguished(self):
        html = FIXTURE.read_text(encoding="utf-8")
        props = schedule_props(html)
        props["month"] = {"weeks": []}
        props["classes"] = []
        props["events"] = []
        props["rotations"] = []
        props["overrides"] = []
        empty = self.run_source(replace_schedule(html, props))
        self.assertEqual(empty["source"]["state"], "empty_verified")
        self.assertEqual(empty["events"], [])

        malformed_props = schedule_props(html)
        malformed_props.pop("classes")
        malformed = self.run_source(replace_schedule(html, malformed_props))
        self.assertEqual(malformed["source"]["state"], "parse_failed")

        broken_props = schedule_props(html)
        broken_props["classes"][0].pop("dayOfWeek")
        broken = self.run_source(replace_schedule(html, broken_props))
        self.assertEqual(broken["source"]["state"], "validation_failed")

        broken = replace_schedule_stream(html, "5:[broken")
        result = self.run_source(broken)
        self.assertEqual(result["source"]["state"], "parse_failed")


if __name__ == "__main__":
    unittest.main()
