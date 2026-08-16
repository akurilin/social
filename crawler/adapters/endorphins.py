"""Procedural adapter for the Endorphins city JSON API."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "endorphins_city_api"
PARSER_VERSION = "endorphins-city-api-v1"
METHOD = "python_adapter"
CITY_ID = re.compile(r"/city/([A-Za-z0-9_-]+)(?:/|$)")


class EndorphinsAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        window_start = seen_date
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        endpoint = _api_url(source["url"])
        response = self.client.get(endpoint)
        if self.artifacts is not None:
            self.artifacts.save_response("city_api_json", "endorphins-city.json", response)
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as error:
            raise ParseError("city API did not return valid JSON") from error

        city = payload.get("city") if isinstance(payload, dict) else None
        if not isinstance(city, dict):
            raise ParseError("city API response is missing city object")
        events = city.get("events")
        if not isinstance(events, list):
            raise ParseError("city API response is missing events list")
        city_id = _city_id(source["url"])
        if not city_id or str(city.get("_id") or "") != city_id:
            raise ParseError("city API payload does not match configured city id")
        events_out = []
        rejections = []
        active_count = 0
        structural_failures = 0
        for raw in events:
            if not isinstance(raw, dict):
                structural_failures += 1
                rejections.append({"reason": "invalid_event", "raw": raw})
                continue
            if not _belongs_to_city(raw, city_id):
                rejections.append(_rejection("outside_configured_city", raw, source, endpoint))
                continue
            if raw.get("deleted") or raw.get("cancelled"):
                rejections.append(_rejection(
                    "cancelled_or_deleted", raw, source, endpoint,
                ))
                continue
            active_count += 1
            try:
                event = _event(raw, source, endpoint, timezone, response)
            except (ParseError, ValueError) as error:
                structural_failures += 1
                rejections.append(_rejection(
                    "event_parse_failed", raw, source, endpoint, error=str(error),
                ))
                continue
            event_date = dt.datetime.fromisoformat(event["start"]).date()
            if not window_start <= event_date <= window_end:
                rejections.append({"reason": "outside_date_window", "raw": event})
                continue
            events_out.append(event)

        structural_error = (
            "{} event(s) failed structural parsing".format(structural_failures)
            if structural_failures else None
        )
        if structural_failures:
            state = "validation_failed"
        elif events_out:
            state = "ok"
        else:
            state = "empty_verified"
        detail = (
            "Endorphins city API returned {} active {} events; {} fell in the "
            "configured local date window {} through {}."
        ).format(active_count, city.get("name") or "city", len(events_out), window_start, window_end)
        return source_result(
            state=state,
            method=METHOD,
            recipe_version=PARSER_VERSION,
            started_at=started_at,
            finished_at=_now_iso(),
            events=events_out,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=detail,
            error=structural_error,
        )


def _api_url(seed_url):
    city_id = _city_id(seed_url)
    if not city_id:
        raise ParseError("source URL does not contain an Endorphins city id")
    parsed = urlsplit(seed_url)
    return "{}://{}/api/city/{}".format(parsed.scheme, parsed.netloc, city_id)


def _city_id(url):
    match = CITY_ID.search(urlsplit(url).path)
    return match.group(1) if match else None


def _belongs_to_city(event, city_id):
    event_city = event.get("city")
    return isinstance(event_city, dict) and str(event_city.get("_id") or "") == city_id


def _event(raw, source, endpoint, timezone, response):
    event_id = str(raw.get("_id") or "").strip()
    title = str(raw.get("title") or "").strip()
    start_value = raw.get("startTime") or raw.get("startTime_utc")
    if not event_id or not title or not start_value:
        raise ParseError("event is missing _id, title, or startTime")
    start = _local_datetime(start_value, timezone)
    location = str(raw.get("locationString") or raw.get("formattedAddress") or "").strip()
    venue = _venue_name(location)
    counts = raw.get("rsvpCounts") if isinstance(raw.get("rsvpCounts"), dict) else {}
    host = _host(raw)
    event_url = "https://app.endorphinsrunning.com/event/{}".format(event_id)
    return {
        "title": title,
        "start": start,
        "end": None,
        "url": event_url,
        "signup_url": event_url,
        "host": host,
        "venue": venue,
        "neighborhood": "",
        "address": location,
        "price": "Free",
        "is_free": True,
        "description": _description(raw.get("text") or ""),
        "capacity_flag": _capacity_flag(counts),
        "status": "cancelled" if raw.get("cancelled") else "active",
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": endpoint,
        "source_event_id": event_id,
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": raw,
    }


def _local_datetime(value, timezone):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    zone = ZoneInfo(timezone)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    else:
        parsed = parsed.astimezone(zone)
    return parsed.isoformat()


def _venue_name(location):
    return location.split(",", 1)[0].strip() if location else ""


def _host(raw):
    hosts = raw.get("hosts")
    if isinstance(hosts, list) and hosts:
        names = []
        for host in hosts:
            if isinstance(host, dict):
                name = " ".join(str(host.get(key) or "").strip() for key in ("firstName", "lastName")).strip()
                if name:
                    names.append(name)
        if names:
            return ", ".join(names)
    city = raw.get("city")
    return "Endorphins {}".format(city.get("name")) if isinstance(city, dict) and city.get("name") else "Endorphins"


def _description(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(value)))).strip()


def _capacity_flag(counts):
    if not counts:
        return None
    labels = (("confirmed", "confirmed"), ("maybe", "maybe"), ("waitlist", "waitlist"))
    values = ["{} {}".format(counts[key], label) for key, label in labels if counts.get(key) is not None]
    return "; ".join(values)


def _rejection(reason, raw, source, endpoint, error=None):
    return {"reason": reason, "raw": {
        "title": raw.get("title"), "url": _event_url(raw), "source_id": source["id"],
        "source_url": endpoint, "source_event_id": raw.get("_id"), "error": error,
        "parser_version": PARSER_VERSION,
    }}


def _event_url(raw):
    return "https://app.endorphinsrunning.com/event/{}".format(raw.get("_id")) if raw.get("_id") else ""


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
