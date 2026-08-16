"""Procedural adapter for Sugary's public event-list API."""

from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "sugary_events_api"
PARSER_VERSION = "sugary-events-api-v1"
METHOD = "python_adapter"
DEFAULT_API_URL = "https://sweetlist-production.up.railway.app/api/events"


class SugaryAdapter:
    """Read Sugary's public complete event collection and filter it locally."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        endpoint = str(source.get("api_url") or DEFAULT_API_URL).strip()
        raw_events, responses = [], []
        expected_total = None
        page = 1
        while True:
            response = self.client.get(_page_url(endpoint, page))
            if self.artifacts is not None:
                self.artifacts.save_response(
                    "events_api_json", "sugary-events-page-{}.json".format(page), response)
            payload = _payload(response, page)
            pagination = payload["pagination"]
            if expected_total is None:
                expected_total = pagination["total"]
            elif pagination["total"] != expected_total:
                raise ParseError("Sugary pagination total changed between pages")
            raw_events.extend(payload["events"])
            responses.extend([response] * len(payload["events"]))
            if not pagination["hasMore"]:
                break
            page += 1
            if page > pagination["totalPages"] or page > 100:
                raise ParseError("Sugary pagination is inconsistent")
        if len(raw_events) != expected_total:
            raise ParseError("Sugary API returned {} event(s), expected {}".format(
                len(raw_events), expected_total))

        events, rejections = [], []
        for raw, response in zip(raw_events, responses):
            if not isinstance(raw, dict):
                rejections.append({"reason": "event_parse_failed", "raw": raw})
                continue
            if raw.get("is_active") is False:
                rejections.append(_rejection("inactive", raw, source, response))
                continue
            try:
                event = _event(raw, source, response, timezone)
            except (ParseError, TypeError, ValueError) as error:
                rejections.append(_rejection("event_parse_failed", raw, source, response,
                                             error=str(error)))
                continue
            event_date = dt.datetime.fromisoformat(event["start"]).date()
            if not seen_date <= event_date <= window_end:
                rejections.append({"reason": "outside_date_window", "raw": event})
            else:
                events.append(event)

        parse_failed = any(item["reason"] == "event_parse_failed" for item in rejections)
        state = ("validation_failed" if parse_failed else "ok" if events else
                 "empty_verified" if not raw_events else "empty_suspicious")
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started_at, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("Sugary public API returned {} event(s); {} event(s) were active "
                    "and inside {} through {} across {} page(s).").format(
                        len(raw_events), len(events), seen_date, window_end, page),
            error="one or more Sugary API events could not be parsed" if parse_failed else None,
        )


def _page_url(endpoint, page):
    parsed = urlsplit(endpoint)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key not in {"page", "limit"}]
    query.extend((("page", str(page)), ("limit", "100")))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _payload(response, requested_page):
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise ParseError("Sugary API did not return valid JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ParseError("Sugary API response is missing events list")
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        raise ParseError("Sugary API response is missing pagination")
    required = ("page", "limit", "total", "totalPages", "hasMore")
    if any(key not in pagination for key in required):
        raise ParseError("Sugary pagination is incomplete")
    for key in ("page", "limit", "total", "totalPages"):
        if isinstance(pagination[key], bool) or not isinstance(pagination[key], int):
            raise ParseError("Sugary pagination {} is invalid".format(key))
    if not isinstance(pagination["hasMore"], bool) or pagination["page"] != requested_page \
            or pagination["limit"] < 1 or pagination["total"] < 0 \
            or pagination["totalPages"] < 0 \
            or (pagination["total"] > 0 and pagination["totalPages"] < 1):
        raise ParseError("Sugary pagination is inconsistent")
    if pagination["total"] == 0 and (payload["events"] or pagination["hasMore"]):
        raise ParseError("Sugary empty collection has inconsistent pagination")
    return payload


def _event(raw, source, response, timezone):
    event_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or raw.get("event_name") or "").strip()
    date_value = raw.get("event_date") or raw.get("date")
    time_value = raw.get("event_time") or raw.get("time")
    signup_url = str(raw.get("rsvp_url") or raw.get("rsvp_link") or "").strip()
    if not event_id or not title or not date_value or not time_value or not signup_url:
        raise ParseError("event is missing id, title, date, time, or RSVP URL")
    start = _local_start(date_value, time_value, timezone)
    cost = str(raw.get("cost_type") or raw.get("price_range") or "").strip()
    is_free = True if cost.lower() == "free" else False if cost else None
    neighborhood = str(raw.get("neighborhood") or "").strip()
    borough = str(raw.get("borough") or "").strip()
    venue = str(raw.get("venue") or "").strip()
    return {
        "title": title, "start": start, "end": None,
        "url": urljoin(response.url, signup_url),
        "signup_url": urljoin(response.url, signup_url),
        "host": "Sugary", "venue": venue, "neighborhood": neighborhood,
        "address": ", ".join(part for part in (venue, neighborhood, borough) if part),
        "price": cost, "is_free": is_free,
        "description": _clean(raw.get("description") or raw.get("one_liner") or ""),
        "capacity_flag": None, "status": "active",
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": event_id,
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash, "extracted_json": raw,
    }


def _local_start(date_value, time_value, timezone):
    try:
        event_date = dt.date.fromisoformat(str(date_value)[:10])
    except ValueError as error:
        raise ParseError("event date is invalid") from error
    text = re.sub(r"\s+", " ", str(time_value).strip().upper())
    for pattern in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            event_time = dt.datetime.strptime(text, pattern).time()
            return dt.datetime.combine(event_date, event_time,
                                       tzinfo=ZoneInfo(timezone)).isoformat()
        except ValueError:
            pass
    raise ParseError("event time is invalid")


def _clean(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def _rejection(reason, raw, source, response, error=None):
    return {"reason": reason, "raw": {
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": raw.get("id"),
        "parser_version": PARSER_VERSION, "error": error, "extracted_json": raw,
    }}


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
