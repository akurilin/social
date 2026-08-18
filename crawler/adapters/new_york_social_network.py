"""Procedural adapter for New York Social Network's Event Espresso calendar."""

from __future__ import annotations

import datetime as dt
import html
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import EventStub, FetchError, ParseError, source_result
from crawler.jsonld import HtmlDocument, find_typed_nodes, load_json_ld


ADAPTER_ID = "new_york_social_network"
PARSER_VERSION = "nysn-event-espresso-jsonld-v1"
METHOD = "python_adapter"
HOST = "New York Social Network"

_QUICK_LIST = re.compile(
    r"<p[^>]*>\s*<strong[^>]*>\s*Upcoming\s+Events\s*</strong>\s*</p>"
    r"\s*<pre[^>]*>(.*?)</pre>",
    re.I | re.S,
)
_ANCHOR = re.compile(
    r"<a\b[^>]*href\s*=\s*([\"'])(.*?)\1[^>]*>(.*?)</a>",
    re.I | re.S,
)
_LISTING_DATE = re.compile(
    r"--\s*(\d{2}/\d{2}/20\d{2})\s+\d{1,2}:\d{2}\s*(?:am|pm)\b",
    re.I,
)
_ICAL_ID = re.compile(r"[?&](?:ics_id|event_id)=(\d+)\b", re.I)
_AGE_RANGE = re.compile(r"\bAges?\s+(\d{2})\s*[-–]\s*(\d{2})\b", re.I)
_AGE_MINIMUM = re.compile(
    r"\b(?:Age\s+Requirement|Recommended\s+Ages?)\s*:?\s*(\d{2})\s*\+",
    re.I,
)


class NewYorkSocialNetworkAdapter:
    """Read the complete quick list and public Event JSON-LD detail pages."""

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
                "listing_html", "new-york-social-network-quick-list.html", listing)

        section = _quick_list_section(listing.text)
        if section is None:
            raise ParseError("quick list has no Upcoming Events section")
        stubs = _listing_stubs(section, listing.url)
        if not stubs:
            return source_result(
                state="empty_verified",
                method=METHOD,
                recipe_version=PARSER_VERSION,
                started_at=started_at,
                finished_at=_now_iso(),
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail="The quick-list Upcoming Events section contained no event links.",
            )

        events = []
        rejections = []
        detail_errors = []
        in_window = []
        for stub in _deduplicate_stubs(stubs):
            if not seen_date <= stub.date_hint <= window_end:
                rejections.append({
                    "reason": "outside_date_window",
                    "raw": _stub_raw(stub, source, listing),
                })
                continue
            in_window.append(stub)

        for index, stub in enumerate(in_window, start=1):
            try:
                detail = self.client.get(stub.url)
                if self.artifacts is not None:
                    self.artifacts.save_response(
                        "detail_html", "detail-{:03d}.html".format(index), detail)
                event = _parse_event(detail, stub, source, timezone)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if event_date != stub.date_hint:
                    raise ParseError(
                        "detail date {} does not match quick-list date {}".format(
                            event_date, stub.date_hint))
                events.append(event)
            except (FetchError, ParseError, TypeError, ValueError) as error:
                detail_errors.append("{}: {}".format(stub.url, error))
                rejections.append({
                    "reason": "event_parse_failed",
                    "raw": {
                        "title": stub.title,
                        "url": stub.url,
                        "source_id": source["id"],
                        "source_listing_url": source["url"],
                        "parser_version": PARSER_VERSION,
                        "error": str(error),
                    },
                })

        if detail_errors:
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
                "Parsed {} quick-list event URL(s) and {} event(s) in {} through {}."
            ).format(len(stubs), len(events), seen_date, window_end),
            error="; ".join(detail_errors[:5]) or None,
        )


def _quick_list_section(text):
    match = _QUICK_LIST.search(text)
    return match.group(1) if match else None


def _listing_stubs(section, base_url):
    stubs = []
    for match in _ANCHOR.finditer(section):
        url = urljoin(base_url, html.unescape(match.group(2)).strip())
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or
                parsed.netloc.lower() not in {
                    "newyorksocialnetwork.com", "www.newyorksocialnetwork.com"} or
                not re.fullmatch(r"/events/[^/]+/", parsed.path)):
            continue
        text = _clean_html(match.group(3))
        date_match = _LISTING_DATE.search(text)
        if not date_match:
            raise ParseError("quick-list event link has no explicit full date and time")
        try:
            date_hint = dt.datetime.strptime(
                date_match.group(1), "%m/%d/%Y").date()
        except ValueError as error:
            raise ParseError("quick-list event link has an invalid date") from error
        title = text[:date_match.start()].strip(" -")
        if not title:
            raise ParseError("quick-list event link has no title")
        stubs.append(EventStub(url=url, title=title, date_hint=date_hint))
    return stubs


def _deduplicate_stubs(stubs):
    unique = []
    seen = set()
    for stub in stubs:
        if stub.url in seen:
            continue
        seen.add(stub.url)
        unique.append(stub)
    return unique


def _stub_raw(stub, source, response):
    return {
        "title": stub.title,
        "url": stub.url,
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "start_date_hint": stub.date_hint.isoformat(),
        "fetched_at": response.fetched_at,
        "listing_content_hash": response.content_hash,
        "parser_version": PARSER_VERSION,
    }


def _parse_event(response, stub, source, timezone):
    document = HtmlDocument(response.text)
    nodes = []
    for value in load_json_ld(document):
        nodes.extend(find_typed_nodes(value, "Event"))
    matching = [node for node in nodes if _same_event_url(node.get("url"), response.url)]
    if len(matching) != 1:
        raise ParseError(
            "detail page must contain one Event JSON-LD node for its own URL")
    raw = matching[0]

    title = _clean(raw.get("name"))
    start = _local_datetime(raw.get("startDate"), timezone, required=True)
    end = _local_datetime(raw.get("endDate"), timezone, required=False)
    url = _clean(raw.get("url"))
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    venue = _clean(location.get("name"))
    address = _address(location.get("address"))
    if not title or not start or not url or not venue or not address:
        raise ParseError(
            "Event JSON-LD is missing name, startDate, URL, venue, or address")

    status = _status(raw.get("eventStatus"))
    price, is_free, capacity = _offer_facts(raw.get("offers"))
    description = _clean_html(raw.get("description"))[:4000]
    age_min, age_max, age_label = _age_facts(
        "{} {} {}".format(title, description, document.text))
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": url,
        "signup_url": _signup_url(raw.get("offers"), url),
        "host": HOST,
        "venue": venue,
        "neighborhood": "",
        "address": address,
        "price": price,
        "is_free": is_free,
        "description": description,
        "capacity_flag": capacity,
        "status": status,
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": _source_event_id(response.text, url),
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": raw,
        "explicit_age_min": age_min,
        "explicit_age_max": age_max,
        "explicit_age_label": age_label,
    }


def _same_event_url(raw_url, response_url):
    if not isinstance(raw_url, str):
        return False
    left = urlsplit(raw_url)
    right = urlsplit(response_url)
    return (left.netloc.lower().removeprefix("www.") ==
            right.netloc.lower().removeprefix("www.") and
            left.path.rstrip("/") == right.path.rstrip("/"))


def _local_datetime(value, timezone, required):
    if value in (None, "") and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ParseError("Event JSON-LD has no required datetime")
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ParseError("Event JSON-LD has an invalid datetime") from error
    if parsed.tzinfo is None:
        raise ParseError("Event JSON-LD datetime has no UTC offset")
    return parsed.astimezone(ZoneInfo(timezone)).isoformat()


def _address(value):
    if isinstance(value, str):
        return _clean(value)
    if not isinstance(value, dict):
        return ""
    parts = [
        value.get("streetAddress"),
        value.get("addressLocality"),
        value.get("addressRegion"),
        value.get("postalCode"),
        value.get("addressCountry"),
    ]
    return ", ".join(part for part in (_clean(item) for item in parts) if part)


def _offers(value):
    if isinstance(value, dict):
        return [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _offer_facts(value):
    offers = _offers(value)
    prices = []
    currencies = set()
    availability = []
    for offer in offers:
        try:
            prices.append(Decimal(str(offer.get("price"))))
        except (InvalidOperation, TypeError, ValueError):
            pass
        currency = _clean(offer.get("priceCurrency")).upper()
        if currency:
            currencies.add(currency)
        raw_availability = _clean(offer.get("availability"))
        if raw_availability:
            availability.append(raw_availability.rstrip("/").rsplit("/", 1)[-1].lower())
    prices = sorted(set(prices))
    is_free = all(price == 0 for price in prices) if prices else None
    if is_free:
        price = "Free"
    elif prices:
        currency = next(iter(currencies)) if len(currencies) == 1 else ""
        values = [_decimal_text(item) for item in prices]
        if currency == "USD":
            values = ["$" + item for item in values]
        elif currency:
            values = [item + " " + currency for item in values]
        price = values[0] if len(values) == 1 else "{}–{}".format(values[0], values[-1])
    else:
        price = ""
    capacity = "sold_out" if availability and all(
        item in {"soldout", "sold_out"} for item in availability) else None
    return price, is_free, capacity


def _decimal_text(value):
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def _signup_url(value, fallback):
    for offer in _offers(value):
        url = _clean(offer.get("url"))
        if url:
            return url
    return fallback


def _status(value):
    values = value if isinstance(value, list) else [value]
    names = {_clean(item).rstrip("/").rsplit("/", 1)[-1].lower() for item in values}
    if "eventcancelled" in names:
        return "cancelled"
    if "eventpostponed" in names:
        return "postponed"
    return "active"


def _source_event_id(text, url):
    match = _ICAL_ID.search(html.unescape(text))
    if match:
        return match.group(1)
    return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]


def _age_facts(text):
    match = _AGE_RANGE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2)), match.group(0)
    match = _AGE_MINIMUM.search(text)
    if match:
        return int(match.group(1)), None, match.group(0)
    return None, None, None


def _clean(value):
    return " ".join(html.unescape(str(value or "")).split())


def _clean_html(value):
    return " ".join(HtmlDocument(html.unescape(str(value or ""))).text.split())


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
