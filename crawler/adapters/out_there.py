"""Procedural adapter for https://outthere.nyc/."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from crawler.contracts import EventStub, FetchError, ParseError, source_result
from crawler.jsonld import HtmlDocument, find_typed_nodes, load_json_ld


ADAPTER_ID = "out_there"
PARSER_VERSION = "out-there-jsonld-v2"
METHOD = "python_adapter"
DATE_SUFFIX = re.compile(r"-(20\d{2}-\d{2}-\d{2})(?:$|[/?#])")
AGE_RANGE = re.compile(r"\bAges?\s+(\d{2})\s*[-–]\s*(\d{2})\b", re.I)
PLAIN_AGE_RANGE = re.compile(r"\b(\d{2})\s*[-–]\s*(\d{2})\b")
AGE_MINIMUM = re.compile(r"\b(\d{2})\s*\+")
OUTBOUND_ID = re.compile(r"/outbound/events/([^/]+)/signup")


class OutThereAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        window_start = seen_date
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        # Out There URL slugs sometimes use the UTC date. Fetch one date on
        # each side, then enforce the real window on the parsed local start.
        hint_start = window_start - dt.timedelta(days=1)
        hint_end = window_end + dt.timedelta(days=1)
        stubs = []
        rejections = []
        seen_urls = set()
        page_number = 1
        total_reported = None

        while True:
            listing_url = _listing_url(source["url"], page_number)
            response = self.client.get(listing_url)
            document = HtmlDocument(response.text)
            page_stubs = _listing_stubs(document, response.url)
            if page_number == 1 and self.artifacts is not None:
                self.artifacts.save_response("listing_html", "listing-001.html", response)
            if total_reported is None:
                total_reported = _reported_total(document.text)
            if not page_stubs and total_reported != 0:
                raise ParseError("listing page {} contained no Event ItemList URLs".format(
                    page_number))
            for stub in page_stubs:
                if stub.url in seen_urls:
                    continue
                seen_urls.add(stub.url)
                if stub.date_hint and not hint_start <= stub.date_hint <= hint_end:
                    rejections.append({
                        "reason": "outside_date_window",
                        "raw": _stub_raw(stub, source, response, window_start, window_end),
                    })
                else:
                    stubs.append(stub)
            if not _has_next_page(document, response.url, page_number):
                break
            page_number += 1
            if page_number > 100:
                raise ParseError("listing pagination exceeded 100 pages")

        events = []
        detail_errors = []
        for index, stub in enumerate(stubs, start=1):
            try:
                response = self.client.get(stub.url)
                if index == 1 and self.artifacts is not None:
                    self.artifacts.save_response("detail_html", "detail-sample.html", response)
                event = _parse_event(response, stub, source, timezone)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if not window_start <= event_date <= window_end:
                    rejections.append({"reason": "outside_date_window", "raw": event})
                    continue
                events.append(event)
            except (FetchError, ParseError, ValueError) as error:
                detail_errors.append("{}: {}".format(stub.url, error))
                rejections.append({
                    "reason": "detail_parse_failed",
                    "raw": {
                        "title": stub.title,
                        "url": stub.url,
                        "source_id": source["id"],
                        "parser_version": PARSER_VERSION,
                        "error": str(error),
                    },
                })

        if events:
            state = "validation_failed" if detail_errors else "ok"
        elif total_reported == 0 or seen_urls:
            state = "empty_verified" if not detail_errors else "validation_failed"
        else:
            state = "parse_failed"
        finished_at = _now_iso()
        detail = (
            "Python adapter parsed {} listing page(s), {} event URL(s), and {} "
            "event(s) in {} through {}."
        ).format(page_number, len(seen_urls), len(events), window_start, window_end)
        return source_result(
            state=state,
            method=METHOD,
            recipe_version=PARSER_VERSION,
            started_at=started_at,
            finished_at=finished_at,
            events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=detail,
            error="; ".join(detail_errors[:5]) or None,
        )


def _listing_url(seed_url, page_number):
    parsed = urlsplit(seed_url)
    query = [(key, value) for key, value in parse_qsl(
        parsed.query, keep_blank_values=True) if key.lower() != "page"]
    if page_number > 1:
        query.append(("page", str(page_number)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _listing_stubs(document, base_url):
    items = []
    for value in load_json_ld(document):
        for node in find_typed_nodes(value, "ListItem"):
            raw_url = node.get("url") or node.get("item")
            if isinstance(raw_url, dict):
                raw_url = raw_url.get("url") or raw_url.get("@id")
            if not isinstance(raw_url, str):
                continue
            url = urljoin(base_url, raw_url)
            parsed = urlsplit(url)
            if parsed.netloc != urlsplit(base_url).netloc \
                    or not parsed.path.startswith("/events/"):
                continue
            match = DATE_SUFFIX.search(parsed.path)
            date_hint = dt.date.fromisoformat(match.group(1)) if match else None
            items.append(EventStub(url=url, title=str(node.get("name") or ""),
                                   date_hint=date_hint))
    return items


def _has_next_page(document, base_url, page_number):
    for link in document.links:
        href = urljoin(base_url, link.get("href") or "")
        query = dict(parse_qsl(urlsplit(href).query, keep_blank_values=True))
        try:
            target_page = int(query.get("page", "0"))
        except ValueError:
            target_page = 0
        if target_page > page_number and (
                "next" in (link.get("text") or "").lower() or target_page == page_number + 1):
            return True
    return False


def _reported_total(text):
    for pattern in (r"\b(\d+)\s+found\b", r"\b(\d+)\s+events?\s+on\s+now\b"):
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1))
    return None


def _stub_raw(stub, source, response, window_start, window_end):
    return {
        "title": stub.title,
        "url": stub.url,
        "start_date_hint": stub.date_hint.isoformat() if stub.date_hint else None,
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "listing_content_hash": response.content_hash,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }


def _parse_event(response, stub, source, timezone):
    document = HtmlDocument(response.text)
    event_nodes = []
    values = load_json_ld(document)
    for value in values:
        event_nodes.extend(find_typed_nodes(value, "Event"))
    if not event_nodes:
        raise ParseError("detail page contained no Event JSON-LD")
    event = event_nodes[0]
    title = str(event.get("name") or stub.title or "").strip()
    start = _local_datetime(event.get("startDate"), timezone)
    end = _local_datetime(event.get("endDate"), timezone, required=False)
    if not title or not start:
        raise ParseError("Event JSON-LD is missing name or startDate")

    place = event.get("location") if isinstance(event.get("location"), dict) else {}
    address = _format_address(place.get("address"))
    organizer = event.get("organizer") if isinstance(event.get("organizer"), dict) else {}
    offers = _offers(event.get("offers"))
    price_value = _first_price(offers)
    signup_url = next((str(item.get("url")) for item in offers if item.get("url")), "")
    availability = [str(item.get("availability") or "") for item in offers]
    capacity_flag = _capacity_flag(title, availability)
    status = _event_status(event.get("eventStatus"))
    age_min, age_max, age_label = _age_range(document.text, title)
    description = _event_description(document.text, title, str(event.get("description") or ""))
    neighborhood = _neighborhood(values)
    external_id = _external_id(document, response.url)
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": str(event.get("url") or response.url or stub.url),
        "signup_url": signup_url,
        "host": str(organizer.get("name") or "").strip(),
        "venue": str(place.get("name") or "").strip(),
        "neighborhood": neighborhood,
        "address": address,
        "price": _format_price(price_value),
        "is_free": price_value == 0,
        "explicit_age_min": age_min,
        "explicit_age_max": age_max,
        "explicit_age_label": age_label,
        "orientation_scope": "straight",
        "description": description,
        "capacity_flag": capacity_flag,
        "status": status,
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": external_id,
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": event,
    }


def _local_datetime(value, timezone, required=True):
    if not value:
        if required:
            raise ParseError("missing date")
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    zone = ZoneInfo(timezone)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    else:
        parsed = parsed.astimezone(zone)
    return parsed.isoformat()


def _format_address(value):
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    locality = ", ".join(part for part in (
        str(value.get("addressLocality") or "").strip(),
        str(value.get("addressRegion") or "").strip(),
    ) if part)
    if value.get("postalCode"):
        locality = "{} {}".format(locality, value["postalCode"]).strip()
    return ", ".join(part for part in (
        str(value.get("streetAddress") or "").strip(), locality,
        str(value.get("addressCountry") or "").strip(),
    ) if part)


def _offers(value):
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _first_price(offers):
    prices = []
    for offer in offers:
        value = offer.get("price")
        if value is None:
            value = offer.get("lowPrice")
        if value is None or isinstance(value, bool):
            continue
        try:
            prices.append(float(value))
        except (TypeError, ValueError):
            continue
    return min(prices) if prices else None


def _format_price(value):
    if value is None:
        return ""
    if float(value).is_integer():
        return "${}".format(int(value))
    return "${:.2f}".format(value)


def _capacity_flag(title, availability):
    lowered_title = title.lower()
    if "men sold out" in lowered_title:
        return "men_sold_out"
    if "women sold out" in lowered_title:
        return "women_sold_out"
    values = [value.lower() for value in availability if value]
    if values and all(value.endswith("/soldout") or value.endswith("soldout")
                      for value in values):
        return "sold_out"
    if any("limitedavailability" in value for value in values):
        return "limited"
    return None


def _event_status(value):
    lowered = str(value or "").lower()
    if "cancelled" in lowered or "canceled" in lowered:
        return "cancelled"
    if "postponed" in lowered:
        return "postponed"
    return "active"


def _header_text(text):
    return re.split(r"\bDATE\s*&\s*TIME\b", text, maxsplit=1, flags=re.I)[0]


def _age_range(text, title):
    header = _header_text(text)
    match = AGE_RANGE.search(header)
    if not match:
        match = AGE_RANGE.search(title)
    if match:
        minimum, maximum = int(match.group(1)), int(match.group(2))
        if 18 <= minimum <= maximum <= 100:
            return minimum, maximum, "{}-{}".format(minimum, maximum)
    minimum_match = AGE_MINIMUM.search(header) or AGE_MINIMUM.search(title)
    if minimum_match:
        minimum = int(minimum_match.group(1))
        if 18 <= minimum <= 100:
            return minimum, None, "{}+".format(minimum)
    if re.search(r"\bAll ages\b", header, re.I):
        return None, None, "All ages"
    match = PLAIN_AGE_RANGE.search(title)
    if match:
        minimum, maximum = int(match.group(1)), int(match.group(2))
        if 18 <= minimum <= maximum <= 100:
            return minimum, maximum, "{}-{}".format(minimum, maximum)
    return None, None, ""


def _event_description(text, title, fallback):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    try:
        start = next(index for index, line in enumerate(lines)
                     if line.upper() == "ABOUT THIS EVENT") + 1
    except StopIteration:
        return fallback.strip()
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index] == title or lines[index].upper() == "SIMILAR EVENTS":
            end = index
            break
    extracted = "\n".join(lines[start:end]).strip()
    return extracted if len(extracted) > len(fallback.strip()) else fallback.strip()


def _neighborhood(values):
    for value in values:
        for breadcrumb in find_typed_nodes(value, "BreadcrumbList"):
            for item in breadcrumb.get("itemListElement") or []:
                if not isinstance(item, dict):
                    continue
                target = item.get("item")
                target_url = target.get("@id") if isinstance(target, dict) else target
                if isinstance(target_url, str) and "/geographies/neighborhood/" in target_url:
                    return str(item.get("name") or "").strip()
    return ""


def _external_id(document, base_url):
    for link in document.links:
        href = urljoin(base_url, link.get("href") or "")
        match = OUTBOUND_ID.search(urlsplit(href).path)
        if match:
            return match.group(1)
    return ""


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
