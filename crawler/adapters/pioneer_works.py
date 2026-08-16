"""Procedural adapter for the Pioneer Works Next.js calendar."""

from __future__ import annotations

import datetime as dt
import json
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import FetchError, ParseError, source_result


ADAPTER_ID = "pioneer_works_next_data"
PARSER_VERSION = "pioneer-works-next-data-v1"
METHOD = "python_adapter"
NEXT_DATA = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.I | re.S)
SUPPORTED_TYPES = {"program", "class"}


class PioneerWorksAdapter:
    """Read calendar records before fetching details for dated events only."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        listing = self.client.get(source["url"])
        if self.artifacts is not None:
            self.artifacts.save_response("calendar_next_data_html", "pioneer-works-calendar.html", listing)
        records = _calendar_records(listing)
        stubs, rejections, filtered_out = [], [], 0
        for record in records:
            if not isinstance(record, dict):
                rejections.append({"reason": "listing_event_parse_failed", "raw": record})
                continue
            if record.get("_type") not in SUPPORTED_TYPES:
                # Exhibitions and other unsupported calendar material are not
                # events in this adapter's factual contract.
                continue
            if record.get("hideFromPage") is True:
                continue
            try:
                start_date = _listing_start_date(record)
            except ParseError as error:
                # The calendar also carries permanent, undated program records.
                # They are not event facts and must not make a dated crawl fail.
                if record.get("calendar") is None or _ended_before_window(record, seen_date):
                    continue
                rejections.append(_listing_rejection(record, source, listing, str(error)))
                continue
            if not seen_date <= start_date <= window_end:
                filtered_out += 1
                continue
            if not str(record["calendar"].get("startTime") or "").strip():
                # A continuous or masked-date listing is not a factual timed
                # event. Do not guess a start or fetch its detail page.
                filtered_out += 1
                continue
            try:
                stubs.append(_stub(record, listing.url, start_date))
            except ParseError as error:
                rejections.append(_listing_rejection(record, source, listing, str(error)))

        events, detail_errors = [], []
        seen_ids = set()
        for stub in stubs:
            if stub["id"] in seen_ids:
                detail_errors.append("duplicate calendar record {}".format(stub["id"]))
                rejections.append({"reason": "duplicate_source_event_id", "raw": _stub_raw(
                    stub, source, listing)})
                continue
            seen_ids.add(stub["id"])
            try:
                detail = self.client.get(stub["url"])
                if self.artifacts is not None:
                    self.artifacts.save_response(
                        "detail_next_data_html", "pioneer-works-{}.html".format(stub["id"]), detail)
                event = _event(_detail_record(detail), stub, source, detail, timezone)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if not seen_date <= event_date <= window_end:
                    rejections.append({"reason": "outside_date_window", "raw": event})
                else:
                    events.append(event)
            except (FetchError, ParseError, TypeError, ValueError) as error:
                detail_errors.append("{}: {}".format(stub["url"], error))
                rejections.append({"reason": "detail_parse_failed", "raw": _stub_raw(
                    stub, source, listing, error=str(error))})

        listing_errors = any(item["reason"] == "listing_event_parse_failed"
                             for item in rejections)
        if listing_errors or detail_errors:
            state = "validation_failed"
        elif events:
            state = "ok"
        else:
            # The complete embedded calendar collection was downloaded and
            # every supported timed record was checked against the window.
            state = "empty_verified"
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started_at, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("Pioneer Works Next.js calendar had {} record(s); selected {} "
                    "program/class record(s) in {} through {} before detail fetches; "
                    "{} other dated record(s) were filtered locally; {} event(s) passed "
                    "detail validation.").format(
                        len(records), len(stubs), seen_date, window_end, filtered_out, len(events)),
            error="; ".join(detail_errors[:5]) or (
                "one or more calendar records could not be parsed" if listing_errors else None),
        )


def _calendar_records(response):
    payload = _next_data(response.text, "calendar")
    try:
        records = payload["props"]["pageProps"]["events"]
    except (KeyError, TypeError) as error:
        raise ParseError("Pioneer Works calendar NEXT_DATA is missing pageProps.events") from error
    if not isinstance(records, list):
        raise ParseError("Pioneer Works calendar events is not a list")
    return records


def _detail_record(response):
    payload = _next_data(response.text, "detail")
    try:
        record = payload["props"]["pageProps"]["data"]
    except (KeyError, TypeError) as error:
        raise ParseError("Pioneer Works detail NEXT_DATA is missing pageProps.data") from error
    if not isinstance(record, dict):
        raise ParseError("Pioneer Works detail data is not an object")
    return record


def _next_data(text, label):
    match = NEXT_DATA.search(text)
    if not match:
        raise ParseError("Pioneer Works {} page has no __NEXT_DATA__ script".format(label))
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ParseError("Pioneer Works {} NEXT_DATA is invalid JSON".format(label)) from error
    if not isinstance(payload, dict):
        raise ParseError("Pioneer Works {} NEXT_DATA is not an object".format(label))
    return payload


def _listing_start_date(record):
    calendar = record.get("calendar")
    if not isinstance(calendar, dict):
        raise ParseError("calendar record is missing calendar")
    return _date(calendar.get("startDate"))


def _ended_before_window(record, seen_date):
    calendar = record.get("calendar")
    if not isinstance(calendar, dict) or not calendar.get("endDate"):
        return False
    try:
        return _date(calendar["endDate"]) < seen_date
    except ParseError:
        return False


def _stub(record, base_url, start_date):
    event_id = str(record.get("_id") or "").strip()
    kind = str(record.get("_type") or "").strip()
    title = str(record.get("title") or "").strip()
    slug_data = record.get("slug")
    slug = str(slug_data.get("current") or "").strip() if isinstance(slug_data, dict) else ""
    calendar = record.get("calendar")
    if not event_id or kind not in SUPPORTED_TYPES or not title or not slug or not isinstance(calendar, dict):
        raise ParseError("calendar record is missing id, type, title, slug, or calendar")
    return {
        "id": event_id, "kind": kind, "title": title, "slug": slug, "date": start_date,
        "url": urljoin(base_url, "/{}/{}".format("classes" if kind == "class" else "programs", slug)),
        "record": record,
    }


def _event(record, stub, source, response, timezone):
    if str(record.get("_id") or "").strip() != stub["id"]:
        raise ParseError("detail record id does not match calendar record")
    if record.get("_type") != stub["kind"]:
        raise ParseError("detail record type does not match calendar record")
    title = str(record.get("title") or "").strip()
    calendar = record.get("calendar")
    if not title or not isinstance(calendar, dict):
        raise ParseError("detail record is missing title or calendar")
    start = _calendar_datetime(calendar.get("startDate"), calendar.get("startTime"), timezone)
    end = _calendar_datetime(calendar.get("endDate"), calendar.get("endTime"), timezone,
                             required=False)
    page_url = response.url
    box_office = record.get("boxOffice") if isinstance(record.get("boxOffice"), dict) else {}
    eventbrite_id = str(box_office.get("eventbriteId") or box_office.get("apiId") or "").strip()
    signup_url = "https://www.eventbrite.com/e/{}".format(eventbrite_id) if eventbrite_id else page_url
    return {
        "title": title, "start": start, "end": end, "url": page_url, "signup_url": signup_url,
        "host": "Pioneer Works", "venue": "Pioneer Works", "neighborhood": "Red Hook",
        "address": "159 Pioneer Street, Brooklyn, NY 11231",
        "price": _price(record), "is_free": _is_free(record),
        "description": _portable_text(record.get("body")), "capacity_flag": None,
        "status": "cancelled" if record.get("isCancelled") or "[cancelled]" in title.lower()
        else "active",
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": stub["id"],
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash, "extracted_json": record,
    }


def _date(value):
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as error:
        raise ParseError("calendar startDate is invalid") from error


def _calendar_datetime(date_value, time_value, timezone, required=True):
    if not date_value or not time_value:
        if required:
            raise ParseError("calendar date or time is missing")
        return None
    try:
        value = dt.datetime.fromisoformat("{}T{}".format(date_value, time_value))
    except ValueError as error:
        raise ParseError("calendar datetime is invalid") from error
    return value.replace(tzinfo=ZoneInfo(timezone)).isoformat()


def _portable_text(value):
    if not isinstance(value, list):
        return ""
    parts = []
    for block in value:
        if not isinstance(block, dict):
            continue
        children = block.get("children")
        if isinstance(children, list):
            parts.extend(str(child.get("text") or "") for child in children
                         if isinstance(child, dict))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _price(record):
    text = _portable_text(record.get("body"))
    match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
    return "${}".format(match.group(1).replace(",", "")) if match else ""


def _is_free(record):
    stated_price = _price(record)
    if stated_price:
        return stated_price == "$0"
    box_office = record.get("boxOffice") if isinstance(record.get("boxOffice"), dict) else {}
    settings = box_office.get("createEventbriteEvent")
    if isinstance(settings, dict) and str(settings.get("kind") or "").lower() == "free":
        return True
    return False if _price(record) else None


def _stub_raw(stub, source, response, error=None):
    return {
        "title": stub["title"], "url": stub["url"], "source_id": source["id"],
        "source_listing_url": source["url"], "source_url": response.url,
        "source_event_id": stub["id"], "start_date_hint": stub["date"].isoformat(),
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash, "error": error, "extracted_json": stub["record"],
    }


def _listing_rejection(record, source, response, error):
    return {"reason": "listing_event_parse_failed", "raw": {
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "parser_version": PARSER_VERSION,
        "fetched_at": response.fetched_at, "content_hash": response.content_hash,
        "error": error, "extracted_json": record,
    }}


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
