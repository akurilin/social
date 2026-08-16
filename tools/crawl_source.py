#!/usr/bin/env python3
"""Run one planned procedural source adapter and stage its result."""

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler.runner import run_planned_source
from tools import events_store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an earlier staged result while the run is active",
    )
    args = parser.parse_args(argv)
    try:
        payload, event_count, rejection_count = run_planned_source(
            args.run_id,
            args.source_id,
            database=os.environ.get("SOCIAL_DB", events_store.DB),
            replace=args.replace,
        )
    except ValueError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        return 1
    source = payload["source"]
    print("procedural crawl {}: {} events, {} rejections, state={}".format(
        args.source_id, event_count, rejection_count, source["state"]))
    if source.get("error"):
        print("detail: {}".format(source["error"]), file=sys.stderr)
    return 0 if source["state"] in {"ok", "empty_verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
