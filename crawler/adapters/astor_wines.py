"""Procedural adapter for Astor Wines' server-rendered tasting calendar."""

from __future__ import annotations

import datetime as dt
import html
import re
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import EventStub, FetchError, ParseError, source_result
from crawler.jsonld import HtmlDocument


ADAPTER_ID = "astor_wines_tastings"
PARSER_VERSION = "astor-wines-tasting-html-v1"
METHOD = "python_adapter"
_MONTH = ("January|February|March|April|May|June|July|August|September|October|November|December|"
          "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec")
_DATE = re.compile(
    rf"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    rf"({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?(?:\s+(20\d{{2}}))?\s+"
    rf"(\d{{1,2}})(?::(\d{{2}}))?\s*(AM|PM)", re.I)
_NUM_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\s+(\d{1,2})(?::(\d{2}))?\s*(AM|PM)", re.I)
_DATE_ONLY = re.compile(
    rf"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    rf"({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?(?:\s+(20\d{{2}}))?\b", re.I)
_EVENT_HREF = re.compile(r"(?:https?://[^\"']+)?/event\.aspx\?[^\"'<> ]*\beid=[^\"'&<> ]+", re.I)


class AstorWinesAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now_iso()
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        listing = self.client.get(source["url"])
        if self.artifacts is not None:
            self.artifacts.save_response("listing_html", "listing.html", listing)
        stubs = _listing_stubs(listing.text, listing.url, seen_date)
        if not stubs:
            if _explicit_empty(listing.text):
                return source_result(state="empty_verified", method=METHOD,
                    recipe_version=PARSER_VERSION, started_at=started, finished_at=_now_iso(),
                    artifacts=self.artifacts.items if self.artifacts else [],
                    detail="Astor Wines tasting calendar explicitly reported no upcoming tastings.")
            raise ParseError("Astor Wines calendar had no event.aspx links or explicit empty signal")

        events, rejections, errors = [], [], []
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        seen = set()
        for stub in stubs:
            if stub.url in seen:
                continue
            seen.add(stub.url)
            if stub.date_hint and not seen_date <= stub.date_hint <= window_end:
                rejections.append({"reason": "outside_date_window", "raw": {
                    "title": stub.title, "url": stub.url, "source_id": source["id"],
                    "source_listing_url": source["url"], "start_date_hint": stub.date_hint.isoformat(),
                    "fetched_at": listing.fetched_at, "listing_content_hash": listing.content_hash,
                    "parser_version": PARSER_VERSION}})
                continue
            try:
                detail = self.client.get(stub.url)
                if len(events) == 0 and self.artifacts is not None:
                    self.artifacts.save_response("detail_html", "detail-sample.html", detail)
                event = _parse_event(detail, stub, source, timezone, seen_date)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if not seen_date <= event_date <= end_date:
                    rejections.append({"reason": "outside_date_window", "raw": event})
                else:
                    events.append(event)
            except FetchError:
                raise
            except (ParseError, TypeError, ValueError) as error:
                errors.append(f"{stub.url}: {error}")
                rejections.append({"reason": "event_parse_failed", "raw": {
                    "title": stub.title, "url": stub.url, "source_id": source["id"],
                    "source_listing_url": source["url"], "parser_version": PARSER_VERSION,
                    "error": str(error)}})
        state = "ok" if events and not errors else ("validation_failed" if errors else "empty_verified")
        return source_result(state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now_iso(), events=events, rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts else [],
            detail=f"Parsed Astor Wines tasting calendar: {len(seen)} event URL(s), {len(events)} event(s).",
            error="; ".join(errors[:5]) or None)


def _listing_stubs(text, base_url, seen_date):
    result = []
    for match in _EVENT_HREF.finditer(text):
        url = urljoin(base_url, html.unescape(match.group(0)))
        nearby = _strip_html(text[max(0, match.start() - 700):match.end() + 700])
        before = _strip_html(text[max(0, match.start() - 350):match.start()])
        title = _title_from_html(text[max(0, match.start() - 350):match.end() + 350])
        date_match = (_last_match(_DATE, before) or _last_match(_NUM_DATE, before)
                      or _last_match(_DATE_ONLY, before))
        if date_match is None:
            date_match = (_DATE.search(nearby) or _NUM_DATE.search(nearby)
                          or _DATE_ONLY.search(nearby))
        date_hint = _parse_date(date_match.group(0), seen_date) if date_match else None
        result.append(EventStub(url=url, title=title, date_hint=date_hint))
    return result


def _last_match(pattern, text):
    matches = list(pattern.finditer(text))
    return matches[-1] if matches else None


def _parse_event(response, stub, source, timezone, seen_date):
    document = HtmlDocument(response.text)
    visible = document.text
    title = _title_from_html(response.text) or stub.title
    start_value = _DATE.search(visible) or _NUM_DATE.search(visible)
    if not title or not start_value:
        raise ParseError("tasting detail is missing title or authoritative date/time")
    start = _local_datetime(start_value, timezone, seen_date)
    end = None
    end_match = re.search(r"(?:-|–|to)\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)",
                          visible[start_value.end():], re.I)
    if end_match:
        end = _local_time(dt.datetime.fromisoformat(start).date(), end_match, timezone, 1).isoformat()
    address = _address(visible)
    is_free = bool(re.search(r"\bfree\b", visible, re.I))
    status = "cancelled" if re.search(r"\bcancel(?:led|ed)\b", visible, re.I) else "active"
    lowered = visible.lower()
    capacity = "sold_out" if "sold out" in lowered else None
    external_id = parse_qs(urlsplit(response.url).query).get("eid", [""])[0]
    if not external_id:
        external_id = parse_qs(urlsplit(stub.url).query).get("eid", [""])[0]
    return {"title": title, "start": start, "end": end, "url": response.url,
        "signup_url": response.url, "host": "Astor Wines & Spirits",
        "venue": "Astor Wines & Spirits", "neighborhood": "NoHo", "address": address,
        "price": "Free" if is_free else "", "is_free": is_free,
        "description": _description(visible, title), "capacity_flag": capacity, "status": status,
        "source_id": source["id"], "source_listing_url": source["url"], "source_url": response.url,
        "source_event_id": external_id, "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION, "content_hash": response.content_hash,
        "extracted_json": {"title": title, "date_text": start_value.group(0)}}


def _title_from_html(text):
    for tag in ("h1", "h2", "h3", "title"):
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.I | re.S)
        if match:
            value = _strip_html(match.group(1)).strip()
            if value and "astor wines" not in value.lower():
                return value
    return ""


def _parse_date(text, seen_date):
    match = _DATE.search(text) or _NUM_DATE.search(text) or _DATE_ONLY.search(text)
    if not match:
        return None
    try:
        if match.re is _NUM_DATE:
            return dt.date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
        if match.re is _DATE_ONLY:
            month = dt.datetime.strptime(match.group(1)[:3], "%b").month
            year = int(match.group(3)) if match.group(3) else seen_date.year
            if not match.group(3):
                if month - seen_date.month > 6:
                    year -= 1
                elif seen_date.month - month > 6:
                    year += 1
            return dt.date(year, month, int(match.group(2)))
        month = dt.datetime.strptime(match.group(1)[:3], "%b").month
        year = int(match.group(3)) if match.group(3) else seen_date.year
        if not match.group(3):
            # A yearless calendar entry belongs to the nearest occurrence of
            # that month relative to the crawl date, with a six-month boundary.
            if month - seen_date.month > 6:
                year -= 1
            elif seen_date.month - month > 6:
                year += 1
        return dt.date(year, month, int(match.group(2)))
    except ValueError:
        return None


def _local_datetime(match, timezone, seen_date):
    date = _parse_date(match.group(0), seen_date)
    if not date:
        raise ParseError("invalid date")
    return _local_time(date, match, timezone, 4).isoformat()


def _local_time(date, match, timezone, group):
    hour = int(match.group(group)); minute = int(match.group(group + 1) or 0)
    meridiem = match.group(group + 2).upper()
    if meridiem == "PM" and hour != 12: hour += 12
    if meridiem == "AM" and hour == 12: hour = 0
    return dt.datetime.combine(date, dt.time(hour, minute), ZoneInfo(timezone))


def _address(text):
    match = re.search(r"399\s+Lafayette\s+(?:Street|St\.?)[^\n]*", text, re.I)
    return match.group(0).strip(" .") if match else "399 Lafayette St, New York, NY"


def _description(text, title):
    value = text.replace(title, "", 1)
    return re.sub(r"\s+", " ", value).strip()[:2000]


def _explicit_empty(text):
    return bool(re.search(r"no\s+(?:upcoming\s+)?(?:tastings?|events?)|currently\s+no\s+(?:upcoming\s+)?(?:tastings?|events?)", _strip_html(text), re.I))


def _strip_html(value):
    return re.sub(r"<[^>]+>", " ", html.unescape(value))


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
