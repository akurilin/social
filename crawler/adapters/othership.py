"""Procedural adapter for Othership's New York event calendar."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import FetchError, ParseError, source_result
from crawler.jsonld import HtmlDocument, find_typed_nodes, load_json_ld


ADAPTER_ID = "othership_events"
PARSER_VERSION = "othership-webflow-events-v1"
METHOD = "python_adapter"
HOST = "Othership"
EVENT_PATH_PREFIX = "/events/"
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
MONTHS = {name: number for number, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
     "OCT", "NOV", "DEC"), 1)}
NYC_VENUES = {
    "Flatiron": {
        "venue": "Othership Flatiron",
        "neighborhood": "Flatiron",
        "address": "23 W 20th St, New York, NY",
    },
    "Williamsburg": {
        "venue": "Othership Williamsburg",
        "neighborhood": "Williamsburg",
        "address": "25 Kent Ave, Brooklyn, NY",
    },
}
TORONTO_LOCATIONS = {"Adelaide", "Yorkville"}
TIME_RANGE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}\s*[AP]M)\s*-\s*"
    r"(?P<end>\d{1,2}:\d{2}\s*[AP]M)\s*(?P<zone>EDT|EST)\b",
    re.IGNORECASE,
)
AGE_MINIMUM = re.compile(
    r"minimum\s+age\s+requirement\D{0,40}(\d{1,2})\b",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class ListingStub:
    url: str
    signup_url: str
    title: str
    description: str
    location: str
    date_hint: dt.date


class OthershipEventsAdapter:
    """Read static Webflow cards and their public detail pages."""

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
            self.artifacts.save_response(
                "listing_html", "othership-events.html", listing)

        stubs, empty = _listing_stubs(listing.text, listing.url, seen_date)
        if empty:
            return source_result(
                state="empty_verified",
                method=METHOD,
                recipe_version=PARSER_VERSION,
                started_at=started_at,
                finished_at=_now_iso(),
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail="Othership's events collection reported no events.",
            )

        events = []
        rejections = []
        failures = []
        in_window = []
        for stub in stubs:
            if stub.location in TORONTO_LOCATIONS:
                rejections.append(_listing_rejection(
                    "outside_geo", stub, source, listing))
            elif stub.location not in NYC_VENUES:
                raise ParseError(
                    "event card has an unsupported location: {}".format(
                        stub.location))
            elif not seen_date <= stub.date_hint <= window_end:
                rejections.append(_listing_rejection(
                    "outside_date_window", stub, source, listing))
            else:
                in_window.append(stub)

        # Card dates prevent detail requests for Toronto and out-of-window events.
        detail_sample_saved = False
        for stub in in_window:
            try:
                detail = self.client.get(stub.url)
                if not detail_sample_saved and self.artifacts is not None:
                    self.artifacts.save_response(
                        "detail_html", "othership-detail-sample.html", detail)
                    detail_sample_saved = True
                event = _event(detail, stub, source, timezone)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if event_date != stub.date_hint:
                    raise ParseError(
                        "detail date {} does not match card date {}".format(
                            event_date, stub.date_hint))
                events.append(event)
            except (FetchError, ParseError, TypeError, ValueError) as error:
                failures.append("{}: {}".format(stub.url, error))
                rejections.append({
                    "reason": "detail_parse_failed",
                    "raw": {
                        "title": stub.title,
                        "url": stub.url,
                        "source_id": source["id"],
                        "source_event_id": _event_id(stub.url),
                        "parser_version": PARSER_VERSION,
                        "error": str(error),
                    },
                })

        if failures:
            state = "validation_failed"
        elif events:
            state = "ok"
        elif max(stub.date_hint for stub in stubs) >= window_end:
            state = "empty_verified"
        else:
            state = "empty_suspicious"

        return source_result(
            state=state,
            method=METHOD,
            recipe_version=PARSER_VERSION,
            started_at=started_at,
            finished_at=_now_iso(),
            events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=(
                "Othership listed {} ordered event(s); {} New York event(s) "
                "fell in the inclusive date window {} through {}."
            ).format(len(stubs), len(events), seen_date, window_end),
            error="; ".join(failures[:5]) or None,
        )


class _ListingParser(HTMLParser):
    """Collect Webflow event cards inside the events collection only."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.wrapper_seen = False
        self.list_seen = False
        self.empty_seen = False
        self.cards = []
        self._card_depth = None
        self._card = None
        self._capture = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        element_depth = self.depth + 1
        if tag not in VOID_TAGS:
            self.depth = element_depth
        classes = set(values.get("class", "").split())

        if "events-list-wrapper" in classes:
            self.wrapper_seen = True
        if not self.wrapper_seen:
            return
        if "events-list" in classes:
            self.list_seen = True
        if "w-dyn-empty" in classes:
            self.empty_seen = True
        if "events-item" in classes:
            if self._card is not None:
                raise ParseError("event cards must not be nested")
            self._card_depth = element_depth
            self._card = {}
        if self._card is None:
            return

        href = str(values.get("href") or "").strip()
        if tag == "a" and "event-info-top" in classes:
            self._card["href"] = href
        elif tag == "a" and "/schedule" in href:
            self._card["signup_url"] = href

        key = None
        if "event-date-number" in classes:
            key = "day"
        elif "event-date-month" in classes:
            key = "month"
        elif "events-location-text" in classes:
            key = "location"
        elif {"subtitle", "is-events", "events"}.issubset(classes):
            key = "title"
        elif {"text-size-medium", "text-weight-normal"}.issubset(classes):
            key = "description"
        if key is not None and self._capture is None:
            self._capture = {"key": key, "depth": element_depth, "parts": []}

    def handle_endtag(self, tag):
        if (self._capture is not None
                and self._capture["depth"] == self.depth):
            self._card[self._capture["key"]] = _clean_parts(
                self._capture["parts"])
            self._capture = None
        if self._card_depth == self.depth:
            self.cards.append(self._card)
            self._card_depth = None
            self._card = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self._capture is not None:
            self._capture["parts"].append(data)


class _DetailParser(HTMLParser):
    """Collect the visible time, full description, and booking URL."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.time_text = ""
        self.description = ""
        self.signup_url = ""
        self._capture = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        element_depth = self.depth + 1
        if tag not in VOID_TAGS:
            self.depth = element_depth
        classes = set(values.get("class", "").split())
        href = str(values.get("href") or "").strip()
        if tag == "a" and not self.signup_url and "/schedule" in href:
            self.signup_url = href
        if self._capture is not None:
            return
        if {"flex-horizontal", "is-events-custom"}.issubset(classes):
            self._capture = {"key": "time_text", "depth": element_depth,
                             "parts": []}
        elif "event-agenda-rich-text" in classes:
            self._capture = {"key": "description", "depth": element_depth,
                             "parts": []}

    def handle_endtag(self, tag):
        if (self._capture is not None
                and self._capture["depth"] == self.depth):
            setattr(self, self._capture["key"], _clean_parts(
                self._capture["parts"]))
            self._capture = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self._capture is not None:
            self._capture["parts"].append(data)


def _listing_stubs(text, base_url, seen_date):
    parser = _ListingParser()
    parser.feed(text)
    parser.close()
    if not parser.wrapper_seen:
        raise ParseError("listing is missing the events collection")
    if not parser.cards:
        if parser.empty_seen:
            return [], True
        raise ParseError("events collection has no cards or explicit empty state")
    if not parser.list_seen:
        raise ParseError("events collection is missing its event list")
    if parser.empty_seen:
        raise ParseError("events collection has cards and an empty state")

    stubs = []
    urls = set()
    for card in parser.cards:
        required = ("href", "signup_url", "title", "location", "day", "month")
        missing = [key for key in required if not str(card.get(key) or "").strip()]
        if missing:
            raise ParseError(
                "event card is missing {}".format(", ".join(missing)))
        url = _event_url(card["href"], base_url)
        signup_url = _signup_url(card["signup_url"], base_url)
        if url in urls:
            raise ParseError("events collection contains a duplicate URL")
        urls.add(url)
        month_name = str(card["month"]).upper()
        if month_name not in MONTHS:
            raise ParseError("event card has an invalid month")
        try:
            day = int(card["day"])
            date_hint = _date_hint(MONTHS[month_name], day, seen_date)
        except (TypeError, ValueError) as error:
            raise ParseError("event card has an invalid date") from error
        stubs.append(ListingStub(
            url=url,
            signup_url=signup_url,
            title=card["title"],
            description=card.get("description", ""),
            location=card["location"],
            date_hint=date_hint,
        ))
    if any(current.date_hint < previous.date_hint
           for previous, current in zip(stubs, stubs[1:])):
        raise ParseError("event cards are not in chronological order")
    return stubs, False


def _event(response, stub, source, timezone):
    document = HtmlDocument(response.text)
    nodes = []
    for value in load_json_ld(document):
        nodes.extend(find_typed_nodes(value, "Event"))
    matching = [node for node in nodes
                if _same_event_url(node.get("url"), response.url)]
    if len(matching) != 1:
        raise ParseError(
            "detail page must contain one matching Event JSON-LD node")
    raw = matching[0]
    title = str(raw.get("name") or "").strip()
    venue = str((raw.get("location") or {}).get("name") or "").strip() \
        if isinstance(raw.get("location"), dict) else ""
    if not title or not venue:
        raise ParseError("Event JSON-LD is missing a title or venue")
    if title != stub.title:
        raise ParseError("detail title does not match its event card")

    venue_facts = NYC_VENUES[stub.location]
    if venue != venue_facts["venue"]:
        raise ParseError("detail venue does not match its event card")
    event_date = _event_date(raw.get("startDate"))
    detail = _DetailParser(response.text)
    start, end = _event_times(event_date, detail.time_text, timezone)
    signup_url = _signup_url(
        detail.signup_url or stub.signup_url, response.url)
    age_match = AGE_MINIMUM.search(document.text)
    age_min = int(age_match.group(1)) if age_match else None
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": response.url,
        "signup_url": signup_url,
        "host": HOST,
        "venue": venue,
        "neighborhood": venue_facts["neighborhood"],
        "address": venue_facts["address"],
        "price": "",
        "is_free": None,
        "description": detail.description or str(raw.get("description") or "").strip(),
        "capacity_flag": None,
        "status": _status(raw.get("eventStatus")),
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": _event_id(response.url),
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": {
            "listing": {
                **dataclasses.asdict(stub),
                "date_hint": stub.date_hint.isoformat(),
            },
            "event": raw,
            "visible_time": detail.time_text,
        },
        "explicit_age_min": age_min,
        "explicit_age_max": None,
        "explicit_age_label": "{}+".format(age_min) if age_min else None,
    }


def _event_url(href, base_url):
    url = urljoin(base_url, href)
    parsed = urlsplit(url)
    base = urlsplit(base_url)
    if (parsed.scheme != "https" or parsed.netloc != base.netloc
            or not parsed.path.startswith(EVENT_PATH_PREFIX)
            or parsed.path.rstrip("/") == EVENT_PATH_PREFIX.rstrip("/")):
        raise ParseError("event card has an invalid detail URL")
    return url


def _signup_url(href, base_url):
    url = urljoin(base_url, href)
    parsed = urlsplit(url)
    base = urlsplit(base_url)
    if (parsed.scheme != "https" or parsed.netloc != base.netloc
            or parsed.path != "/schedule"):
        raise ParseError("event card has an invalid booking URL")
    return url


def _same_event_url(raw_url, response_url):
    if not isinstance(raw_url, str):
        return False
    left = urlsplit(raw_url)
    right = urlsplit(response_url)
    return (left.netloc.lower().removeprefix("www.") ==
            right.netloc.lower().removeprefix("www.") and
            left.path.rstrip("/") == right.path.rstrip("/"))


def _date_hint(month, day, seen_date):
    year = seen_date.year
    if month - seen_date.month > 6:
        year -= 1
    elif seen_date.month - month > 6:
        year += 1
    return dt.date(year, month, day)


def _event_date(value):
    if not isinstance(value, str) or not value.strip():
        raise ParseError("Event JSON-LD is missing a date")
    normalized = value.strip()
    try:
        return dt.date.fromisoformat(normalized)
    except ValueError:
        pass
    try:
        return dt.datetime.strptime(normalized, "%b %d, %Y").date()
    except ValueError as error:
        raise ParseError("Event JSON-LD has an invalid date") from error


def _event_times(event_date, text, timezone):
    match = TIME_RANGE.search(text)
    if match is None:
        raise ParseError("detail page is missing its event time range")
    zone = ZoneInfo(timezone)
    start_time = dt.datetime.strptime(
        match.group("start").upper(), "%I:%M %p").time()
    end_time = dt.datetime.strptime(
        match.group("end").upper(), "%I:%M %p").time()
    start = dt.datetime.combine(event_date, start_time, tzinfo=zone)
    end = dt.datetime.combine(event_date, end_time, tzinfo=zone)
    if end <= start:
        end += dt.timedelta(days=1)
    abbreviation = match.group("zone").upper()
    if start.tzname() != abbreviation or end.tzname() != abbreviation:
        raise ParseError("detail time-zone label does not match the event date")
    return start.isoformat(), end.isoformat()


def _status(value):
    status = str(value or "").rstrip("/").rsplit("/", 1)[-1]
    if status == "EventCancelled":
        return "cancelled"
    if status == "EventPostponed":
        return "postponed"
    return "active"


def _event_id(url):
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


def _listing_rejection(reason, stub, source, response):
    return {"reason": reason, "raw": {
        "title": stub.title,
        "url": stub.url,
        "location": stub.location,
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_event_id": _event_id(stub.url),
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "start_date_hint": stub.date_hint.isoformat(),
    }}


def _clean_parts(parts):
    return " ".join(" ".join(parts).split())


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
