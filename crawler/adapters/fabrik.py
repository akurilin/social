"""Procedural adapter for Fabrik's public gatherings API."""

from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "fabrik_gatherings_api"
PARSER_VERSION = "fabrik-gatherings-api-v1"
METHOD = "python_adapter"
API_ROOT = "https://api.joinfabrik.com"
APP_ROOT = "https://app.joinfabrik.com"
MAX_PAGES = 100
TIME_FORMATS = ("%I:%M %p", "%H:%M")
FREE_WORD = re.compile(r"\bfree\b", re.IGNORECASE)
PRICE_VALUE = re.compile(r"(?:[$€£]\s*|\bUSD\s*)\d|^\s*\d+(?:\.\d{1,2})?\s*$", re.IGNORECASE)


class FabrikGatheringsAdapter:
    """Read the complete, ordered public Fabrik gatherings feed."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        raw_events = []
        expected_total = None
        expected_pages = None

        # Read all pages so a zero inside the date window has complete evidence.
        for page in range(1, MAX_PAGES + 1):
            response = self.client.get(_api_url(page))
            if self.artifacts is not None:
                self.artifacts.save_response(
                    "fabrik_gatherings_json",
                    "fabrik-gatherings-page-{}.json".format(page),
                    response,
                )
            payload = _payload(response, page)
            if expected_total is None:
                expected_total = payload["total_count"]
                expected_pages = payload["total_pages"]
            elif (payload["total_count"], payload["total_pages"]) != (
                    expected_total, expected_pages):
                raise ParseError("Fabrik pagination changed between pages")
            raw_events.extend((item, response) for item in payload["items"])
            if page >= max(expected_pages, 1):
                break
        else:
            raise ParseError("Fabrik pagination exceeded {} pages".format(MAX_PAGES))

        if len(raw_events) != expected_total:
            raise ParseError("Fabrik returned {} gathering(s), expected {}".format(
                len(raw_events), expected_total))
        _validate_order_and_ids([raw for raw, _response in raw_events])

        events = []
        rejections = []
        parse_failures = 0
        for raw, response in raw_events:
            try:
                event = _event(raw, source, response)
            except (ParseError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
                parse_failures += 1
                rejections.append(_rejection(
                    "event_parse_failed", raw, source, response, error=str(error)))
                continue
            event_date = dt.datetime.fromisoformat(event["start"]).date()
            if not seen_date <= event_date <= window_end:
                rejections.append(_rejection(
                    "outside_date_window", raw, source, response))
            elif event["status"] != "active":
                rejections.append(_rejection(event["status"], raw, source, response))
            else:
                events.append(event)

        state = "validation_failed" if parse_failures else (
            "ok" if events else "empty_verified")
        error = ("{} gathering(s) failed structural parsing".format(parse_failures)
                 if parse_failures else None)
        return source_result(
            state=state,
            method=METHOD,
            recipe_version=PARSER_VERSION,
            started_at=started_at,
            finished_at=_now_iso(),
            events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("Fabrik API returned {} ordered gathering(s); {} active "
                    "gathering(s) fell in the inclusive date window {} through {}.").format(
                        expected_total, len(events), seen_date, window_end),
            error=error,
        )


def _api_url(page):
    return "{}/guests/all-gatherings?{}".format(API_ROOT, urlencode({"page": page}))


def _payload(response, requested_page):
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise ParseError("Fabrik gatherings API did not return valid JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ParseError("Fabrik gatherings API is missing an items list")
    for key in ("total_count", "total_pages", "current_page"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ParseError("Fabrik pagination {} is invalid".format(key))
    total_count = payload["total_count"]
    total_pages = payload["total_pages"]
    if total_count < 0 or total_pages < 0 or payload["current_page"] != requested_page:
        raise ParseError("Fabrik pagination does not match the requested page")
    if total_count and total_pages < 1:
        raise ParseError("Fabrik non-empty response has no pages")
    if total_pages and requested_page > total_pages:
        raise ParseError("Fabrik current page exceeds total pages")
    if total_count == 0 and payload["items"]:
        raise ParseError("Fabrik empty pagination has gathering items")
    return payload


def _validate_order_and_ids(raw_events):
    previous_date = None
    event_ids = set()
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        event_id = str(raw.get("id") or "").strip()
        if event_id:
            if event_id in event_ids:
                raise ParseError("Fabrik pagination returned a duplicate gathering id")
            event_ids.add(event_id)
        try:
            event_date = dt.date.fromisoformat(str(raw.get("date") or ""))
        except ValueError:
            continue
        if previous_date is not None and event_date < previous_date:
            raise ParseError("Fabrik gatherings are not in chronological order")
        previous_date = event_date


def _event(raw, source, response):
    if not isinstance(raw, dict):
        raise ParseError("gathering is not an object")
    event_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not event_id or not title:
        raise ParseError("gathering is missing id or title")
    space = raw.get("space")
    if not isinstance(space, dict):
        raise ParseError("gathering is missing its space")
    timezone_details = space.get("timezone_details")
    if not isinstance(timezone_details, dict):
        raise ParseError("gathering is missing space timezone details")
    event_timezone = str(timezone_details.get("iana_name") or "").strip()
    if not event_timezone:
        raise ParseError("gathering is missing an IANA timezone")
    start, end = _datetimes(raw, event_timezone)
    event_url = _event_url(raw)
    status = _status(raw.get("status"))
    address = space.get("address")
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": event_url,
        "signup_url": event_url,
        "host": _host(raw, source),
        "venue": str(space.get("name") or "").strip(),
        "neighborhood": "",
        "address": _address(address),
        "price": _price(raw),
        "is_free": _is_free(raw),
        "description": str(raw.get("description") or "").strip(),
        "capacity_flag": _capacity_flag(raw),
        "status": status,
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": event_id,
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": raw,
    }


def _datetimes(raw, timezone):
    date_value = str(raw.get("date") or "").strip()
    start_value = str(raw.get("start_time") or "").strip()
    if not date_value or not start_value:
        raise ParseError("gathering is missing date or start_time")
    zone = ZoneInfo(timezone)
    start = _local_datetime(date_value, start_value, zone)
    end_value = str(raw.get("end_time") or "").strip()
    if end_value:
        end = _local_datetime(date_value, end_value, zone)
        if end <= start:
            end += dt.timedelta(days=1)
    else:
        duration = raw.get("duration")
        end = (start + dt.timedelta(minutes=duration)
               if isinstance(duration, int) and not isinstance(duration, bool)
               and duration > 0 else None)
    return start.isoformat(), end.isoformat() if end is not None else None


def _local_datetime(date_value, time_value, timezone):
    event_date = dt.date.fromisoformat(date_value)
    for time_format in TIME_FORMATS:
        try:
            event_time = dt.datetime.strptime(time_value, time_format).time()
            return dt.datetime.combine(event_date, event_time, timezone)
        except ValueError:
            continue
    raise ParseError("gathering time has an unsupported format")


def _event_url(raw):
    url = str(raw.get("luma_link") or "").strip()
    if not url:
        short_code = str(raw.get("short_code") or "").strip()
        url = "{}/{}".format(APP_ROOT, short_code) if short_code else ""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ParseError("gathering is missing a valid RSVP URL")
    return url


def _status(value):
    normalized = str(value or "").strip().lower()
    if normalized == "published":
        return "active"
    if normalized in ("cancelled", "canceled"):
        return "cancelled"
    if normalized == "postponed":
        return "postponed"
    raise ParseError("gathering has an unsupported status")


def _host(raw, source):
    community = str(raw.get("community") or "").strip()
    owner = raw.get("owner")
    owner_name = (str(owner.get("display_name") or "").strip()
                  if isinstance(owner, dict) else "")
    return community or owner_name or str(source.get("title") or "").strip()


def _address(value):
    if not isinstance(value, dict):
        return ""
    state_zip = " ".join(str(value.get(key) or "").strip()
                         for key in ("state", "zip_code")).strip()
    parts = [str(value.get(key) or "").strip()
             for key in ("address1", "address2", "city")]
    parts.extend((state_zip, str(value.get("country") or "").strip()))
    return ", ".join(part for part in parts if part)


def _price(raw):
    value = re.sub(r"\s+", " ", str(raw.get("ticketing_details") or "")).strip()
    return "Free" if value and FREE_WORD.search(value) and not PRICE_VALUE.search(value) else value


def _is_free(raw):
    value = str(raw.get("ticketing_details") or "").strip()
    if not value:
        return None
    has_free = bool(FREE_WORD.search(value))
    has_price = bool(PRICE_VALUE.search(value))
    return True if has_free and not has_price else False if has_price and not has_free else None


def _capacity_flag(raw):
    values = []
    event_size = raw.get("event_size")
    if isinstance(event_size, int) and not isinstance(event_size, bool) and event_size > 0:
        values.append("{} planned attendees".format(event_size))
    if raw.get("is_members_only") is True:
        values.append("members only")
    return "; ".join(values) or None


def _rejection(reason, raw, source, response, error=None):
    return {"reason": reason, "raw": {
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": raw.get("id") if isinstance(raw, dict) else None,
        "parser_version": PARSER_VERSION,
        "error": error,
        "extracted_json": raw,
    }}


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
