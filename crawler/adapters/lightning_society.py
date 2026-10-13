"""Placeholder for Lightning Society newsletters received through Feedbin."""

import datetime as dt

from crawler.contracts import source_result


ADAPTER_ID = "lightning_society_feedbin"
PARSER_VERSION = "lightning-society-feedbin-placeholder-v1"
BLOCKED_REASON = (
    "Feedbin newsletter extraction is not implemented. Receive a Lightning "
    "Society newsletter in Feedbin, confirm its feed ID, then implement and "
    "test extraction through the official Feedbin API before enabling this source."
)


class LightningSocietyFeedbinAdapter:
    id = ADAPTER_ID
    version = PARSER_VERSION

    def __init__(self, client, artifacts=None):
        self.client = client
        self.artifacts = artifacts

    def crawl(self, source, seen_date, lookahead_days, timezone):
        # Report incomplete setup so a placeholder cannot prove an empty calendar.
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        return source_result(
            state="blocked",
            method="python_adapter",
            recipe_version=self.version,
            started_at=now,
            finished_at=now,
            detail=BLOCKED_REASON,
            error=BLOCKED_REASON,
        )
