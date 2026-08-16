"""Procedural adapter for public Luma calendars."""

from __future__ import annotations

import json
import datetime as dt
from urllib.parse import urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import ParseError, source_result
from crawler.jsonld import HtmlDocument, find_typed_nodes, load_json_ld


ADAPTER_ID = "luma_calendar"
PARSER_VERSION = "luma-calendar-jsonld-v1"
METHOD = "python_adapter"

LUMA_CATEGORY_ADAPTER_ID = "luma_category"
LUMA_CATEGORY_PARSER_VERSION = "luma-category-discovery-api-v1"
LUMA_CATEGORY_API = "https://api.lu.ma/discover/get-paginated-events"
LUMA_CATEGORY_PAGE_SIZE = 50
NYC_COORDINATE = {"latitude": "40.7128", "longitude": "-74.0060"}
NYC_CITIES = {"new york", "new york city", "manhattan", "brooklyn", "queens",
              "bronx", "staten island"}


class LumaCalendarAdapter:
    """Read the server-rendered Event JSON-LD on a Luma calendar page."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        rejections = []
        try:
            response = self.client.get(source["url"])
            if self.artifacts is not None:
                self.artifacts.save_response("listing_html", "calendar.html", response)
            document = HtmlDocument(response.text)
            values = load_json_ld(document)
            nodes = []
            for value in values:
                nodes.extend(find_typed_nodes(value, "Event"))
            if not nodes:
                if _empty_collection_signal(values):
                    return source_result(
                        state="empty_verified",
                        method=METHOD,
                        recipe_version=PARSER_VERSION,
                        started_at=started_at,
                        finished_at=_now_iso(),
                        artifacts=(self.artifacts.items if self.artifacts is not None else []),
                        detail="Luma calendar returned an explicit empty ItemList.",
                    )
                raise ParseError("calendar page contained no Event JSON-LD")

            events = []
            for node in nodes:
                try:
                    event = _parse_event(node, source, response, timezone)
                    event_date = dt.datetime.fromisoformat(event["start"]).date()
                    if not seen_date <= event_date <= window_end:
                        rejections.append({
                            "reason": "outside_date_window",
                            "raw": event,
                        })
                        continue
                    events.append(event)
                except (ParseError, ValueError, TypeError) as error:
                    rejections.append({
                        "reason": "event_parse_failed",
                        "raw": {
                            "source_id": source["id"],
                            "source_listing_url": source["url"],
                            "parser_version": PARSER_VERSION,
                            "error": str(error),
                            "extracted_json": node,
                        },
                    })

            parse_errors = any(item["reason"] == "event_parse_failed"
                               for item in rejections)
            # A page with only stale or future-outside-window events is not an
            # explicit empty signal. Keep it auditable so the runner can use a
            # fallback instead of marking a temporarily stale calendar empty.
            state = ("validation_failed" if parse_errors else
                     "ok" if events else
                     "empty_suspicious")
            detail = (
                "Python adapter parsed {} Event JSON-LD node(s), {} event(s) in "
                "{} through {}."
            ).format(len(nodes), len(events), seen_date, window_end)
            return source_result(
                state=state,
                method=METHOD,
                recipe_version=PARSER_VERSION,
                started_at=started_at,
                finished_at=_now_iso(),
                events=events,
                rejections=rejections,
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail=detail,
                error=("one or more Event nodes could not be parsed"
                       if state == "validation_failed" else None),
            )
        except ParseError as error:
            return source_result(
                state="parse_failed",
                method=METHOD,
                recipe_version=PARSER_VERSION,
                started_at=started_at,
                finished_at=_now_iso(),
                rejections=rejections,
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail="Luma calendar JSON-LD contract was not met.",
                error=str(error),
            )


class LumaCategoryAdapter:
    """Read a public Luma discovery category through its paginated API."""

    id = LUMA_CATEGORY_ADAPTER_ID
    version = LUMA_CATEGORY_PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        rejections = []
        events = []
        cursor = None
        cursors = set()
        last_start = None
        page = 1
        ordering_is_proven = True
        scan_complete = False
        seen_ids = set()
        try:
            slug = _category_slug(source["url"])
            while True:
                endpoint = _category_api_url(slug, cursor)
                response = self.client.get(endpoint)
                if self.artifacts is not None:
                    self.artifacts.save_response(
                        "category_api_json", "luma-category-page-{}.json".format(page), response)
                payload = _category_payload(response)
                entries, has_more, next_cursor = _validate_category_page(payload)
                page_starts = []
                for entry in entries:
                    try:
                        raw = _category_event(entry)
                        event_id = str(raw.get("api_id") or "").strip()
                        if not event_id:
                            raise ParseError("discovery entry event is missing api_id")
                        if event_id in seen_ids:
                            raise ParseError("discovery API repeated event {}".format(event_id))
                        seen_ids.add(event_id)
                        start = _local_datetime(raw.get("start_at"), timezone)
                        start_at = dt.datetime.fromisoformat(start)
                    except (ParseError, ValueError, TypeError) as error:
                        ordering_is_proven = False
                        rejections.append(_category_rejection(
                            "event_parse_failed", entry, source, response, error=str(error)))
                        continue
                    if last_start is not None and start_at < last_start:
                        ordering_is_proven = False
                    last_start = start_at
                    page_starts.append(start_at)
                    if not _is_nyc_event(raw):
                        rejections.append(_category_rejection(
                            "outside_nyc", entry, source, response))
                        continue
                    try:
                        event = _parse_category_event(entry, source, response, timezone)
                    except (ParseError, ValueError, TypeError) as error:
                        rejections.append(_category_rejection(
                            "event_parse_failed", entry, source, response, error=str(error)))
                        continue
                    event_date = dt.datetime.fromisoformat(event["start"]).date()
                    if not seen_date <= event_date <= window_end:
                        rejections.append({"reason": "outside_date_window", "raw": event})
                        continue
                    events.append(event)

                if not has_more:
                    scan_complete = True
                    break
                if not entries:
                    raise ParseError("discovery API has_more result contained no entries")
                if ordering_is_proven and page_starts and min(page_starts).date() > window_end:
                    # The public API is chronological. A full page after the
                    # window proves later pages cannot add an in-window event.
                    scan_complete = True
                    break
                if next_cursor in cursors:
                    raise ParseError("discovery API repeated a pagination cursor")
                cursors.add(next_cursor)
                cursor = next_cursor
                page += 1

            parse_failed = any(item["reason"] == "event_parse_failed"
                               for item in rejections)
            if parse_failed:
                state = "validation_failed"
                error = "one or more discovery events could not be parsed"
            elif events:
                state = "ok"
                error = None
            elif scan_complete and ordering_is_proven:
                state = "empty_verified"
                error = None
            else:
                state = "empty_suspicious"
                error = None
            detail = (
                "Luma category discovery API scanned {} page(s), with {} event(s) "
                "in NYC during {} through {}; chronological scope {}."
            ).format(page, len(events), seen_date, window_end,
                     "was verified" if scan_complete and ordering_is_proven else "was not verified")
            return source_result(
                state=state,
                method=METHOD,
                recipe_version=LUMA_CATEGORY_PARSER_VERSION,
                started_at=started_at,
                finished_at=_now_iso(),
                events=events,
                rejections=rejections,
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail=detail,
                error=error,
            )
        except ParseError as error:
            return source_result(
                state="parse_failed",
                method=METHOD,
                recipe_version=LUMA_CATEGORY_PARSER_VERSION,
                started_at=started_at,
                finished_at=_now_iso(),
                events=events,
                rejections=rejections,
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail="Luma category discovery API contract was not met.",
                error=str(error),
            )


def _category_slug(source_url):
    parsed = urlsplit(source_url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc not in {"lu.ma", "luma.com"}:
        raise ParseError("Luma category source URL is not a public Luma URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1:
        raise ParseError("Luma category source URL must contain one category slug")
    return parts[0]


def _category_api_url(slug, cursor):
    query = {
        "slug": slug,
        "latitude": NYC_COORDINATE["latitude"],
        "longitude": NYC_COORDINATE["longitude"],
        "pagination_limit": LUMA_CATEGORY_PAGE_SIZE,
    }
    if cursor:
        query["pagination_cursor"] = cursor
    return "{}?{}".format(LUMA_CATEGORY_API, urlencode(query))


def _category_payload(response):
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise ParseError("discovery API did not return valid JSON") from error
    if not isinstance(payload, dict):
        raise ParseError("discovery API response is not an object")
    return payload


def _validate_category_page(payload):
    entries = payload.get("entries")
    has_more = payload.get("has_more")
    next_cursor = payload.get("next_cursor")
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ParseError("discovery API entries field is invalid")
    if not isinstance(has_more, bool):
        raise ParseError("discovery API has_more field is invalid")
    if has_more and (not isinstance(next_cursor, str) or not next_cursor.strip()):
        raise ParseError("discovery API has_more result is missing next_cursor")
    if not has_more and next_cursor not in (None, ""):
        raise ParseError("discovery API terminal result has an unexpected next_cursor")
    return entries, has_more, next_cursor


def _category_event(entry):
    event = entry.get("event")
    if not isinstance(event, dict):
        raise ParseError("discovery entry is missing event object")
    return event


def _is_nyc_event(raw):
    address = raw.get("geo_address_info")
    if not isinstance(address, dict):
        return False
    city = str(address.get("city") or "").strip().lower()
    region = str(address.get("region") or "").strip().lower()
    country = str(address.get("country") or "").strip().lower()
    return city in NYC_CITIES and region == "new york" and country in {"united states", "usa"}


def _parse_category_event(entry, source, response, timezone):
    raw = _category_event(entry)
    event_id = str(raw.get("api_id") or "").strip()
    title = str(raw.get("name") or "").strip()
    start = _local_datetime(raw.get("start_at"), timezone)
    if not event_id or not title or not start:
        raise ParseError("discovery event is missing api_id, name, or start_at")
    end = _local_datetime(raw.get("end_at"), timezone, required=False)
    event_path = str(raw.get("url") or "").strip()
    if not event_path:
        raise ParseError("discovery event is missing public URL")
    address = raw.get("geo_address_info")
    if not isinstance(address, dict):
        raise ParseError("discovery event is missing geo_address_info")
    ticket_info = entry.get("ticket_info")
    if ticket_info is None:
        ticket_info = {}
    if not isinstance(ticket_info, dict):
        raise ParseError("discovery event ticket_info is invalid")
    hosts = entry.get("hosts")
    if hosts is None:
        hosts = []
    if not isinstance(hosts, list) or any(not isinstance(host, dict) for host in hosts):
        raise ParseError("discovery event hosts are invalid")
    calendar = entry.get("calendar")
    if calendar is None:
        calendar = {}
    if not isinstance(calendar, dict):
        raise ParseError("discovery event calendar is invalid")
    event_url = urljoin("https://luma.com/", event_path)
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": event_url,
        "signup_url": event_url,
        "host": _category_host(hosts, calendar),
        "venue": str(address.get("address") or "").strip(),
        "neighborhood": str(address.get("sublocality") or "").strip(),
        "address": str(address.get("full_address") or address.get("address") or "").strip(),
        "price": _category_price(ticket_info),
        "is_free": ticket_info.get("is_free") is True,
        "description": "",
        "capacity_flag": _category_capacity_flag(ticket_info),
        "status": "active",
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": event_id,
        "fetched_at": response.fetched_at,
        "parser_version": LUMA_CATEGORY_PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": entry,
    }


def _category_host(hosts, calendar):
    names = [str(host.get("name") or "").strip() for host in hosts]
    names = [name for name in names if name]
    if names:
        return ", ".join(names)
    return str(calendar.get("name") or "").strip()


def _category_price(ticket_info):
    if ticket_info.get("is_free") is True:
        return "Free"
    price = ticket_info.get("price")
    if not isinstance(price, dict):
        return ""
    cents = price.get("cents")
    currency = str(price.get("currency") or "").lower()
    if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0 or currency != "usd":
        return ""
    value = cents / 100
    return "${}".format(int(value)) if value.is_integer() else "${:.2f}".format(value)


def _category_capacity_flag(ticket_info):
    if ticket_info.get("is_sold_out") is True:
        return "sold_out"
    if ticket_info.get("is_near_capacity") is True:
        return "limited"
    return None


def _category_rejection(reason, raw, source, response, error=None):
    payload = {
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "parser_version": LUMA_CATEGORY_PARSER_VERSION,
        "extracted_json": raw,
    }
    if error:
        payload["error"] = error
    return {"reason": reason, "raw": payload}


def _parse_event(node, source, response, timezone):
    title = str(node.get("name") or "").strip()
    start = _local_datetime(node.get("startDate"), timezone)
    if not title or not start:
        raise ParseError("Event JSON-LD is missing name or startDate")
    end = _local_datetime(node.get("endDate"), timezone, required=False)
    place = node.get("location") if isinstance(node.get("location"), dict) else {}
    organizer = node.get("organizer") if isinstance(node.get("organizer"), dict) else {}
    offers = _offers(node.get("offers"))
    price = _first_price(offers)
    signup_url = next((str(item["url"]) for item in offers if item.get("url")), "")
    availability = [str(item.get("availability") or "") for item in offers]
    event_url = str(node.get("url") or node.get("@id") or response.url)
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": urljoin(response.url, event_url),
        "signup_url": signup_url,
        "host": str(organizer.get("name") or "").strip(),
        "venue": str(place.get("name") or "").strip(),
        "neighborhood": "",
        "address": _format_address(place.get("address")),
        "price": _format_price(price),
        "is_free": price == 0,
        "description": str(node.get("description") or "").strip(),
        "capacity_flag": _capacity_flag(availability),
        "status": _event_status(node.get("eventStatus")),
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": response.url,
        "source_event_id": str(node.get("@id") or event_url),
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": node,
    }


def _empty_collection_signal(values):
    """Return true only for an explicit empty JSON-LD calendar collection."""
    for value in values:
        for collection in find_typed_nodes(value, "ItemList"):
            items = collection.get("itemListElement")
            if isinstance(items, list) and not items:
                return True
    return False


def _local_datetime(value, timezone, required=True):
    if not value:
        if required:
            raise ParseError("missing date")
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    zone = ZoneInfo(timezone)
    return (parsed.replace(tzinfo=zone) if parsed.tzinfo is None
            else parsed.astimezone(zone)).isoformat()


def _offers(value):
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _first_price(offers):
    prices = []
    for offer in offers:
        value = offer.get("price", offer.get("lowPrice"))
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
    return "${}".format(int(value)) if float(value).is_integer() else "${:.2f}".format(value)


def _format_address(value):
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    locality = ", ".join(str(value.get(key) or "").strip() for key in
                          ("addressLocality", "addressRegion") if value.get(key))
    if value.get("postalCode"):
        locality = "{} {}".format(locality, value["postalCode"]).strip()
    return ", ".join(part for part in
                      (str(value.get("streetAddress") or "").strip(), locality,
                       str(value.get("addressCountry") or "").strip()) if part)


def _capacity_flag(availability):
    values = [value.lower() for value in availability if value]
    if values and all("soldout" in value for value in values):
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


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
