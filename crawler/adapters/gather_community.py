"""Procedural adapter for public Gather community calendars."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "gather_community_calendar"
PARSER_VERSION = "gather-community-calendar-v1"
METHOD = "python_adapter"
DEFAULT_API_URL = "https://atlas-web-gather57-7t7d9xgg1-gather-nyc.vercel.app/api/getPosts"
GROUP_ID = "clfi9j47x0000rrrkyg1p32f5"
PAGE_SIZE = 100


class GatherCommunityCalendarAdapter:
    """Read public event entities without retaining community member data."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        # Gather returns member and reaction data with event records. Never save it.
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        group_id = str(source.get("gather_group_id") or GROUP_ID).strip()
        endpoint = str(source.get("api_url") or DEFAULT_API_URL).strip()
        if not group_id or not _http_url(endpoint):
            raise ParseError("Gather source configuration is invalid")
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        filter_data = {
            "entityFeed": {
                "startTime": _window_bound(seen_date, timezone, end=False),
                "endTime": _window_bound(window_end, timezone, end=True),
            }
        }

        raw_posts = []
        cursor = None
        cursors = set()
        page = 0
        while True:
            request = {"groupId": group_id, "pageSize": PAGE_SIZE, "filter": filter_data}
            if cursor is not None:
                request["cursor"] = cursor
            response = self.client.post_json(endpoint, request)
            posts, next_cursor = _page(response)
            raw_posts.extend((post, response) for post in posts)
            page += 1
            if len(posts) < PAGE_SIZE:
                break
            if not next_cursor or next_cursor in cursors:
                raise ParseError("Gather pagination cursor is missing or repeats")
            cursors.add(next_cursor)
            cursor = next_cursor
            if page >= 100:
                raise ParseError("Gather pagination exceeded safe page limit")

        events, rejections = [], []
        parse_failures = 0
        for raw, response in raw_posts:
            try:
                evidence = _event_evidence(raw)
                event = _event(evidence, source, endpoint, response, timezone)
            except (ParseError, TypeError, ValueError) as error:
                parse_failures += 1
                rejections.append(_rejection("event_parse_failed", raw, error=str(error)))
                continue
            event_date = dt.datetime.fromisoformat(event["start"]).date()
            if not seen_date <= event_date <= window_end:
                rejections.append({"reason": "outside_date_window", "raw": event})
                continue
            events.append(event)

        state = "validation_failed" if parse_failures else "ok" if events else "empty_verified"
        detail = (
            "Gather public community calendar returned {} post(s) across {} page(s); "
            "{} factual event entity or entities fell inside {} through {}."
        ).format(len(raw_posts), page, len(events), seen_date, window_end)
        return source_result(
            state=state,
            method=METHOD,
            recipe_version=PARSER_VERSION,
            started_at=started_at,
            finished_at=_now_iso(),
            events=events,
            rejections=rejections,
            # The API includes member and reaction data. Do not retain raw responses.
            artifacts=[],
            detail=detail,
            error="one or more Gather posts could not be parsed" if parse_failures else None,
        )


def _page(response):
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise ParseError("Gather API did not return valid JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
        raise ParseError("Gather API response is missing posts list")
    cursor = payload.get("endCursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
        raise ParseError("Gather API response has an invalid endCursor")
    return payload["posts"], cursor.strip() if isinstance(cursor, str) else None


def _event_evidence(raw):
    """Make the sole retained event record from a whitelist of public fields."""

    if not isinstance(raw, dict):
        raise ParseError("post is not an object")
    entity = raw.get("entity")
    if not isinstance(entity, dict):
        raise ParseError("post has no event entity")
    links = raw.get("links")
    first_link = links[0] if isinstance(links, list) and links and isinstance(links[0], dict) else {}
    return {
        "post_id": _text(raw.get("id")),
        "content": _public_content(raw.get("content")),
        "entity_id": _text(entity.get("id")),
        "entity_type": _text(entity.get("type")),
        "entity_url": _text(entity.get("url")),
        "start_time": _text(entity.get("startTime")),
        "end_time": _text(entity.get("endTime")),
        "address": _text(entity.get("address")),
        "link_url": _text(first_link.get("link")),
        "link_title": _text(first_link.get("title")),
    }


def _event(evidence, source, endpoint, response, timezone):
    post_id = evidence["post_id"]
    event_id = evidence["entity_id"] or post_id
    title = _preview_title(evidence["link_title"]) or _first_line(evidence["content"])
    if evidence["entity_type"] != "EVENT":
        raise ParseError("post entity is not an EVENT")
    if not post_id or not event_id or not title or not evidence["start_time"]:
        raise ParseError("event is missing post id, title, or start time")
    start = _local_datetime(evidence["start_time"], timezone)
    end = _local_datetime(evidence["end_time"], timezone) if evidence["end_time"] else None
    if end is not None and dt.datetime.fromisoformat(end) < dt.datetime.fromisoformat(start):
        raise ParseError("event end time is earlier than start time")
    event_url = _first_url(
        evidence["entity_url"], evidence["link_url"],
        "https://www.57.nyc/posts/{}".format(post_id),
    )
    if not event_url:
        raise ParseError("event has no valid event URL")
    canonical_evidence = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return {
        "title": title,
        "start": start,
        "end": end,
        "url": event_url,
        "signup_url": event_url,
        "host": "",
        "venue": "",
        "neighborhood": "",
        "address": evidence["address"],
        "price": "",
        "is_free": None,
        "description": evidence["content"],
        "capacity_flag": None,
        "status": "active",
        "source_id": source["id"],
        "source_listing_url": source["url"],
        "source_url": endpoint,
        "source_event_id": event_id,
        "fetched_at": response.fetched_at,
        "parser_version": PARSER_VERSION,
        "content_hash": hashlib.sha256(canonical_evidence.encode("utf-8")).hexdigest(),
        "extracted_json": evidence,
    }


def _rejection(reason, raw, error=None):
    try:
        evidence = _event_evidence(raw)
    except (ParseError, TypeError, ValueError):
        evidence = {"post_id": _text(raw.get("id"))} if isinstance(raw, dict) else {}
    if error:
        evidence["error"] = error
    return {"reason": reason, "raw": evidence}


def _window_bound(day, timezone, end):
    moment = dt.datetime.combine(day, dt.time.max if end else dt.time.min, ZoneInfo(timezone))
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _local_datetime(value, timezone):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ParseError("event time is invalid") from error
    if parsed.tzinfo is None:
        raise ParseError("event time has no timezone")
    return parsed.astimezone(ZoneInfo(timezone)).isoformat()


def _first_url(*values):
    return next((value for value in values if _http_url(value)), "")


def _http_url(value):
    parsed = urlsplit(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _first_line(value):
    return next((line.strip() for line in str(value).splitlines() if line.strip()), "")


def _preview_title(value):
    """Remove only the fixed wrappers used by known public link previews."""

    title = _text(value)
    if title.endswith(" · Luma"):
        return title[:-len(" · Luma")].strip()
    if title.startswith("Viewcy | "):
        return title[len("Viewcy | "):].strip()
    return title


def _text(value):
    return str(value or "").strip()


def _public_content(value):
    """Keep event copy but remove direct contact details from community posts."""

    text = _text(value)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email removed]", text)
    return re.sub(r"(?<!\w)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]\d{4}(?!\w)",
                  "[phone removed]", text)


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
