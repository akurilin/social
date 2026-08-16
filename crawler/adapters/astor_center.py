"""Procedural adapter for Astor Center's server-rendered class calendar."""

from __future__ import annotations

import datetime as dt
import html
import re
from urllib.parse import urljoin, urlsplit, parse_qs
from zoneinfo import ZoneInfo

from crawler.contracts import EventStub, FetchError, ParseError, source_result
from crawler.jsonld import HtmlDocument, find_typed_nodes, load_json_ld


ADAPTER_ID = "astor_center"
PARSER_VERSION = "astor-center-html-jsonld-v1"
METHOD = "python_adapter"
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}
_EVENT_LINK = re.compile(
    r'<a[^>]+href=["\']([^"\']*(?:class|controls/class)-[^"\']+)["\'][^>]*>'
    r'(.*?)</a>', re.I | re.S)
_DATE = re.compile(r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
                   r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{1,2}):([0-5]\d)\s*"
                   r"(AM|PM)\b", re.I)
_PRICE = re.compile(r"Ticket\s+Price:\s*\$\s*([\d,]+(?:\.\d{1,2})?)", re.I)


class AstorCenterAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now_iso()
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        response = self.client.get(source["url"])
        if self.artifacts is not None:
            self.artifacts.save_response("listing_html", "listing.html", response)
        stubs = _listing_stubs(response.text, response.url, seen_date)
        if not stubs:
            if not re.search(r"no\s+upcoming\s+(?:classes|events)", response.text, re.I):
                return source_result(
                    state="parse_failed", method=METHOD, recipe_version=PARSER_VERSION,
                    started_at=started, finished_at=_now_iso(), events=[], rejections=[],
                    artifacts=self.artifacts.items if self.artifacts is not None else [],
                    detail="Astor Center calendar had no active class URLs and no explicit empty signal.",
                    error="calendar structure contained no class links")
            return source_result(
                state="empty_verified", method=METHOD, recipe_version=PARSER_VERSION,
                started_at=started, finished_at=_now_iso(), events=[], rejections=[],
                artifacts=self.artifacts.items if self.artifacts is not None else [],
                detail="Astor Center calendar loaded successfully with no active class URLs.")
        events, rejections, errors = [], [], []
        seen = set()
        for stub in stubs:
            if stub.url in seen:
                continue
            seen.add(stub.url)
            if stub.date_hint and not seen_date <= stub.date_hint <= end_date:
                rejections.append(_rejection(stub, source, response, "outside_date_window"))
                continue
            try:
                detail = self.client.get(stub.url)
                if len(events) == 0 and self.artifacts is not None:
                    self.artifacts.save_response("detail_html", "detail-sample.html", detail)
                event = _parse_event(detail, stub, source, timezone)
                date = dt.datetime.fromisoformat(event["start"]).date()
                if not seen_date <= date <= end_date:
                    rejections.append({"reason": "outside_date_window", "raw": event})
                else:
                    events.append(event)
            except (FetchError, ParseError, ValueError) as error:
                errors.append(f"{stub.url}: {error}")
                rejections.append({"reason": "detail_parse_failed", "raw": {
                    "title": stub.title, "url": stub.url, "source_id": source["id"],
                    "parser_version": PARSER_VERSION, "error": str(error)}})
        state = "ok" if events and not errors else (
            "validation_failed" if errors else "empty_verified")
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=f"Parsed Astor Center calendar: {len(seen)} class URL(s), {len(events)} event(s).",
            error="; ".join(errors[:5]) or None)


def _listing_stubs(text, base_url, seen_date):
    result = []
    for href, body in _EVENT_LINK.findall(text):
        title = _strip_html(body).strip()
        if not title:
            continue
        match = _DATE.search(_strip_html(body))
        hint = None
        if match:
            month = _MONTHS.get(match.group(1).lower())
            if month:
                year = seen_date.year
                # Handle a calendar crossing New Year without using wall-clock time.
                if month - seen_date.month > 6:
                    year -= 1
                elif seen_date.month - month > 6:
                    year += 1
                hint = dt.date(year, month, int(match.group(2)))
        url = urljoin(base_url, html.unescape(href))
        parsed = urlsplit(url)
        canonical_path = re.sub(r"^/controls/(class-[^/]+\.ac)$", r"/\1", parsed.path,
                                flags=re.I)
        if canonical_path != parsed.path:
            url = parsed._replace(path=canonical_path).geturl()
        result.append(EventStub(url=url, title=title,
                                date_hint=hint))
    return result


def _parse_event(response, stub, source, timezone):
    document = HtmlDocument(response.text)
    nodes = []
    for value in load_json_ld(document):
        nodes.extend(find_typed_nodes(value, "Event"))
    if not nodes:
        raise ParseError("detail page contained no Event JSON-LD")
    event = nodes[0]
    title = str(event.get("name") or stub.title).strip()
    start = _local_datetime(event.get("startDate"), timezone)
    if not title or not start:
        raise ParseError("Event JSON-LD is missing name or startDate")
    place = event.get("location") if isinstance(event.get("location"), dict) else {}
    description = _strip_html(str(event.get("description") or "")).strip()
    visible = document.text
    host_match = re.search(r"\bwith\s+([A-Z][^\n]+?)(?:\s+Ticket Price:|\s*$)", visible)
    host = host_match.group(1).strip() if host_match else ""
    price_match = _PRICE.search(visible)
    price = float(price_match.group(1).replace(",", "")) if price_match else None
    lowered = visible.lower()
    status = "cancelled" if "cancelled" in lowered or "canceled" in lowered else "active"
    capacity_flag = "sold_out" if "sold out" in lowered else None
    parsed_url = response.url
    query_class = parse_qs(urlsplit(parsed_url).query).get("class", [""])[0]
    external_id = query_class or urlsplit(stub.url).path.rsplit("/", 1)[-1].removesuffix(".ac")
    external_id = re.sub(r"^(?:controls/)?class-", "", external_id)
    return {
        "title": title, "start": start, "end": None,
        "url": response.url, "signup_url": response.url, "host": host,
        "venue": str(place.get("name") or "Astor Center").strip(),
        "neighborhood": "", "address": str(place.get("address") or "").strip(),
        "price": _format_price(price), "is_free": price == 0,
        "description": description, "capacity_flag": capacity_flag,
        "status": status, "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": external_id,
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash, "extracted_json": event,
    }


def _local_datetime(value, timezone):
    if not value:
        raise ParseError("missing date")
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        parsed = dt.datetime.strptime(raw, "%m/%d/%Y %I:%M:%S %p")
    zone = ZoneInfo(timezone)
    return (parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)).isoformat()


def _strip_html(value):
    return re.sub(r"<[^>]+>", " ", html.unescape(value))


def _format_price(value):
    if value is None:
        return ""
    return f"${int(value)}" if float(value).is_integer() else f"${value:.2f}"


def _rejection(stub, source, response, reason):
    return {"reason": reason, "raw": {"title": stub.title, "url": stub.url,
        "source_id": source["id"], "source_listing_url": source["url"],
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "start_date_hint": stub.date_hint.isoformat() if stub.date_hint else None}}


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
