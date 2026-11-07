#!/usr/bin/env python3
"""Run one full event sync per Eastern day from a macOS launch agent."""

import argparse
import datetime as dt
import fcntl
import os
import plistlib
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler import sync_log
from tools import events_store

EASTERN = ZoneInfo("America/New_York")
START_HOUR = 8
CHECK_MINUTE = 0
RUN_TIMEOUT_SECONDS = 3 * 60 * 60
PRIVATE_FILE_MODE = 0o600
JOB_LABEL = "local.social.daily-sync"
AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / (JOB_LABEL + ".plist")
LOG_DIR = Path.home() / "Library" / "Logs" / "SocialCrawler"
LOG_PATH = LOG_DIR / "daily-sync.log"
LOCK_PATH = ROOT / ".cache" / "daily-sync.lock"
GOOD_STATES = {"ok", "ok_via_fallback", "empty_verified"}


def enabled_sources():
    # The catalog defines coverage; the database records each completed run.
    catalog = events_store.load_catalog()
    errors = events_store.validate_catalog(catalog)
    if errors:
        raise ValueError("Invalid sources.json: " + "; ".join(errors))
    return {source["id"] for source in catalog["sources"]
            if source.get("enabled", True)}


def full_runs(seen_date, source_ids):
    con = events_store.connect()
    try:
        rows = con.execute(
            "SELECT id, state FROM runs WHERE seen_date=? "
            "ORDER BY started_at DESC, id DESC", (seen_date,)).fetchall()
    finally:
        con.close()
    for row in rows:
        sources = events_store.load_source_runs(row["id"])
        if {source["source_id"] for source in sources} == source_ids:
            yield dict(row), sources


def finished_run(seen_date, source_ids):
    for run, sources in full_runs(seen_date, source_ids):
        if run["state"] != "completed":
            continue
        if any(row["event"] == "sync.completed"
               for row in sync_log.read_records(run["id"])):
            return run, sources
    return None


def latest_full_run(seen_date, source_ids):
    return next(full_runs(seen_date, source_ids), None)


def notify(message, title):
    # A local Notification Center notice reports completion without Desktop.
    script = "display notification {} with title {}".format(
        apple_quote(message), apple_quote(title))
    result = subprocess.run(["/usr/bin/osascript", "-e", script],
                            capture_output=True, text=True)
    if result.returncode:
        print("Notification failed: " + result.stderr.strip(), file=sys.stderr)


def apple_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def append_log(message):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.touch(mode=PRIVATE_FILE_MODE, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log:
        log.write("{} {}\n".format(dt.datetime.now(dt.timezone.utc).isoformat(), message))


def run_sync(seen_date, previous, source_ids, codex_path):
    # Resume an unfinished full run so a retry does not abandon staged work.
    run_id = previous[0]["id"] if previous else None
    if run_id and previous[0]["state"] == "completed":
        task = ("Finish ranking and reporting full run {} for {}. "
                "Do not create a new run.").format(run_id, seen_date)
    elif run_id:
        task = ("Resume full run {} for {}. Complete retrieval, finalization, "
                "ranking, and reporting.").format(run_id, seen_date)
    else:
        task = ("Run a full sync for {}. Complete retrieval, finalization, "
                "ranking, and reporting.").format(seen_date)
    prompt = (
        "Read and follow .agents/skills/sync-events/SKILL.md. " + task + " "
        "Read AGENTS.md and the full sources.json before the run. "
        "Do not read EVENT_RANKING_CRITERIA.md until the final ranking phase. "
        "Use the Python Playwright adapters and browser fallback when needed. "
        "Do not ask for input. If a critical source fails after fallback, stop "
        "and report the failure; never allow critical failures. "
        "End with the required short coverage report."
    )
    # Chromium needs the macOS Mach service denied by the Codex workspace sandbox.
    command = [str(codex_path), "exec", "--cd", str(ROOT),
               "--ignore-user-config", "--ephemeral", "--sandbox",
               "danger-full-access", prompt]
    append_log("Starting sync for " + seen_date)
    with LOG_PATH.open("a", encoding="utf-8") as log:
        try:
            result = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    timeout=RUN_TIMEOUT_SECONDS)
            log.write("\nCodex exit: {}\n".format(result.returncode))
        except subprocess.TimeoutExpired:
            log.write("\nCodex timed out\n")
    finished = finished_run(seen_date, source_ids)
    if finished:
        run, sources = finished
        issues = sum(source["state"] not in GOOD_STATES for source in sources)
        totals = events_store.load_run_totals(run["id"])
        message = "Daily sync finished: {} new events, {} source issue{}.".format(
            totals["new"], issues, "" if issues == 1 else "s")
        append_log(message + " Run: " + run["id"])
        notify(message, "Social Crawler: review needed" if issues else "Social Crawler")
        return 0
    append_log("Daily sync failed; retry at the next hourly check.")
    notify("Daily sync failed. It will retry next hour. Check the sync log.",
           "Social Crawler")
    return 1


def check():
    seen_date = dt.datetime.now(EASTERN).date().isoformat()
    source_ids = enabled_sources()
    finished = finished_run(seen_date, source_ids)
    if finished:
        run, sources = finished
        issues = sum(source["state"] not in GOOD_STATES for source in sources)
        print("Daily sync complete: {} ({} sources, {} issue{})".format(
            run["id"], len(sources), issues, "" if issues == 1 else "s"))
    else:
        print("Daily sync due: {}".format(seen_date))


def install():
    codex_path = shutil.which("codex")
    if not codex_path:
        raise RuntimeError("Codex CLI is not installed")
    python_path = ROOT / ".venv" / "bin" / "python"
    if not python_path.is_file():
        python_path = Path(sys.executable)
    path = os.pathsep.join(dict.fromkeys([
        str(python_path.parent), str(Path(codex_path).parent),
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
        "/usr/sbin", "/sbin",
    ]))
    agent = {
        "Label": JOB_LABEL,
        "ProgramArguments": [str(python_path), str(Path(__file__).resolve()), "--run"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"PATH": path, "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True,
        "StartCalendarInterval": {"Minute": CHECK_MINUTE},
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.touch(mode=PRIVATE_FILE_MODE, exist_ok=True)
    LOG_PATH.chmod(PRIVATE_FILE_MODE)
    AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AGENT_PATH.open("wb") as output:
        plistlib.dump(agent, output)
    domain = "gui/{}".format(os.getuid())
    subprocess.run(["/bin/launchctl", "bootout", domain + "/" + JOB_LABEL],
                   capture_output=True)
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(AGENT_PATH)],
                   check=True)
    print("Installed " + str(AGENT_PATH))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--notify-test", action="store_true")
    args = parser.parse_args()
    if args.install:
        install()
        return 0
    if args.notify_test:
        notify("Local sync notices are ready.", "Social Crawler")
        return 0
    if args.check:
        check()
        return 0
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = dt.datetime.now(EASTERN)
        if now.hour < START_HOUR:
            return 0
        seen_date = now.date().isoformat()
        source_ids = enabled_sources()
        if finished_run(seen_date, source_ids):
            return 0
        codex_path = shutil.which("codex")
        if not codex_path:
            append_log("Codex CLI is not installed; retry next hour.")
            notify("Daily sync failed: Codex CLI is missing.", "Social Crawler")
            return 1
        return run_sync(seen_date, latest_full_run(seen_date, source_ids),
                        source_ids, codex_path)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Surface setup and database errors that occur before Codex can start.
        append_log(traceback.format_exc())
        notify("Daily sync failed. It will retry next hour. Check the sync log.",
               "Social Crawler")
        raise
