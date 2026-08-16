#!/usr/bin/env python3
"""Run all pending procedural adapters with bounded parallel retrieval."""

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler.runner import run_planned_adapters
from tools import events_store


SUCCESS_STATES = {"ok", "empty_verified"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="maximum concurrent source retrievals (default: 4)",
    )
    args = parser.parse_args(argv)

    try:
        summary = run_planned_adapters(
            args.run_id,
            database=os.environ.get("SOCIAL_DB", events_store.DB),
            workers=args.workers,
        )
    except ValueError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1

    failed_states = []
    for result in summary["results"]:
        print("procedural crawl {}: {} events, {} rejections, state={}".format(
            result["source_id"], result["event_count"],
            result["rejection_count"], result["state"]))
        if result.get("error"):
            print("detail: {}".format(result["error"]), file=sys.stderr)
        if result["state"] not in SUCCESS_STATES:
            failed_states.append(result["source_id"])

    for error in summary["errors"]:
        print("ERROR: {}: {}".format(
            error["source_id"], error["error"]), file=sys.stderr)

    print("procedural batch: {} staged, {} execution errors, {} "
          "non-procedural pending".format(
              len(summary["results"]), len(summary["errors"]),
              len(summary["skipped"])))
    if summary["skipped"]:
        print("non-procedural sources: {}".format(
            ", ".join(summary["skipped"])))
    return 1 if summary["errors"] or failed_states else 0


if __name__ == "__main__":
    raise SystemExit(main())
