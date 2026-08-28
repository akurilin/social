"""Procedural adapter for Thursday city calendars rendered in a browser."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "thursday_city_browser"
PARSER_VERSION = "thursday-city-browser-v1"
METHOD = "python_adapter"
HOST = "Thursday"
LISTING_READY_SELECTOR = ".ev-card:not(.ev-skeleton), .events-empty"
DETAIL_READY_SELECTOR = ".edp-layout, .edp-error-page"
EMPTY_PREFIX = "No upcoming events in "
EVENT_STATUS = {
    "https://schema.org/EventCancelled": "cancelled",
    "https://schema.org/EventPostponed": "postponed",
    "https://schema.org/EventRescheduled": "postponed",
    "https://schema.org/EventScheduled": "active",
}
MONTH_FORMAT = "%d %b %Y"
AGE_RANGE = re.compile(r"\b(\d{2})\s*[-–]\s*(\d{2})\b")


@dataclasses.dataclass(frozen=True)
class ListingEvent:
    url: str
    title: str
    date_hint: dt.date
    venue: str
    price: str
    source_event_id: str
    raw: dict


class ThursdayCityAdapter:
    """Read one Thursday city page and its in-window event details."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        events = []
        rejections = []
        structural_failures = 0

        with self.client.rendered_session() as browser:
            listing_response = browser.get(source["url"], LISTING_READY_SELECTOR)
            self._save("rendered_listing_html", "thursday-city.html", listing_response)
            listing_events, explicit_empty = _listing_events(
                listing_response, source, seen_date)
            if explicit_empty:
                return source_result(
                    state="empty_verified", method=METHOD,
                    recipe_version=PARSER_VERSION, started_at=started_at,
                    finished_at=_now_iso(), events=[], rejections=[],
                    artifacts=self._artifacts(),
                    detail="Thursday rendered city page explicitly reported no upcoming events.",
                )

            # Listing dates avoid detail requests outside the inclusive window.
            in_window = []
            for stub in listing_events:
                if seen_date <= stub.date_hint <= window_end:
                    in_window.append(stub)
                else:
                    rejections.append({"reason": "outside_date_window", "raw": stub.raw})

            for stub in in_window:
                response = browser.get(stub.url, DETAIL_READY_SELECTOR)
                self._save(
                    "rendered_event_html",
                    "thursday-event-{}.html".format(stub.source_event_id),
                    response,
                )
                try:
                    events.append(_event(stub, source, response, timezone))
                except (ParseError, TypeError, ValueError) as error:
                    structural_failures += 1
                    rejections.append({"reason": "event_parse_failed", "raw": {
                        "source_id": source["id"],
                        "source_listing_url": source["url"],
                        "source_url": response.url,
                        "source_event_id": stub.source_event_id,
                        "parser_version": PARSER_VERSION,
                        "error": str(error),
                    }})

        error = None
        if structural_failures:
            state = "validation_failed"
            error = "{} event(s) failed structural parsing".format(structural_failures)
        elif events:
            state = "ok"
        else:
            state = "empty_verified"
        detail = (
            "Thursday rendered city page listed {} event(s); {} fell in the local "
            "date window {} through {}."
        ).format(len(listing_events), len(events), seen_date, window_end)
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started_at, finished_at=_now_iso(), events=events,
            rejections=rejections, artifacts=self._artifacts(), detail=detail,
            error=error,
        )

    def _save(self, kind, name, response):
        if self.artifacts is not None:
            self.artifacts.save_response(kind, name, response)

    def _artifacts(self):
        return self.artifacts.items if self.artifacts is not None else []


def _listing_events(response, source, seen_date):
    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select("a.ev-card:not(.ev-skeleton)")
    empty = soup.select_one(".events-empty")
    if cards and empty:
        raise ParseError("rendered city page contains events and an empty signal")
    if empty:
        if not _clean(empty.get_text(" ", strip=True)).startswith(EMPTY_PREFIX):
            raise ParseError("rendered city page empty signal is not recognized")
        return [], True
    if not cards:
        raise ParseError("rendered city page has no event cards or explicit empty signal")

    source_parts = urlsplit(source["url"])
    path_prefix = source_parts.path.rstrip("/") + "/"
    events = []
    seen_ids = set()
    for card in cards:
        title = _child_text(card, ".ev-name")
        when = _child_text(card, ".ev-when")
        venue = _child_text(card, ".ev-venue")
        price = _child_text(card, ".ev-price")
        url = urljoin(response.url, str(card.get("href") or ""))
        parts = urlsplit(url)
        event_id = parts.path.rstrip("/").rsplit("/", 1)[-1]
        if (not title or not when or not event_id or parts.netloc != source_parts.netloc
                or not parts.path.startswith(path_prefix)):
            raise ParseError("rendered city page contains a malformed event card")
        if event_id in seen_ids:
            raise ParseError("rendered city page contains a duplicate event id")
        seen_ids.add(event_id)
        date_hint = _listing_date(when, seen_date)
        raw = {
            "title": title, "when": when, "venue": venue, "price": price,
            "url": url, "source_event_id": event_id,
        }
        events.append(ListingEvent(
            url=url, title=title, date_hint=date_hint, venue=venue,
            price=price, source_event_id=event_id, raw=raw,
        ))
    return events, False


def _event(stub, source, response, timezone):
    soup = BeautifulSoup(response.text, "html.parser")
    payload = _event_json_ld(soup)
    title = _clean(payload.get("name") or "")
    if not title or title != stub.title:
        raise ParseError("event detail title does not match its listing card")
    start = _local_datetime(payload.get("startDate"), timezone)
    end = _local_datetime(payload.get("endDate"), timezone, required=False)
    if dt.datetime.fromisoformat(start).date() != stub.date_hint:
        raise ParseError("event detail date does not match its listing card")
    location = payload.get("location") or {}
    if not isinstance(location, dict):
        raise ParseError("event JSON-LD location is invalid")
    venue = _text(soup, ".edp-venue-name") or _clean(location.get("name") or "")
    address = _text(soup, ".edp-venue-addr")
    description = _clean(payload.get("description") or "")
    if not venue or not description:
        raise ParseError("event detail is missing venue or description")
    age_label, age_min, age_max = _age_range(soup)
    status = _status(payload.get("eventStatus"))
    event_url = response.url
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": event_url,
        "signup_url": event_url,
        "host": HOST,
        "venue": venue,
        "neighborhood": "",
        "address": address,
        "price": stub.price,
        "is_free": _is_free(stub.price),
        "description": description,
        "capacity_flag": _capacity_flag(payload.get("offers")),
        "status": status,
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": stub.source_event_id,
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": {
            "listing": stub.raw,
            "json_ld": payload,
            "dom": {"venue": venue, "address": address, "age": age_label},
        },
        "explicit_age_min": age_min,
        "explicit_age_max": age_max,
        "explicit_age_label": age_label,
    }


def _event_json_ld(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except json.JSONDecodeError:
            continue
        for item in _json_ld_items(payload):
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if "Event" in types:
                if not item.get("startDate"):
                    raise ParseError("event JSON-LD is missing startDate")
                return item
    raise ParseError("rendered event detail has no Event JSON-LD")


def _json_ld_items(payload):
    if isinstance(payload, list):
        for item in payload:
            yield from _json_ld_items(item)
    elif isinstance(payload, dict):
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from _json_ld_items(item)
        yield payload


def _listing_date(value, seen_date):
    match = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})\b", value)
    if not match:
        raise ParseError("event card has an unrecognized date")
    try:
        candidate = dt.datetime.strptime(
            "{} {} {}".format(match.group(1), match.group(2), seen_date.year),
            MONTH_FORMAT,
        ).date()
    except ValueError as error:
        raise ParseError("event card has an invalid date") from error
    # The page lists upcoming events, so a past month belongs to the next year.
    if candidate < seen_date:
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate


def _local_datetime(value, timezone, required=True):
    if not value:
        if required:
            raise ParseError("event JSON-LD is missing a required datetime")
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ParseError("event JSON-LD datetime has no UTC offset")
    return parsed.astimezone(ZoneInfo(timezone)).isoformat()


def _status(value):
    if value in EVENT_STATUS:
        return EVENT_STATUS[value]
    raise ParseError("event JSON-LD status is missing or unsupported")


def _age_range(soup):
    for item in soup.select(".edp-image-meta-item"):
        text = _clean(item.get_text(" ", strip=True))
        if not text.lower().startswith("age restrictions:"):
            continue
        label = text.split(":", 1)[1].strip()
        match = AGE_RANGE.search(label)
        if match:
            return label, int(match.group(1)), int(match.group(2))
        return label, None, None
    return None, None, None


def _capacity_flag(offers):
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None
    availability = str(offers.get("availability") or "")
    if availability.endswith("SoldOut"):
        return "Sold out"
    if availability.endswith("LimitedAvailability"):
        return "Limited availability"
    return None


def _is_free(price):
    normalized = re.sub(r"[\s.,$€£]", "", price).lower()
    if not normalized:
        return None
    return normalized in {"free", "from0usd", "0usd", "0"}


def _child_text(node, selector):
    child = node.select_one(selector)
    return _clean(child.get_text(" ", strip=True)) if child else ""


def _text(soup, selector):
    node = soup.select_one(selector)
    return _clean(node.get_text(" ", strip=True)) if node else ""


def _clean(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
