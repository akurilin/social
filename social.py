#!/usr/bin/env python3
"""Top-level entry point for the social event crawler app."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Mirrors requirements.txt; used to decide whether an install is needed
# before starting the web control center.
WEB_DEPS = ["fastapi", "jinja2", "pydantic", "uvicorn", "watchfiles"]

USAGE = """\
usage: python3 social.py <command>

commands:
  web    start the local web control center (installs deps if missing)
  test   run all validation: catalog-check on sources.json, then the test suite

`test` runs, in order:
  1. python3 tools/events_store.py catalog-check
     Validates sources.json: unique source ids, retrieval profiles referenced
     by each source exist, profiles declare primary recipe / required fields /
     empty rule, disabled sources carry a disabled_reason, and source schedules
     are absent.
  2. python3 -m unittest discover -s tests -v
     Pipeline and web control center behavior, including catalog round-trips.
"""


def _missing_web_deps():
    try:
        for name in WEB_DEPS:
            __import__(name)
        return []
    except ImportError as exc:
        return [str(exc)]


def ensure_web_deps():
    """Install requirements.txt when web dependencies are not importable."""
    if _missing_web_deps():
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
            check=True,
        )


def cmd_web():
    ensure_web_deps()
    from web.__main__ import main

    main()


def cmd_test():
    # Validation lives in two places; run both so there is exactly one
    # command to reach for after touching sources.json, tools/, or web/.
    checks = [
        ("catalog-check", [sys.executable, "tools/events_store.py", "catalog-check"]),
        ("tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]),
    ]
    failed = []
    for name, cmd in checks:
        print("==> {} {}".format(name, " ".join(cmd[1:])))
        if subprocess.run(cmd, cwd=ROOT).returncode != 0:
            failed.append(name)
    if failed:
        print("validation FAILED: {}".format(", ".join(failed)), file=sys.stderr)
        sys.exit(1)
    print("validation OK")


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return
    command = argv[0]
    if command == "web":
        cmd_web()
    elif command == "test":
        cmd_test()
    else:
        print(f"unknown command: {command}", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
