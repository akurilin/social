"""Procedural adapter for Casa Italiana's public Events Calendar API."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "casa_italiana"
PARSER_VERSION = "casa-italiana-tribe-api-v1"
METHOD = "python_adapter"
PER_PAGE = 100
API_PATH = "/wp-json/tribe/events/v1/events"


class CasaItalianaAdapter:
    """Read events from Casa Italiana's same-origin WordPress API."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now_iso()
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        endpoint = _api_endpoint(source["url"])
        page = 1
        expected_total = None
        expected_pages = None
        raw_events = []

        while expected_pages is None or page <= expected_pages:
            response = self.client.get(_page_url(endpoint, seen_date, end_date, page))
            if self.artifacts is not None:
                self.artifacts.save_response(
                    "events_api_json", "casa-italiana-events-page-{}.json".format(page), response)
            payload = _payload(response)
            total, total_pages, events = _validate_page(payload, page)
            if expected_total is None:
                expected_total, expected_pages = total, total_pages
            elif (total, total_pages) != (expected_total, expected_pages):
                raise ParseError("events API pagination metadata changed between pages")
            raw_events.extend((item, response) for item in events)
            page += 1

        if expected_total == 0:
            return source_result(
                state="empty_verified", method=METHOD, recipe_version=PARSER_VERSION,
                started_at=started, finished_at=_now_iso(), events=[], rejections=[],
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail=("Casa Italiana Events Calendar API explicitly returned total 0 for "
                        "{} through {}.").format(seen_date, end_date),
            )
        if len(raw_events) != expected_total:
            raise ParseError("events API returned {} event(s), expected {}".format(
                len(raw_events), expected_total))

        events = []
        rejections = []
        seen = set()
        for raw, response in raw_events:
            try:
                event = _event(raw, source, response, timezone)
                key = (event["source_event_id"], event["start"])
                if key in seen:
                    raise ParseError("events API returned a duplicate event")
                seen.add(key)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if not seen_date <= event_date <= end_date:
                    rejections.append({"reason": "outside_date_window", "raw": event})
                else:
                    events.append(event)
            except (ParseError, ValueError, TypeError) as error:
                rejections.append({"reason": "event_parse_failed", "raw": {
                    "source_id": source["id"], "source_listing_url": source["url"],
                    "source_url": response.url, "parser_version": PARSER_VERSION,
                    "error": str(error), "extracted_json": raw}})

        parse_failed = any(item["reason"] == "event_parse_failed" for item in rejections)
        date_contract_failed = (not events and len(rejections) == len(raw_events)
                                and all(item["reason"] == "outside_date_window"
                                        for item in rejections))
        validation_failed = parse_failed or date_contract_failed
        error = None
        if parse_failed:
            error = "one or more API events could not be parsed"
        elif date_contract_failed:
            error = "events API returned only events outside the requested date window"
        return source_result(
            state="validation_failed" if validation_failed else "ok",
            method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("Casa Italiana Events Calendar API returned {} event(s) across {} page(s); "
                    "{} event(s) were valid in {} through {}.").format(
                        expected_total, expected_pages, len(events), seen_date, end_date),
            error=error,
        )


def _api_endpoint(source_url):
    parsed = urlsplit(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ParseError("source URL is not an absolute HTTP URL")
    return "{}://{}{}".format(parsed.scheme, parsed.netloc, API_PATH)


def _page_url(endpoint, start_date, end_date, page):
    return "{}?{}".format(endpoint, urlencode({
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "per_page": PER_PAGE, "page": page,
    }))


def _payload(response):
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise ParseError("events API did not return valid JSON") from error
    if not isinstance(payload, dict):
        raise ParseError("events API response is not an object")
    return payload


def _validate_page(payload, page):
    required = ("events", "rest_url", "total", "total_pages")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ParseError("events API response is missing {}".format(", ".join(missing)))
    events, total, total_pages = payload["events"], payload["total"], payload["total_pages"]
    if not isinstance(events, list):
        raise ParseError("events API events field is not a list")
    if not isinstance(payload["rest_url"], str) or not payload["rest_url"].strip():
        raise ParseError("events API rest_url field is invalid")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ParseError("events API total field is invalid")
    if isinstance(total_pages, bool) or not isinstance(total_pages, int) or total_pages < 0:
        raise ParseError("events API total_pages field is invalid")
    if total == 0 and (events or total_pages != 0):
        raise ParseError("events API zero result has inconsistent pagination")
    if total > 0 and (total_pages < 1 or page > total_pages):
        raise ParseError("events API pagination fields are inconsistent")
    if any(not isinstance(event, dict) for event in events):
        raise ParseError("events API events list contains a non-object")
    return total, total_pages, events


def _event(raw, source, response, timezone):
    event_id = raw.get("id") or raw.get("global_id")
    title = str(raw.get("title") or "").strip()
    if not event_id or not title or not raw.get("start_date"):
        raise ParseError("event is missing id, title, or start_date")
    start = _local_datetime(raw["start_date"], timezone)
    end = _local_datetime(raw.get("end_date"), timezone, required=False)
    event_url = str(raw.get("url") or "").strip()
    if not event_url:
        raise ParseError("event is missing url")
    venue = raw.get("venue")
    if venue is None:
        venue = {}
    elif not isinstance(venue, dict):
        raise ParseError("event venue field is invalid")
    organizers = raw.get("organizer")
    if organizers is None:
        organizers = []
    elif isinstance(organizers, dict):
        organizers = [organizers]
    if not isinstance(organizers, list) or any(not isinstance(item, dict) for item in organizers):
        raise ParseError("event organizer field is invalid")
    if raw.get("cost") is not None and not isinstance(raw.get("cost"), str):
        raise ParseError("event cost field is invalid")
    if raw.get("website") is not None and not isinstance(raw.get("website"), str):
        raise ParseError("event website field is invalid")
    cost = str(raw.get("cost") or "").strip()
    website = str(raw.get("website") or "").strip()
    signup_url = urljoin(response.url, website) if website else urljoin(response.url, event_url)
    raw_status = str(raw.get("status") or "").strip().lower()
    if not raw_status:
        raise ParseError("event is missing status")
    return {
        "title": title, "start": start, "end": end,
        "url": urljoin(response.url, event_url), "signup_url": signup_url,
        "host": ", ".join(str(item.get("organizer") or "").strip()
                          for item in organizers if item.get("organizer")),
        "venue": str(venue.get("venue") or "").strip(), "neighborhood": "",
        "address": _venue_address(venue), "price": cost, "is_free": _is_free(cost),
        "description": _clean(raw.get("description") or ""), "capacity_flag": None,
        "status": _status(raw_status), "source_id": source["id"],
        "source_listing_url": source["url"], "source_url": response.url,
        "source_event_id": str(event_id), "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION, "content_hash": response.content_hash,
        "extracted_json": raw,
    }


def _venue_address(venue):
    state = venue.get("stateprovince") or venue.get("state") or venue.get("province")
    locality = ", ".join(str(value).strip() for value in (venue.get("city"), state) if value)
    if venue.get("zip"):
        locality = "{} {}".format(locality, venue["zip"]).strip()
    return ", ".join(str(value).strip() for value in
                      (venue.get("address"), locality, venue.get("country")) if value)


def _is_free(cost):
    if not cost:
        return None
    normalized = re.sub(r"[\s.,$€£]", "", cost).lower()
    if normalized in {"0", "000", "free", "gratuito", "gratuita"}:
        return True
    return False


def _status(value):
    if value in {"cancelled", "canceled", "trash"}:
        return "cancelled"
    if value == "postponed":
        return "postponed"
    if value in {"publish", "published"}:
        return "active"
    raise ParseError("event status is unsupported: {}".format(value))


def _local_datetime(value, timezone, required=True):
    if not value:
        if required:
            raise ParseError("missing date")
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    zone = ZoneInfo(timezone)
    return (parsed.replace(tzinfo=zone) if parsed.tzinfo is None
            else parsed.astimezone(zone)).isoformat()


def _clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value)))).strip()


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
