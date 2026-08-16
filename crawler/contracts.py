"""Shared contracts for procedural source adapters."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from typing import Annotated, Dict, Literal, Mapping, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, JsonValue, ValidationError


class CrawlerError(Exception):
    """Base error for a procedural crawl."""


class FetchError(CrawlerError):
    """The source could not be downloaded."""


class RobotsDenied(CrawlerError):
    """The source does not permit this crawler path."""


class ParseError(CrawlerError):
    """The source was downloaded but did not match its parser contract."""


class _ContractModel(BaseModel):
    """Strict base model for data passed from an adapter to the event store."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _aware_iso_datetime(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("datetime must use ISO 8601 format") from error
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a UTC offset")
    return value


AwareIsoDatetime = Annotated[str, AfterValidator(_aware_iso_datetime)]


class RawEvent(_ContractModel):
    """One validated factual event with its fetch and parser provenance."""

    title: str
    start: AwareIsoDatetime
    end: Optional[AwareIsoDatetime]
    url: str
    signup_url: str
    host: str
    venue: str
    neighborhood: str
    address: str
    price: str
    is_free: Optional[bool]
    description: str
    capacity_flag: Optional[str]
    status: Literal["active", "cancelled", "postponed"]
    source_id: str
    source_listing_url: str
    source_url: str
    source_event_id: str
    fetched_at: AwareIsoDatetime
    parser_version: str
    content_hash: str
    extracted_json: JsonValue
    explicit_age_min: Optional[int] = None
    explicit_age_max: Optional[int] = None
    explicit_age_label: Optional[str] = None
    orientation_scope: Optional[str] = None


class Rejection(_ContractModel):
    """One rejected source value; raw evidence can have any JSON shape."""

    reason: str
    raw: JsonValue


class Artifact(_ContractModel):
    """One saved response used as source-run evidence."""

    kind: str
    path: str
    url: str
    sha256: str
    fetched_at: AwareIsoDatetime


class SourceRunResult(_ContractModel):
    """Validated source-run metadata returned by one adapter."""

    state: Literal[
        "ok", "ok_via_fallback", "empty_verified", "empty_suspicious",
        "parse_failed", "validation_failed", "fetch_failed", "auth_required",
        "blocked",
    ]
    method: Literal["python_adapter"]
    recipe_version: str
    started_at: AwareIsoDatetime
    finished_at: AwareIsoDatetime
    artifacts: list[Artifact]
    detail: str
    error: Optional[str]


class AdapterResult(_ContractModel):
    """Complete validated payload accepted by stage_source_result."""

    source: SourceRunResult
    events: list[RawEvent]
    rejections: list[Rejection]


def validate_adapter_result(payload) -> Dict[str, object]:
    """Validate any adapter payload and return its existing dictionary shape."""

    try:
        validated = AdapterResult.model_validate(payload)
    except ValidationError as error:
        problems = []
        for item in error.errors(include_url=False):
            location = ".".join(str(part) for part in item["loc"])
            problems.append("{}: {}".format(location, item["msg"]))
        raise ParseError("adapter result failed runtime schema validation: {}".format(
            "; ".join(problems[:5]))) from error
    return validated.model_dump(mode="python", exclude_unset=True)


@dataclasses.dataclass(frozen=True)
class HttpResponse:
    """One HTTP response with the provenance needed by a RawEvent."""

    url: str
    status: int
    body: bytes
    headers: Mapping[str, str]
    fetched_at: str

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        return self.body.decode(charset or "utf-8", errors="replace")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@dataclasses.dataclass(frozen=True)
class EventStub:
    """A detail URL discovered on a source listing page."""

    url: str
    title: str = ""
    date_hint: Optional[dt.date] = None


def source_result(
    *,
    state: str,
    method: str,
    recipe_version: str,
    started_at: str,
    finished_at: str,
    events=None,
    rejections=None,
    artifacts=None,
    detail: str = "",
    error: Optional[str] = None,
) -> Dict[str, object]:
    """Build and validate the result accepted by stage_source_result."""

    payload = {
        "source": {
            "state": state,
            "method": method,
            "recipe_version": recipe_version,
            "started_at": started_at,
            "finished_at": finished_at,
            "artifacts": list(artifacts or []),
            "detail": detail,
            "error": error,
        },
        "events": list(events or []),
        "rejections": list(rejections or []),
    }
    return validate_adapter_result(payload)
