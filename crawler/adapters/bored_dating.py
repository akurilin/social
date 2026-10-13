"""Read BODA's worldwide calendar and verify NYC events on Fatsoma."""

from __future__ import annotations

import datetime as dt
import html
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "bored_dating"
PARSER_VERSION = "bored-dating-v2"
METHOD = "python_adapter"
HOST = "Bored of Dating Apps"
NYC_LOCALITIES = {"new york", "new york city", "brooklyn", "queens", "bronx",
                  "the bronx", "staten island", "manhattan"}
EVENT_STATES = {"EventScheduled": "active", "EventCancelled": "cancelled",
                "EventPostponed": "postponed", "EventRescheduled": "postponed"}
DISPLAY_DATE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]{3})\s+at\s+(\d{1,2}:\d{2}\s*[ap]m)", re.I)
FATSOMA_EVENT_PATH = re.compile(r"^/e/([A-Za-z0-9]+)/")


class BoredDatingAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        listing = self.client.get(source["url"])
        self._save("listing_html", "boda-listing.html", listing)
        soup = BeautifulSoup(listing.text, "html.parser")
        cards = soup.select(".events-list-alternate__card")
        if soup.select_one(".events-list-alternate__grid") is None:
            raise ParseError("BODA calendar grid is missing")
        records = _json_events(soup)
        # Matching HTML and structured records detects partial or changed feeds.
        if len(cards) != len(records):
            raise ParseError("BODA calendar cards and JSON-LD records differ")
        card_urls = {}
        for card in cards:
            link = card.select_one(".card__heading a")
            when = card.select_one(".meta--date")
            if not link or not when or not link.get("href"):
                raise ParseError("BODA calendar card is malformed")
            url = link["href"]
            if url in card_urls:
                raise ParseError("BODA calendar contains duplicate URLs")
            card_urls[url] = when.get_text(" ", strip=True)

        events, rejections, dates = [], [], []
        failures = 0
        seen_urls = set()
        for record in records:
            try:
                url = record.get("url")
                if url not in card_urls or url in seen_urls:
                    raise ParseError("BODA JSON-LD URL does not match one card")
                seen_urls.add(url)
                if (urlsplit(url).netloc != urlsplit(source["url"]).netloc
                        or not urlsplit(url).path.startswith("/event/")):
                    raise ParseError("BODA event URL is invalid")
                year = dt.datetime.fromisoformat(record["startDate"]).year
                local_date = _display_start(card_urls[url], year, timezone)
                dates.append(local_date.date())
                if not seen_date <= local_date.date() <= window_end:
                    rejections.append({"reason": "outside_date_window", "raw": record})
                    continue
                if not _is_nyc(record):
                    rejections.append({"reason": "outside_target_geography", "raw": record})
                    continue
                detail = self.client.get(url)
                slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
                self._save("event_html", "boda-{}.html".format(slug), detail)
                detail_events = _json_events(BeautifulSoup(detail.text, "html.parser"))
                if len(detail_events) != 1:
                    raise ParseError("BODA detail must contain one Event")
                ticket_url = detail_events[0].get("url", "")
                parts = urlsplit(ticket_url)
                match = FATSOMA_EVENT_PATH.match(parts.path)
                if parts.scheme != "https" or parts.netloc != "www.fatsoma.com" or not match:
                    raise ParseError("BODA detail has no Fatsoma ticket URL")
                ticket = self.client.get(ticket_url)
                self._save("ticket_html", "fatsoma-{}.html".format(match[1]), ticket)
                parsed = _json_events(BeautifulSoup(ticket.text, "html.parser"))
                if len(parsed) != 1:
                    raise ParseError("Fatsoma detail must contain one Event")
                event = _event(parsed[0], record, ticket, source, url, match[1], timezone)
                if dt.datetime.fromisoformat(event["start"]) != local_date:
                    raise ParseError("Fatsoma start differs from the BODA calendar")
                # Keep inactive dates out of discoveries while retaining evidence.
                if event["status"] != "active":
                    rejections.append({"reason": "event_" + event["status"], "raw": parsed[0]})
                    continue
                events.append(event)
            except (ParseError, KeyError, TypeError, ValueError) as error:
                failures += 1
                rejections.append({"reason": "event_parse_failed", "raw": {
                    "event": record, "error": str(error)}})

        # A date beyond the window proves ordered coverage; a blank grid does not.
        covered = bool(dates) and dates == sorted(dates) and max(dates) > window_end
        state = "validation_failed" if failures else (
            "ok" if events else "empty_verified" if covered else "empty_suspicious")
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started_at, finished_at=_now(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail="Read {} BODA records; verified {} NYC events through {}.".format(
                len(records), len(events), window_end),
            error="{} malformed event(s).".format(failures) if failures else None,
        )

    def _save(self, kind, name, response):
        if self.artifacts is not None:
            self.artifacts.save_response(kind, name, response)


def _json_events(soup):
    events = []
    found_array = False
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.get_text())
        except json.JSONDecodeError as error:
            raise ParseError("invalid JSON-LD") from error
        found_array |= isinstance(payload, list)
        for item in _json_items(payload):
            kind = item.get("@type")
            if kind == "Event" or isinstance(kind, list) and "Event" in kind:
                events.append(item)
    if not events and not found_array:
        raise ParseError("no Event JSON-LD or explicit empty array")
    return events


def _json_items(value):
    if isinstance(value, list):
        for item in value:
            yield from _json_items(item)
    elif isinstance(value, dict):
        yield value
        yield from _json_items(value.get("@graph", []))


def _display_start(value, year, timezone):
    match = DISPLAY_DATE.search(value)
    if not match:
        raise ParseError("BODA card has no usable local date")
    clock = re.sub(r"\s*([ap]m)$", r" \1", match[3], flags=re.I)
    stamp = "{} {} {} {}".format(match[1], match[2], year, clock)
    return dt.datetime.strptime(stamp, "%d %b %Y %I:%M %p").replace(tzinfo=ZoneInfo(timezone))


def _is_nyc(record):
    location = record.get("location")
    address = location.get("address") if isinstance(location, dict) else None
    if not isinstance(address, dict):
        raise ParseError("event has no structured locality")
    locality = _clean(address.get("addressLocality") or "").lower()
    country = _clean(address.get("addressCountry") or "").lower()
    if country not in {"", "us", "usa", "united states"}:
        return False
    if locality:
        return locality in NYC_LOCALITIES
    # A full US postal address can supply the city when the city field is blank.
    match = re.search(
        r",\s*(New York(?: City)?|Manhattan|Brooklyn|Queens|(?:The )?Bronx|Staten Island)"
        r",\s*(?:NY|New York)\s+\d{5}(?:-\d{4})?(?:,\s*(?:USA|US|United States))?\s*$",
        _clean(address.get("streetAddress")), re.I)
    if not match:
        raise ParseError("event has no structured locality or validated NYC address")
    return True


def _event(record, listing_record, response, source, boda_url, event_id, timezone):
    title = _clean(record.get("name", ""))
    description = _clean(record.get("description", ""))
    if not title or not description or not _is_nyc(record):
        raise ParseError("Fatsoma event lacks title, description, or NYC venue")
    if _title_key(title) != _title_key(listing_record.get("name", "")):
        raise ParseError("Fatsoma title differs from BODA")
    if record.get("url", "").rstrip("/") != response.url.rstrip("/"):
        raise ParseError("Fatsoma event URL does not match its response")
    start = _datetime(record.get("startDate"), timezone)
    end = _datetime(record.get("endDate"), timezone) if record.get("endDate") else None
    if end and dt.datetime.fromisoformat(end) <= dt.datetime.fromisoformat(start):
        raise ParseError("event ends before it starts")
    status = EVENT_STATES.get(str(record.get("eventStatus", "")).rsplit("/", 1)[-1])
    if status is None:
        raise ParseError("Fatsoma event status is unknown")
    location = record["location"]
    address = location["address"]
    venue = _clean(location.get("name", ""))
    street = _clean(address.get("streetAddress", ""))
    if not venue or not street:
        raise ParseError("Fatsoma venue or address is missing")
    price, is_free, capacity = _offers(record.get("offers", []))
    label = _clean(record.get("typicalAgeRange", "")) or None
    age_match = re.fullmatch(r"(\d+)\s*(?:\+|[-–]\s*(\d+))", label or "")
    organizer = record.get("organizer") or {}
    return {
        "title": title, "start": start, "end": end, "url": boda_url,
        "signup_url": response.url, "host": _clean(organizer.get("name", "")) or HOST,
        "venue": venue, "address": street, "neighborhood": "",
        "price": price, "is_free": is_free, "description": description,
        "capacity_flag": capacity, "status": status,
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": event_id,
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": {"listing": listing_record, "ticket": record},
        "explicit_age_label": label,
        "explicit_age_min": int(age_match[1]) if age_match else None,
        "explicit_age_max": int(age_match[2]) if age_match and age_match[2] else None,
    }


def _datetime(value, timezone):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ParseError("Fatsoma datetime lacks UTC offset")
    return parsed.astimezone(ZoneInfo(timezone)).isoformat()


def _offers(offers):
    if not isinstance(offers, list):
        raise ParseError("Fatsoma offers are malformed")
    amounts, currencies, availability = [], set(), []
    for offer in offers:
        if not isinstance(offer, dict):
            raise ParseError("Fatsoma offer is malformed")
        if offer.get("@type") == "AggregateOffer":
            continue
        availability.append(str(offer.get("availability", "")).rsplit("/", 1)[-1])
        if offer.get("price") is None:
            continue
        try:
            amount = Decimal(str(offer["price"]))
        except InvalidOperation as error:
            raise ParseError("Fatsoma price is invalid") from error
        if not amount.is_finite() or amount < 0 or not offer.get("priceCurrency"):
            raise ParseError("Fatsoma price or currency is invalid")
        amounts.append(amount)
        currencies.add(offer["priceCurrency"])
    price = ""
    if amounts and len(currencies) == 1:
        low, high = min(amounts), max(amounts)
        price = "{} {:.2f}{} + booking fees".format(
            next(iter(currencies)), low, "–{:.2f}".format(high) if high != low else "")
    capacity = "Sold out" if availability and all(x == "SoldOut" for x in availability) else None
    if capacity is None and "SoldOut" in availability:
        capacity = "Some ticket types sold out"
    return price, all(x == 0 for x in amounts) if amounts else None, capacity


def _clean(value):
    return re.sub(r"\s+", " ", html.unescape(html.unescape(str(value)))).strip()


def _title_key(value):
    return _clean(value).replace("–", "-").replace("—", "-").casefold()


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
