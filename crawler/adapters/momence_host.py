"""Reusable adapter for Momence's public host-schedule API."""

from __future__ import annotations

import datetime as dt
import json
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from crawler.contracts import ParseError, source_result


ADAPTER_ID = "momence_host"
PARSER_VERSION = "momence-host-schedule-v1"
METHOD = "python_adapter"
READONLY_API = "https://readonly-api.momence.com"
PAGE_SIZE = 100


class MomenceHostAdapter:
    """Read one public Momence host, with a server-side date window."""

    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        started_at = _now_iso()
        host_id = _host_id(source)
        window_end = seen_date + dt.timedelta(days=lookahead_days)
        pages, raw_events = 0, []
        expected_total = None
        for page in range(100):
            endpoint = _sessions_url(host_id, seen_date, window_end, timezone, page)
            response = self.client.get(endpoint)
            pages += 1
            if self.artifacts is not None:
                self.artifacts.save_response(
                    "momence_host_schedule_json", "momence-host-{}-page-{}.json".format(
                        host_id, page), response)
            payload = _payload(response, page)
            pagination = payload["pagination"]
            if expected_total is None:
                expected_total = pagination["totalCount"]
            elif pagination["totalCount"] != expected_total:
                raise ParseError("Momence totalCount changed between pages")
            raw_events.extend((item, response) for item in payload["payload"])
            if len(raw_events) >= expected_total:
                break
        else:
            raise ParseError("Momence pagination exceeded 100 pages")
        if len(raw_events) != expected_total:
            raise ParseError("Momence returned {} session(s), expected {}".format(
                len(raw_events), expected_total))

        events, rejections = [], []
        date_contract_failed = False
        for raw, response in raw_events:
            try:
                event = _event(raw, source, response, host_id, timezone)
                event_date = dt.datetime.fromisoformat(event["start"]).date()
                if not seen_date <= event_date <= window_end:
                    date_contract_failed = True
                    rejections.append(_rejection(
                        "outside_date_window", raw, source, response,
                        error="session fell outside requested date window"))
                    continue
            except (ParseError, TypeError, ValueError) as error:
                rejections.append(_rejection(
                    "event_parse_failed", raw, source, response, error=str(error)))
                continue
            if event["status"] == "cancelled":
                rejections.append(_rejection("cancelled", raw, source, response))
            else:
                events.append(event)
        parse_failed = any(item["reason"] == "event_parse_failed" for item in rejections)
        return source_result(
            state=("validation_failed" if parse_failed or date_contract_failed else
                   "ok" if events else "empty_verified"),
            method=METHOD, recipe_version=PARSER_VERSION,
            started_at=started_at, finished_at=_now_iso(), events=events,
            rejections=rejections,
            artifacts=self.artifacts.items if self.artifacts is not None else [],
            detail=("Momence host {} returned {} session(s) in the requested window "
                    "{} through {} across {} page(s).").format(
                        host_id, expected_total, seen_date, window_end, pages),
            error=("Momence API returned a session outside the requested date window"
                   if date_contract_failed else
                   "one or more Momence sessions could not be parsed" if parse_failed else None),
        )


def _host_id(source):
    value = source.get("momence_host_id")
    if isinstance(value, bool):
        value = None
    try:
        host_id = int(value)
    except (TypeError, ValueError) as error:
        raise ParseError("source is missing a valid momence_host_id") from error
    if host_id < 1:
        raise ParseError("source is missing a valid momence_host_id")
    return host_id


def _sessions_url(host_id, start_date, end_date, timezone, page):
    query = urlencode({
        "pageSize": PAGE_SIZE, "page": page, "fromDate": start_date.isoformat(),
        "toDate": end_date.isoformat(), "timeZone": timezone,
    })
    return "{}/host-plugins/host/{}/host-schedule/sessions?{}".format(
        READONLY_API, host_id, query)


def _payload(response, requested_page):
    try:
        payload = json.loads(response.text)
    except json.JSONDecodeError as error:
        raise ParseError("Momence host schedule did not return valid JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("payload"), list):
        raise ParseError("Momence host schedule is missing payload list")
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        raise ParseError("Momence host schedule is missing pagination")
    for key in ("page", "pageSize", "totalCount"):
        if isinstance(pagination.get(key), bool) or not isinstance(pagination.get(key), int):
            raise ParseError("Momence pagination {} is invalid".format(key))
    if pagination["page"] != requested_page or pagination["pageSize"] < 1 \
            or pagination["totalCount"] < 0:
        raise ParseError("Momence pagination does not match requested page")
    if len(payload["payload"]) > pagination["pageSize"]:
        raise ParseError("Momence payload exceeds pageSize")
    if pagination["totalCount"] == 0 and payload["payload"]:
        raise ParseError("Momence empty pagination has session payload")
    return payload


def _event(raw, source, response, host_id, timezone):
    if not isinstance(raw, dict):
        raise ParseError("session is not an object")
    if raw.get("hostId") != host_id:
        raise ParseError("session does not belong to configured host")
    session_id = str(raw.get("id") or "").strip()
    title = str(raw.get("sessionName") or "").strip()
    url = str(raw.get("link") or "").strip()
    if not session_id or not title or not url or not raw.get("startsAt"):
        raise ParseError("session is missing id, name, link, or startsAt")
    if not isinstance(raw.get("freeEvent"), bool):
        raise ParseError("session freeEvent is invalid")
    start = _local_datetime(raw["startsAt"], timezone)
    end = _local_datetime(raw.get("endsAt"), timezone, required=False)
    price = _price(raw)
    return {
        "title": title, "start": start, "end": end, "url": url, "signup_url": url,
        "host": str(source.get("title") or "").strip(),
        "venue": str(raw.get("location") or "").strip(), "neighborhood": "", "address": "",
        "price": price, "is_free": raw["freeEvent"],
        "description": _description(raw),
        "capacity_flag": _capacity_flag(raw),
        "status": "cancelled" if raw.get("isCancelled") else "active",
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": session_id,
        "fetched_at": response.fetched_at, "parser_version": PARSER_VERSION,
        "content_hash": response.content_hash, "extracted_json": raw,
    }


def _local_datetime(value, timezone, required=True):
    if not value:
        if required:
            raise ParseError("missing datetime")
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    zone = ZoneInfo(timezone)
    return (parsed.replace(tzinfo=zone) if parsed.tzinfo is None else
            parsed.astimezone(zone)).isoformat()


def _price(raw):
    if raw.get("freeEvent"):
        return "Free"
    value = raw.get("fixedTicketPrice")
    if isinstance(value, bool) or value is None:
        return ""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    return "${:g}".format(amount)


def _description(raw):
    text = str(raw.get("level") or "").strip()
    teacher = str(raw.get("teacher") or "").strip()
    return "Facilitator: {}. {}".format(teacher, text).strip() if teacher else text


def _capacity_flag(raw):
    capacity, sold, remaining = raw.get("capacity"), raw.get("ticketsSold"), raw.get("remainingSpots")
    values = []
    if isinstance(capacity, int) and not isinstance(capacity, bool):
        values.append("{} capacity".format(capacity))
    if isinstance(sold, int) and not isinstance(sold, bool):
        values.append("{} sold".format(sold))
    if isinstance(remaining, int) and not isinstance(remaining, bool):
        values.append("{} remaining".format(remaining))
    return "; ".join(values) or None


def _rejection(reason, raw, source, response, error=None):
    return {"reason": reason, "raw": {
        "source_id": source["id"], "source_listing_url": source["url"],
        "source_url": response.url, "source_event_id": raw.get("id") if isinstance(raw, dict) else None,
        "parser_version": PARSER_VERSION, "error": error, "extracted_json": raw,
    }}


def _now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")
