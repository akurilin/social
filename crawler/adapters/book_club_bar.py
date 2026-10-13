"""Read Book Club Bar public API events, with a rendered fallback parser."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from uuid import uuid4
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "book_club_bar_browser"
PARSER_VERSION = "book-club-bar-api-v2"
METHOD = "python_adapter"
API_BASE = "https://api.bookmanager.com/customer/"
PAGE_SIZE = 20
MAX_PAGES = 100
PUBLIC_FIELDS = ("id", "title", "description", "summary", "date", "start_time",
                 "end_time", "end_date", "all_day", "location_text", "recurring_as_single_row")
HOST = "Book Club Bar"
TIME_FORMAT = "%I:%M %p"
CARD_SELECTOR = 'h4[aria-label="Click to view event"]'
READY_SELECTOR = CARD_SELECTOR + ', #main-content .ant-empty-description'
DATE_PATTERN = re.compile(r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) "
                          r"[A-Z][a-z]{2} \d{1,2}(?:st|nd|rd|th), \d{4}\b")
TIME_PATTERN = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M$")
BOOKING_LABEL = re.compile(r"\b(?:tickets?|register|reserve|rsvp)\b", re.I)
EMPTY_TEXT = {"No upcoming events", "No events found"}


class BookClubBarAdapter:
    """Keep event dates, locations, and booking links from the visible list."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        listing = self.client.get(source["url"])
        match = re.search(r'\bvar\s+san\s*=\s*["\'](\d+)["\']', listing.text)
        if not match:
            raise ParseError("Book Club Bar page has no public store identifier")
        common = {"uuid": str(uuid4()), "log_url": urlsplit(source["url"]).path}
        settings, _ = self._post("store/getSettings", {**common, "webstore_name": match[1]})
        if settings.get("using_events_v2") is not True:
            raise ParseError("Book Club Bar does not expose the expected v2 calendar")
        store = settings.get("store_info")
        if not isinstance(store, dict) or not store.get("id"):
            raise ParseError("Book Club Bar settings have no store ID")
        common["store_id"] = store["id"]
        session, _ = self._post("session/get", {**common, "referrer": source["url"]})
        if not isinstance(session.get("session_id"), str) or not session["session_id"]:
            raise ParseError("Book Club Bar anonymous session is missing")
        # Session data is used in memory only; never save the responses or request fields.
        common["session_id"] = session["session_id"]
        events, rejections, seen = [], [], set()
        offset = 0
        for _ in range(MAX_PAGES):
            payload, response = self._post("event/v2/list", {
                **common, "start_date": seen_date.strftime("%Y%m%d"),
                "end_date": window_end.strftime("%Y%m%d"),
                "offset": offset, "limit": PAGE_SIZE,
            })
            rows = payload.get("rows")
            if not isinstance(rows, list):
                raise ParseError("Book Club Bar API has no rows list")
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    raise ParseError("Book Club Bar API event has no ID")
                key = (str(row["id"]), str(row.get("date")))
                if key in seen:
                    raise ParseError("Book Club Bar API returned a duplicate event")
                seen.add(key)
                evidence = {key: row.get(key) for key in PUBLIC_FIELDS}
                try:
                    event = _api_event(evidence, source, response, timezone)
                    day = dt.datetime.fromisoformat(event["start"]).date()
                    if not seen_date <= day <= window_end:
                        rejections.append({"reason": "outside_date_window", "raw": evidence})
                    elif event["status"] != "active":
                        rejections.append({"reason": event["status"], "raw": evidence})
                    else:
                        events.append(event)
                except (ParseError, ValueError, TypeError) as error:
                    rejections.append({"reason": "event_parse_failed",
                                       "raw": {**evidence, "error": str(error)}})
            # Request the next offset even after a short page to prove exhaustion.
            offset += len(rows)
        else:
            raise ParseError("Book Club Bar API exceeded the page limit")
        failed = any(item["reason"] == "event_parse_failed" for item in rejections)
        return source_result(
            state="validation_failed" if failed else "ok" if events else "empty_verified",
            method=METHOD, recipe_version=PARSER_VERSION, started_at=started,
            finished_at=_now(), events=events, rejections=rejections, artifacts=[],
            detail="Book Club Bar API returned {} rows; {} events matched {} through {}.".format(
                offset, len(events), seen_date, window_end),
            error="One or more API events could not be parsed" if failed else None,
        )

    def _post(self, route, fields):
        response = self.client.post_form(API_BASE + route, fields)
        try:
            payload = json.loads(response.text)
        except ValueError as error:
            raise ParseError("Book Club Bar API returned invalid JSON") from error
        if not isinstance(payload, dict) or payload.get("error"):
            raise ParseError("Book Club Bar API request failed")
        return payload, response

    def crawl_rendered(self, source, seen_date, lookahead_days, timezone):
        started = _now()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        # Keep the permitted rendered website as a separate fallback path.
        with self.client.rendered_session() as browser:
            response = browser.get(source["url"], READY_SELECTOR)
        if self.artifacts is not None:
            self.artifacts.save_response(
                "rendered_listing_html", "book-club-bar-events.html", response)
        soup = BeautifulSoup(response.text, "html.parser")
        main = soup.select_one("#main-content")
        if main is None:
            raise ParseError("Book Club Bar is missing its main event container")
        headings = main.select(CARD_SELECTOR)
        empty = main.select_one(".ant-empty-description")
        explicit_empty = empty is not None and _text(empty) in EMPTY_TEXT
        if headings and explicit_empty:
            raise ParseError("Book Club Bar lists events and an empty signal")

        events, rejections, dates, failures = [], [], [], []
        seen_ids = set()
        for heading in headings:
            raw = {"title": _text(heading), "card_text": _text(heading.parent)}
            try:
                event = _event(heading, source, response, timezone)
                day = dt.datetime.fromisoformat(event["start"]).date()
                if dates and day < dates[-1]:
                    raise ParseError("Book Club Bar cards are not in date order")
                dates.append(day)
                if event["source_event_id"] in seen_ids:
                    raise ParseError("Book Club Bar contains a duplicate event card")
                seen_ids.add(event["source_event_id"])
                if not seen_date <= day <= window_end:
                    rejections.append({"reason": "outside_date_window", "raw": raw})
                elif event["status"] != "active":
                    rejections.append({"reason": event["status"], "raw": raw})
                else:
                    events.append(event)
            except (ParseError, ValueError, TypeError) as error:
                failures.append(str(error))
                rejections.append({"reason": "event_parse_failed", "raw": {
                    **raw, "error": str(error), "source_url": response.url,
                }})

        # A card strictly after the window proves that infinite-scroll truncation
        # did not omit another event on the inclusive final day.
        complete = explicit_empty or bool(dates and max(dates) > window_end)
        error = "; ".join(failures[:5]) or None
        if failures:
            state = "validation_failed"
        elif not complete:
            state = "validation_failed" if events else "empty_suspicious"
            error = "Rendered cards do not prove complete coverage of the date window"
        else:
            state = "ok" if events else "empty_verified"
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail="Book Club Bar listed {} cards; {} events matched {} through {}.".format(
                len(headings), len(events), seen_date, window_end),
            error=error,
        )


def _event(heading, source, response, timezone):
    card = heading.parent
    title = _text(heading)
    schedule = card.select_one('div[aria-label="Click to view event"]')
    if not title or schedule is None:
        raise ParseError("Event card is missing its title or schedule")
    date_match = DATE_PATTERN.search(_text(schedule))
    times = [part for part in schedule.stripped_strings if TIME_PATTERN.fullmatch(part)]
    location_icon = schedule.select_one('[aria-label="environment"]')
    if date_match is None or len(times) != 2 or location_icon is None:
        raise ParseError("Event card is missing its date, time range, or location")
    date_text = re.sub(r"(\d)(st|nd|rd|th)", r"\1", date_match.group())
    day = dt.datetime.strptime(date_text, "%A %b %d, %Y").date()
    zone = ZoneInfo(timezone)
    normalized_times = [re.sub(r"\s*([AP]M)$", r" \1", value) for value in times]
    start = dt.datetime.combine(day, dt.datetime.strptime(normalized_times[0], TIME_FORMAT).time(), zone)
    end = dt.datetime.combine(day, dt.datetime.strptime(normalized_times[1], TIME_FORMAT).time(), zone)
    if end <= start:
        end += dt.timedelta(days=1)
    location = _text(location_icon.parent)
    if not location:
        raise ParseError("Event card has an empty location")
    description_node = card.select_one('[class*="bm-lnkin-"]')
    description = _text(description_node) if description_node is not None else ""
    signup = ""
    for link in card.select("a[href]"):
        candidate = str(link.get("href") or "").strip()
        parts = urlsplit(candidate)
        if BOOKING_LABEL.search(_text(link)) and parts.scheme in ("http", "https") and parts.netloc:
            signup = candidate
            break
    neighborhood = ""
    if "troutman" in location.lower() or "bushwick" in location.lower():
        neighborhood = "Bushwick"
    elif "east village" in location.lower():
        neighborhood = "East Village"
    venue = "{} {}".format(HOST, neighborhood) if neighborhood else location.split(",", 1)[0]
    is_free = True if re.search(r"\b(?:this (?:is a|event is)|event:) free\b", description, re.I) else None
    if is_free is None and re.search(r"\b(?:ticketed event|what your ticket includes|admission:)\b", description, re.I):
        is_free = False
    price = "Free" if is_free else ""
    if re.search(r"admission:\s*one drink", description, re.I):
        price = "One drink"
        is_free = False
    status = "cancelled" if re.search(r"\bcancelled\b|\bcanceled\b", title, re.I) else (
        "postponed" if re.search(r"\bpostponed\b", title, re.I) else "active")
    identity = "|".join((title.casefold(), start.isoformat(), location.casefold()))
    # Cards have click handlers, not detail hrefs. Stable factual IDs avoid
    # treating two events at different venues as one listing URL.
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    return {
        "title": title, "start": start.isoformat(), "end": end.isoformat(),
        "url": signup or source["url"], "signup_url": signup,
        "host": HOST, "venue": venue, "neighborhood": neighborhood,
        "address": location, "price": price, "is_free": is_free,
        "description": description,
        "capacity_flag": "private event" if re.search(r"\bprivate event\b", title, re.I) else None,
        "status": status,
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": event_id,
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": {"title": title, "date": date_match.group(),
                           "times": times, "location": location,
                           "description": description, "signup_url": signup},
    }


def _api_event(raw, source, response, timezone):
    """Use explicit local clocks and keep only public event evidence."""
    if raw.get("all_day") is not False:
        raise ParseError("Book Club Bar event has no explicit timed schedule")
    day = dt.datetime.strptime(str(raw.get("date")), "%Y%m%d").date()
    end_day = dt.datetime.strptime(raw["end_date"], "%Y%m%d").date() if raw.get("end_date") else day
    start = dt.datetime.combine(day, dt.time.fromisoformat(raw["start_time"]), ZoneInfo(timezone))
    end = dt.datetime.combine(end_day, dt.time.fromisoformat(raw["end_time"]), ZoneInfo(timezone))
    if end <= start and end_day == day:
        end += dt.timedelta(days=1)
    if end <= start:
        raise ParseError("Book Club Bar event ends before it starts")
    title = str(raw.get("title") or "").strip()
    location = str(raw.get("location_text") or "").strip()
    if not title or not location:
        raise ParseError("Book Club Bar event has no title or location")
    copy = BeautifulSoup(str(raw.get("description") or raw.get("summary") or ""), "html.parser")
    description = _text(copy)
    url = source["url"].rstrip("/") + "/" + str(raw["id"])
    signup = url
    for link in copy.select("a[href]"):
        candidate = str(link["href"])
        if BOOKING_LABEL.search(_text(link)) and urlsplit(candidate).scheme in {"http", "https"}:
            signup = candidate
            break
    neighborhood = "Bushwick" if re.search("troutman|bushwick", location, re.I) else (
        "East Village" if "east village" in location.lower() else "")
    free = True if re.search(r"\b(?:this (?:is a|event is)|event:) free\b", description, re.I) else None
    price = "Free" if free else ""
    if re.search(r"admission:\s*one drink", description, re.I):
        price, free = "One drink", False
    status = "cancelled" if re.search(r"\bcancelled\b|\bcanceled\b", title, re.I) else (
        "postponed" if re.search(r"\bpostponed\b", title, re.I) else "active")
    evidence = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    # Retain the rendered adapter's identity so existing stored events still match.
    identity = "|".join((title.casefold(), start.isoformat(), location.casefold()))
    event_id = hashlib.sha256(identity.encode()).hexdigest()
    return {
        "title": title, "start": start.isoformat(), "end": end.isoformat(),
        "url": url, "signup_url": signup, "host": HOST,
        "venue": HOST + " " + neighborhood if neighborhood else location.split(",", 1)[0],
        "neighborhood": neighborhood, "address": location, "price": price, "is_free": free,
        "description": description, "capacity_flag": "private event" if re.search(r"\bprivate event\b", title, re.I) else None,
        "status": status, "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": event_id,
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": hashlib.sha256(evidence.encode()).hexdigest(), "extracted_json": raw,
    }


def _text(node):
    return " ".join(node.get_text(" ", strip=True).split())


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
