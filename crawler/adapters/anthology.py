"""Procedural adapter for Anthology Film Archives public Veezi sessions."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import ParseError, source_result
from crawler.jsonld import HtmlDocument, load_json_ld


ADAPTER_ID = "anthology_veezi_jsonld"
PARSER_VERSION = "anthology-veezi-jsonld-v1"
METHOD = "python_adapter"
SESSIONS_URL = (
    "https://ticketing.uswest.veezi.com/sessions/"
    "?siteToken=bsrxtagjxmgh2qy0b6p646xdcr"
)


class AnthologyVeeziAdapter:
    """Read public sessions without loading ticket purchase pages."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now_iso()
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        response = self.client.get(SESSIONS_URL)
        if self.artifacts is not None:
            self.artifacts.save_response("sessions_html", "anthology-veezi-sessions.html", response)
        values = load_json_ld(HtmlDocument(response.text))
        arrays = [value for value in values if isinstance(value, list)]
        if not arrays:
            raise ParseError("Veezi sessions page has no JSON-LD event array")

        events, rejections, errors = [], [], []
        latest_date = None
        raw_count = 0
        for array in arrays:
            for raw in array:
                raw_count += 1
                try:
                    event = _event(raw, source, response, timezone)
                    event_date = dt.datetime.fromisoformat(event["start"]).date()
                    latest_date = max(latest_date, event_date) if latest_date else event_date
                    if seen_date <= event_date <= end_date:
                        events.append(event)
                    else:
                        rejections.append({"reason": "outside_date_window", "raw": event})
                except (ParseError, TypeError, ValueError) as error:
                    errors.append(str(error))
                    rejections.append({"reason": "event_parse_failed", "raw": raw})

        if errors:
            state = "validation_failed"
        elif events:
            state = "ok"
        elif raw_count == 0 and _explicit_empty(response.text):
            state = "empty_verified"
        elif latest_date is not None and latest_date >= end_date:
            state = "empty_verified"
        else:
            state = "empty_suspicious"
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("Anthology Veezi sessions contained {} record(s); {} event(s) were "
                    "in {} through {}.").format(raw_count, len(events), seen_date, end_date),
            error="; ".join(errors[:5]) or None,
        )


def _event(raw, source, response, timezone):
    if not isinstance(raw, dict) or not _is_visual_arts_event(raw):
        raise ParseError("JSON-LD array contains a non-VisualArtsEvent record")
    title = _clean(raw.get("name"))
    start = _local_datetime(raw.get("startDate"), timezone)
    url = _clean(raw.get("url"))
    event_id = _purchase_id(url)
    place = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    venue = _clean(place.get("name"))
    address = _clean(place.get("address"))
    if not title or not start or not event_id or not venue or not address:
        raise ParseError("VisualArtsEvent is missing name, startDate, purchase URL, or location")
    return {
        "title": title, "start": start, "end": None,
        "url": url, "signup_url": url, "host": "Anthology Film Archives",
        "venue": venue, "neighborhood": "", "address": address,
        "price": "", "is_free": None, "description": "", "capacity_flag": None,
        "status": "active", "source_id": source["id"],
        "source_listing_url": source["url"], "source_url": response.url,
        "source_event_id": event_id, "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION, "content_hash": response.content_hash,
        "extracted_json": raw,
    }


def _is_visual_arts_event(raw):
    value = raw.get("@type") if isinstance(raw, dict) else None
    return value == "VisualArtsEvent" or (
        isinstance(value, list) and "VisualArtsEvent" in value)


def _local_datetime(value, timezone):
    if not isinstance(value, str) or not value.strip():
        raise ParseError("VisualArtsEvent is missing startDate")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ParseError("VisualArtsEvent startDate is invalid") from error
    if parsed.tzinfo is None:
        raise ParseError("VisualArtsEvent startDate has no UTC offset")
    return parsed.astimezone(ZoneInfo(timezone)).isoformat()


def _purchase_id(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "ticketing.uswest.veezi.com":
        return ""
    match = re.fullmatch(r"/purchase/(\d+)", parsed.path)
    return match.group(1) if match else ""


def _explicit_empty(text):
    return bool(re.search(r"no\s+(?:upcoming\s+)?(?:sessions|showtimes|events?)", text, re.I))


def _clean(value):
    return " ".join(str(value or "").split())


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
