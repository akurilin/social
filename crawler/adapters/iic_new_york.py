"""Procedural adapter for Istituto Italiano di Cultura New York events."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from crawler.contracts import EventStub, ParseError, source_result


ADAPTER_ID = "iic_new_york_events"
PARSER_VERSION = "iic-new-york-wordpress-html-v1"
METHOD = "python_adapter"
MAX_PAGES = 100
_LISTING_DATE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Z][a-z]+)\s+(\d{1,2})\s+(20\d{2})\b")
_DETAIL_DATE = re.compile(
    r"\b(?:The\s+)?([A-Z][a-z]+)\s+(\d{1,2})\s+(20\d{2}),\s*"
    r"(\d{1,2}):(\d{2})\s*\(Local time\)", re.I)
_MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}
_MONTHS.update({name[:3]: number for name, number in list(_MONTHS.items())})


class IICNewYorkEventsAdapter:
    """Read the English WordPress listing pages and their event details."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started = _now_iso()
        end_date = seen_date + dt.timedelta(days=lookahead_days)
        stubs, listing_responses, listing_empty = self._listing_stubs(
            source, seen_date, end_date)
        if not stubs:
            if listing_empty:
                return source_result(
                    state="empty_verified", method=METHOD, recipe_version=PARSER_VERSION,
                    started_at=started, finished_at=_now_iso(),
                    artifacts=self.artifacts.items if self.artifacts is not None else [],
                    detail="IIC New York event listing explicitly reported no events in the date window.")
            raise ParseError("IIC New York listing pages contained no event cards or explicit empty signal")

        events, rejections, errors = [], [], []
        seen_urls = set()
        for stub in stubs:
            if stub.url in seen_urls:
                continue
            seen_urls.add(stub.url)
            if stub.date_hint and not seen_date <= stub.date_hint <= end_date:
                rejections.append(_stub_rejection(stub, source, "outside_date_window"))
                continue
            try:
                response = self.client.get(stub.url)
                if self.artifacts is not None:
                    self.artifacts.save_response(
                        "event_detail_html", "iic-new-york-detail-{}.html".format(len(events) + 1), response)
                event = _event(response, stub, source, timezone)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if seen_date <= event_date <= end_date:
                    events.append(event)
                else:
                    rejections.append({"reason": "outside_date_window", "raw": event})
            except (ParseError, ValueError, TypeError) as error:
                errors.append("{}: {}".format(stub.url, error))
                rejections.append({"reason": "event_parse_failed", "raw": {
                    "title": stub.title, "url": stub.url, "source_id": source["id"],
                    "source_listing_url": source["url"], "parser_version": PARSER_VERSION,
                    "error": str(error),
                }})

        date_contract_failed = (not events and rejections and not errors
                                and all(item["reason"] == "outside_date_window"
                                        for item in rejections))
        state = "ok" if events and not errors else (
            "validation_failed" if errors or date_contract_failed else "empty_verified")
        return source_result(
            state=state, method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("IIC New York parsed {} listing page(s), {} event URL(s), and {} "
                    "event(s) in {} through {}.").format(
                        len(listing_responses), len(seen_urls), len(events), seen_date, end_date),
            error=("; ".join(errors[:5]) or
                   ("IIC New York listing returned only events outside the requested date window"
                    if date_contract_failed else None)),
        )

    def _listing_stubs(self, source, start_date, end_date):
        page = 1
        seen_pages = set()
        stubs = []
        responses = []
        explicit_empty = False
        while True:
            if page > MAX_PAGES:
                raise ParseError("IIC New York listing exceeded {} pages".format(MAX_PAGES))
            url = _listing_url(source["url"], start_date, end_date, page)
            if url in seen_pages:
                raise ParseError("IIC New York listing pagination repeated a page URL")
            seen_pages.add(url)
            response = self.client.get(url)
            responses.append(response)
            if self.artifacts is not None:
                self.artifacts.save_response(
                    "event_listing_html", "iic-new-york-listing-page-{}.html".format(page), response)
            page_stubs, is_event_listing = _page_stubs(response.text, response.url)
            if not is_event_listing:
                raise ParseError("IIC New York listing page has no event-card structure")
            stubs.extend(page_stubs)
            if not page_stubs:
                explicit_empty = _explicit_empty(response.text)
            next_url = _next_page(response.text, response.url)
            if not next_url:
                break
            page += 1
        return stubs, responses, explicit_empty


def _listing_url(source_url, start_date, end_date, page):
    parsed = urlsplit(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ParseError("source URL is not an absolute HTTP URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/en/gli_eventi/",
                       urlencode({"date-init": start_date.isoformat(),
                                  "date-end": end_date.isoformat(), "pag": page}), ""))


def _page_stubs(text, base_url):
    soup = BeautifulSoup(text, "html.parser")
    cards = soup.select(".card-wrapper.card-space")
    result = []
    for card in cards:
        link = card.select_one('a[href*="/gli_eventi/calendario/"]')
        if not link:
            continue
        title_node = card.select_one("h5.card-title") or link
        title = _clean(title_node.get_text(" ", strip=True))
        date_node = card.select_one(".category-top")
        date_match = _LISTING_DATE.search(date_node.get_text(" ", strip=True) if date_node else "")
        hint = _date(date_match) if date_match else None
        if title:
            result.append(EventStub(url=urljoin(base_url, link.get("href")), title=title,
                                    date_hint=hint))
    # The form is a stable part of the WordPress event index even when a date
    # window has no cards. It makes an explicit empty message auditable.
    return result, bool(cards) or soup.select_one("#searchFormPost") is not None


def _next_page(text, base_url):
    soup = BeautifulSoup(text, "html.parser")
    link = soup.select_one("a.next.page-numbers[href]")
    return urljoin(base_url, link.get("href")) if link else ""


def _event(response, stub, source, timezone):
    soup = BeautifulSoup(response.text, "html.parser")
    article = soup.select_one("article.eventi")
    title_node = article.select_one("h1.entry-title") if article else None
    title = _clean(title_node.get_text(" ", strip=True) if title_node else stub.title)
    meta = article.select_one(".entry-meta") if article else None
    meta_text = _clean(meta.get_text(" ", strip=True) if meta else "")
    date_match = _DETAIL_DATE.search(meta_text)
    if not article or not title or not date_match:
        raise ParseError("IIC event detail is missing event article, title, or local date/time")
    start = _datetime(date_match, timezone)
    place = _label_value(meta_text, "Place")
    fee = _label_value(meta_text, "For a fee")
    content = article.select_one(".entry-content")
    if content:
        for node in content.select("form, script, style, .container-form"):
            node.decompose()
    description = _clean(content.get_text(" ", strip=True) if content else "")
    venue, address = _place(place)
    event_id = str(article.get("id") or "").removeprefix("post-")
    if not event_id:
        event_id = urlsplit(response.url).path.rstrip("/").rsplit("/", 1)[-1]
    visible = " ".join((meta_text, description))
    return {
        "title": title, "start": start, "end": None,
        "url": response.url, "signup_url": response.url,
        "host": "Istituto Italiano di Cultura New York", "venue": venue,
        "neighborhood": "", "address": address,
        "price": "Free" if fee.lower() == "no" else "", "is_free": True if fee.lower() == "no" else (
            False if fee.lower() == "yes" else None),
        "description": description, "capacity_flag": None,
        "status": "cancelled" if re.search(r"\bcancel(?:led|ed)\b", visible, re.I) else "active",
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": event_id,
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash,
        "extracted_json": {"event_date": date_match.group(0), "place": place,
                           "for_a_fee": fee, "description": description},
    }


def _date(match):
    month = _MONTHS.get(match.group(1).lower()) if match else None
    if not month:
        return None
    try:
        return dt.date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None


def _datetime(match, timezone):
    date = _date(match)
    if not date:
        raise ParseError("IIC event detail has an invalid date")
    return dt.datetime.combine(date, dt.time(int(match.group(4)), int(match.group(5))),
                               ZoneInfo(timezone)).isoformat()


def _label_value(text, label):
    match = re.search(r"\b{}:\s*(.*?)(?=\s+(?:Event date|Place|For a fee):|$)".format(
        re.escape(label)), text, re.I)
    return _clean(match.group(1)) if match else ""


def _place(value):
    values = [part.strip() for part in value.split(",") if part.strip()]
    return (values[0], ", ".join(values[1:])) if values else ("", "")


def _explicit_empty(text):
    return bool(re.search(
        r"(?:no\s+(?:upcoming\s+)?events?|no\s+results?|nothing)\s+"
        r"(?:found|available)", text, re.I))


def _stub_rejection(stub, source, reason):
    return {"reason": reason, "raw": {
        "title": stub.title, "url": stub.url, "source_id": source["id"],
        "source_listing_url": source["url"], "start_date_hint": stub.date_hint.isoformat()
        if stub.date_hint else None, "parser_version": PARSER_VERSION,
    }}


def _clean(value):
    return re.sub(r"\s+", " ", value).strip()


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
