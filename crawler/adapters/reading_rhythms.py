"""Procedural adapter for the Reading Rhythms NYC event calendar."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import FetchError, ParseError, source_result
from crawler.jsonld import HtmlDocument, find_typed_nodes, load_json_ld


ADAPTER_ID = "reading_rhythms"
PARSER_VERSION = "reading-rhythms-html-jsonld-v1"
METHOD = "python_adapter"
NYC_CITY_ID = "new-york-city"
EVENT_PATH_PREFIX = "/events/"
SPONSORED_CHAPTER = "new-york-sponsored"
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
MONTHS = {name: number for number, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
     "OCT", "NOV", "DEC"), 1)}
CARD_DATE = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})\b", re.IGNORECASE)
CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$"}


@dataclasses.dataclass(frozen=True)
class ListingStub:
    url: str
    title: str
    event_id: str
    chapter: str
    price: str
    sold_out: bool
    date_hint: dt.date


class ReadingRhythmsAdapter:
    """Read NYC cards, then use Event JSON-LD on each detail page."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        listing_url = urldefrag(source["url"])[0]
        listing = self.client.get(listing_url)
        if self.artifacts is not None:
            self.artifacts.save_response(
                "listing_html", "reading-rhythms-events.html", listing)
        stubs, empty = _listing_stubs(listing.text, listing.url, seen_date)
        if empty:
            return source_result(
                state="empty_verified",
                method=METHOD,
                recipe_version=PARSER_VERSION,
                started_at=started_at,
                finished_at=_now_iso(),
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail="Reading Rhythms reported zero NYC events.",
            )

        events = []
        rejections = []
        failures = []
        detail_sample_saved = False
        in_window = [stub for stub in stubs
                     if seen_date <= stub.date_hint <= window_end]
        for stub in stubs:
            if stub not in in_window:
                rejections.append(_listing_rejection(
                    "outside_date_window", stub, source, listing))

        # Card dates let the crawler avoid detail requests outside the window.
        for stub in in_window:
            try:
                detail = self.client.get(stub.url)
                if not detail_sample_saved and self.artifacts is not None:
                    self.artifacts.save_response(
                        "detail_html", "reading-rhythms-detail-sample.html", detail)
                    detail_sample_saved = True
                event = _event(detail, stub, source, timezone)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if not seen_date <= event_date <= window_end:
                    rejections.append({
                        "reason": "outside_date_window",
                        "raw": event,
                    })
                elif event["status"] != "active":
                    rejections.append({
                        "reason": event["status"],
                        "raw": event,
                    })
                else:
                    events.append(event)
            except (FetchError, ParseError, TypeError, ValueError) as error:
                failures.append("{}: {}".format(stub.url, error))
                rejections.append({
                    "reason": "detail_parse_failed",
                    "raw": {
                        "title": stub.title,
                        "url": stub.url,
                        "source_id": source["id"],
                        "source_event_id": stub.event_id,
                        "parser_version": PARSER_VERSION,
                        "error": str(error),
                    },
                })

        state = "validation_failed" if failures else (
            "ok" if events else "empty_verified")
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
                "Reading Rhythms listed {} ordered NYC event(s); {} active "
                "event(s) fell in the inclusive date window {} through {}."
            ).format(len(stubs), len(events), seen_date, window_end),
            error="; ".join(failures[:5]) or None,
        )


class _ListingParser(HTMLParser):
    """Collect the NYC count and cards without depending on visual rendering."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.city_seen = False
        self.city_has_events = None
        self.city_count = None
        self.section_seen = False
        self.cards = []
        self._button_depth = None
        self._count_depth = None
        self._count_parts = []
        self._section_depth = None
        self._card_depth = None
        self._card = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        element_depth = self.depth + 1
        if tag not in VOID_TAGS:
            self.depth = element_depth
        classes = set(values.get("class", "").split())
        if tag == "button" and values.get("data-city-id") == NYC_CITY_ID:
            self.city_seen = True
            self.city_has_events = values.get("data-has-events") == "true"
            self._button_depth = element_depth
        if (self._button_depth is not None and tag == "span"
                and "pill-count" in classes):
            self._count_depth = element_depth
            self._count_parts = []
        if tag == "div" and values.get("data-city-events") == NYC_CITY_ID:
            self.section_seen = True
            self._section_depth = element_depth
        if (self._section_depth is not None and tag == "a"
                and "event-card" in classes):
            self._card_depth = element_depth
            self._card = {"attrs": values, "text": []}

    def handle_endtag(self, tag):
        if tag == "a" and self._card_depth == self.depth:
            self.cards.append(self._card)
            self._card_depth = None
            self._card = None
        if tag == "span" and self._count_depth == self.depth:
            value = "".join(self._count_parts).strip()
            self.city_count = int(value) if value.isdigit() else None
            self._count_depth = None
        if tag == "button" and self._button_depth == self.depth:
            self._button_depth = None
        if tag == "div" and self._section_depth == self.depth:
            self._section_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        value = " ".join(data.split())
        if not value:
            return
        if self._count_depth is not None:
            self._count_parts.append(value)
        if self._card is not None:
            self._card["text"].append(value)


def _listing_stubs(text, base_url, seen_date):
    parser = _ListingParser()
    parser.feed(text)
    parser.close()
    if not parser.city_seen or parser.city_has_events is None:
        raise ParseError("listing is missing the New York City status button")
    if parser.city_count is None:
        raise ParseError("New York City status button is missing its event count")
    if not parser.city_has_events:
        if parser.city_count != 0:
            raise ParseError("New York City empty status has a nonzero count")
        return [], True
    if not parser.section_seen:
        raise ParseError("listing is missing the New York City event section")
    if parser.city_count != len(parser.cards):
        raise ParseError(
            "New York City count does not match its event cards")

    stubs = []
    ids = set()
    urls = set()
    for card in parser.cards:
        attrs = card["attrs"]
        event_id = str(attrs.get("data-event-id") or "").strip()
        title = str(attrs.get("data-event-title") or "").strip()
        href = str(attrs.get("href") or "").strip()
        date_match = CARD_DATE.search(" ".join(card["text"]))
        if not event_id or not title or not href or date_match is None:
            raise ParseError("New York City event card is missing id, title, URL, or date")
        url = _event_url(href, base_url)
        if event_id in ids or url in urls:
            raise ParseError("New York City event cards contain a duplicate id or URL")
        ids.add(event_id)
        urls.add(url)
        month = MONTHS[date_match.group(1).upper()]
        date_hint = _date_hint(month, int(date_match.group(2)), seen_date)
        stubs.append(ListingStub(
            url=url,
            title=title,
            event_id=event_id,
            chapter=str(attrs.get("data-event-chapter") or "").strip(),
            price=str(attrs.get("data-event-price") or "").strip(),
            sold_out=str(attrs.get("data-event-sold-out") or "").lower() == "true",
            date_hint=date_hint,
        ))
    if any(current.date_hint < previous.date_hint
           for previous, current in zip(stubs, stubs[1:])):
        raise ParseError("New York City event cards are not in chronological order")
    return stubs, False


def _event_url(href, base_url):
    url = urljoin(base_url, href)
    parsed = urlsplit(url)
    base = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or parsed.netloc != base.netloc
            or not parsed.path.startswith(EVENT_PATH_PREFIX)):
        raise ParseError("event card has an invalid detail URL")
    return url


def _date_hint(month, day, seen_date):
    year = seen_date.year
    if month - seen_date.month > 6:
        year -= 1
    elif seen_date.month - month > 6:
        year += 1
    try:
        return dt.date(year, month, day)
    except ValueError as error:
        raise ParseError("event card has an invalid date") from error


def _event(response, stub, source, timezone):
    document = HtmlDocument(response.text)
    nodes = []
    for value in load_json_ld(document):
        nodes.extend(find_typed_nodes(value, "Event"))
    if len(nodes) != 1:
        raise ParseError("detail page must contain one Event JSON-LD node")
    raw = nodes[0]
    title = str(raw.get("name") or "").strip()
    if not title:
        raise ParseError("Event JSON-LD is missing name")
    start = _local_datetime(raw.get("startDate"), timezone)
    end = (_local_datetime(raw.get("endDate"), timezone)
           if raw.get("endDate") else None)
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    offer = _offer(raw.get("offers"))
    signup_url = str(offer.get("url") or response.url).strip()
    if not _absolute_http_url(signup_url):
        signup_url = response.url
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": response.url,
        "signup_url": signup_url,
        "host": _host(raw),
        "venue": str(location.get("name") or "").strip(),
        "neighborhood": _neighborhood(stub.chapter),
        "address": _address(location.get("address")),
        "price": stub.price or _price(offer),
        "is_free": _is_free(stub.price, offer),
        "description": str(raw.get("description") or "").strip(),
        "capacity_flag": _capacity_flag(stub, offer),
        "status": _status(raw.get("eventStatus")),
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": stub.event_id,
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": {
            "listing": {
                **dataclasses.asdict(stub),
                "date_hint": stub.date_hint.isoformat(),
            },
            "event": raw,
        },
    }


def _local_datetime(value, timezone):
    if not value:
        raise ParseError("Event JSON-LD is missing a date")
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ParseError("Event JSON-LD has an invalid date") from error
    zone = ZoneInfo(timezone)
    return (parsed.replace(tzinfo=zone) if parsed.tzinfo is None
            else parsed.astimezone(zone)).isoformat()


def _offer(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), {})
    return {}


def _host(raw):
    for key in ("performer", "organizer"):
        value = raw.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                return str(item["name"]).strip()
    return "Reading Rhythms"


def _address(value):
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    state_postal = " ".join(str(value.get(key) or "").strip()
                            for key in ("addressRegion", "postalCode")).strip()
    parts = [str(value.get(key) or "").strip()
             for key in ("streetAddress", "addressLocality")]
    parts.extend((state_postal, str(value.get("addressCountry") or "").strip()))
    return ", ".join(part for part in parts if part)


def _neighborhood(chapter):
    if chapter == SPONSORED_CHAPTER:
        return "Sponsored"
    return chapter.replace("-", " ").title()


def _price(offer):
    value = offer.get("price")
    if value in (None, ""):
        return ""
    currency = str(offer.get("priceCurrency") or "").upper()
    symbol = CURRENCY_SYMBOLS.get(currency, currency + " " if currency else "")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "{}{}".format(symbol, value)
    amount = str(int(number)) if number.is_integer() else "{:.2f}".format(number)
    return "{}{}".format(symbol, amount)


def _is_free(listing_price, offer):
    normalized = listing_price.strip().lower()
    if normalized:
        return normalized in {"$0", "free"}
    try:
        return float(offer.get("price")) == 0
    except (TypeError, ValueError):
        return None


def _capacity_flag(stub, offer):
    availability = str(offer.get("availability") or "").rstrip("/").rsplit("/", 1)[-1]
    return "sold_out" if stub.sold_out or availability == "SoldOut" else None


def _status(value):
    status = str(value or "").rstrip("/").rsplit("/", 1)[-1]
    if status == "EventCancelled":
        return "cancelled"
    if status == "EventPostponed":
        return "postponed"
    return "active"


def _absolute_http_url(value):
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _listing_rejection(reason, stub, source, response):
    return {"reason": reason, "raw": {
        "title": stub.title,
        "url": stub.url,
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_event_id": stub.event_id,
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "start_date_hint": stub.date_hint.isoformat(),
    }}


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
