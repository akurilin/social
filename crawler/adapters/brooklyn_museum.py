"""Procedural adapter for Brooklyn Museum programs via its public search API."""

from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "brooklyn_museum_search"
PARSER_VERSION = "brooklyn-museum-search-api-v1"
METHOD = "python_adapter"
API_URL = "https://search.brooklynmuseum.org/api/search"
FILTER_CONFIG = "program_filter"
ALL_PROGRAMS = "all_programs"
FIRST_SATURDAYS = "first_saturdays"
FIRST_SATURDAYS_SUBTYPE = "First Saturdays"


class BrooklynMuseumSearchAdapter:
    """Read all museum programs or the First Saturdays subset from one API."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now_iso()
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        program_filter = _program_filter(source)
        page = 1
        expected_total = expected_pages = None
        raw_events = []
        while expected_pages is None or page <= expected_pages:
            response = self.client.get(_page_url(seen_date, end_date, page, program_filter))
            if self.artifacts is not None:
                self.artifacts.save_response(
                    "program_search_api_json", "brooklyn-museum-programs-page-{}.json".format(page), response)
            payload = _payload(response)
            total, pages, data = _validate_page(payload, page)
            if expected_total is None:
                expected_total, expected_pages = total, pages
            elif (total, pages) != (expected_total, expected_pages):
                raise ParseError("Brooklyn Museum API pagination metadata changed between pages")
            raw_events.extend((item, response) for item in data)
            page += 1

        if expected_total == 0:
            return source_result(
                state="empty_verified", method=METHOD, recipe_version=PARSER_VERSION,
                started_at=started, finished_at=_now_iso(),
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail=("Brooklyn Museum search API explicitly returned total 0 for {} "
                        "in {} through {}.").format(program_filter, seen_date, end_date),
            )
        if len(raw_events) != expected_total:
            raise ParseError("Brooklyn Museum API returned {} event(s), expected {}".format(
                len(raw_events), expected_total))

        events, rejections, parse_errors = [], [], []
        seen = set()
        for raw, response in raw_events:
            try:
                if not _matches_filter(raw, program_filter):
                    rejections.append(_rejection("outside_configured_program_filter", raw, source, response))
                    continue
                event = _event(raw, source, response, timezone)
                key = (event["source_event_id"], event["start"])
                if key in seen:
                    raise ParseError("Brooklyn Museum API returned duplicate event occurrence")
                seen.add(key)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if seen_date <= event_date <= end_date:
                    events.append(event)
                else:
                    rejections.append({"reason": "outside_date_window", "raw": event})
            except (ParseError, ValueError, TypeError) as error:
                parse_errors.append(str(error))
                rejections.append(_rejection("event_parse_failed", raw, source, response, error=str(error)))

        filter_error = any(item["reason"] == "outside_configured_program_filter" for item in rejections)
        date_error = (not events and len(rejections) == len(raw_events)
                      and all(item["reason"] == "outside_date_window" for item in rejections))
        failed = bool(parse_errors) or filter_error or date_error
        error = None
        if parse_errors:
            error = "{} event(s) failed structural parsing".format(len(parse_errors))
        elif filter_error:
            error = "search API returned event(s) outside configured program filter"
        elif date_error:
            error = "search API returned only events outside the requested date window"
        return source_result(
            state="validation_failed" if failed else ("ok" if events else "empty_verified"),
            method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("Brooklyn Museum search API returned {} event(s) across {} page(s); {} "
                    "{} event(s) were valid in {} through {}.").format(
                        expected_total, expected_pages, program_filter, len(events), seen_date, end_date),
            error=error,
        )


def _program_filter(source):
    config = source.get("adapter_config")
    value = config.get(FILTER_CONFIG) if isinstance(config, dict) else None
    if value not in {ALL_PROGRAMS, FIRST_SATURDAYS}:
        raise ParseError("Brooklyn Museum source requires adapter_config.{} of {} or {}".format(
            FILTER_CONFIG, ALL_PROGRAMS, FIRST_SATURDAYS))
    return value


def _page_url(start_date, end_date, page, program_filter):
    query = {"type": "event", "startDate": start_date.isoformat(),
             "endDate": end_date.isoformat(), "sortField": "startDate",
             "sortOrder": "asc", "page": page}
    if program_filter == FIRST_SATURDAYS:
        query["subtype"] = FIRST_SATURDAYS_SUBTYPE
    return "{}?{}".format(API_URL, urlencode(query))


def _payload(response):
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise ParseError("Brooklyn Museum search API did not return valid JSON") from error
    if not isinstance(payload, dict):
        raise ParseError("Brooklyn Museum search API response is not an object")
    return payload


def _validate_page(payload, page):
    metadata = payload.get("metadata")
    data = payload.get("data")
    if not isinstance(metadata, dict):
        raise ParseError("Brooklyn Museum search API response is missing metadata object")
    required = ("total", "pages", "pageNumber")
    missing = [key for key in required if key not in metadata]
    if missing:
        raise ParseError("Brooklyn Museum search API metadata is missing {}".format(
            ", ".join(missing)))
    total, pages, page_number = (metadata["total"], metadata["pages"], metadata["pageNumber"])
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in (total, pages, page_number)):
        raise ParseError("Brooklyn Museum search API pagination metadata is invalid")
    if page_number != page:
        raise ParseError("Brooklyn Museum search API returned an unexpected page number")
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ParseError("Brooklyn Museum search API data field is invalid")
    if total == 0 and (pages != 0 or data):
        raise ParseError("Brooklyn Museum search API zero result has inconsistent pagination")
    if total > 0 and (pages < 1 or page > pages):
        raise ParseError("Brooklyn Museum search API pagination is inconsistent")
    return total, pages, data


def _matches_filter(raw, program_filter):
    if raw.get("type") != "event":
        return False
    if program_filter == FIRST_SATURDAYS:
        subtypes = raw.get("subtype")
        return isinstance(subtypes, list) and FIRST_SATURDAYS_SUBTYPE in subtypes
    return True


def _event(raw, source, response, timezone):
    title = _clean(raw.get("title") or "")
    event_url = _absolute_url(raw.get("url"), response.url)
    start = _local_datetime(raw.get("startDate"), timezone, required=True)
    end = _local_datetime(raw.get("endDate"), timezone, required=False)
    event_id = str(raw.get("sourceId") or "").strip() or urlsplit(event_url).path.rstrip("/")
    if not title or not event_id:
        raise ParseError("Brooklyn Museum event is missing title or stable URL")
    subtype = raw.get("subtype")
    if subtype is None:
        subtype = []
    if not isinstance(subtype, list) or any(not isinstance(item, str) for item in subtype):
        raise ParseError("Brooklyn Museum event subtype field is invalid")
    location = raw.get("museumLocation")
    if location is None:
        location = {}
    if not isinstance(location, dict):
        raise ParseError("Brooklyn Museum event museumLocation field is invalid")
    description = _clean(raw.get("description") or raw.get("summary") or "")
    status_value = str(raw.get("status") or "").lower()
    return {
        "title": title, "start": start, "end": end,
        "url": event_url, "signup_url": event_url,
        "host": "Brooklyn Museum", "venue": _clean(location.get("name") or ""),
        "neighborhood": "", "address": "",
        "price": "Free" if "Free" in subtype else "", "is_free": True if "Free" in subtype else None,
        "description": description, "capacity_flag": None,
        "status": "cancelled" if status_value in {"cancelled", "canceled"} else "active",
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": event_id,
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash, "extracted_json": raw,
    }


def _absolute_url(value, base_url):
    value = str(value or "").strip()
    if not value:
        raise ParseError("Brooklyn Museum event is missing url")
    return urljoin(base_url, value)


def _local_datetime(value, timezone, required):
    if not value:
        if required:
            raise ParseError("Brooklyn Museum event is missing startDate")
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as error:
        raise ParseError("Brooklyn Museum event date is invalid") from error
    zone = ZoneInfo(timezone)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    else:
        parsed = parsed.astimezone(zone)
    return parsed.isoformat()


def _rejection(reason, raw, source, response, error=None):
    return {"reason": reason, "raw": {
        "title": raw.get("title"), "url": raw.get("url"), "source_id": source["id"],
        "source_listing_url": source["url"], "source_url": response.url,
        "source_event_id": raw.get("sourceId"), "parser_version": PARSER_VERSION,
        "error": error, "extracted_json": raw,
    }}


def _clean(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
