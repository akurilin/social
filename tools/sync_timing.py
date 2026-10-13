#!/usr/bin/env python3
"""Show source timings or mark the end of a fully ranked sync."""

import argparse
import datetime as dt
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler import sync_log
from tools import events_store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--complete', action='store_true', help='mark a finalized, fully ranked sync complete')
    args = parser.parse_args(argv)
    rows = sync_log.read_records(args.run_id)
    if args.complete:
        # The completion marker must follow storage and fit ranking.
        con = events_store.connect()
        try:
            run = con.execute('SELECT state FROM runs WHERE id=?', (args.run_id,)).fetchone()
            missing = con.execute("""SELECT COUNT(*) FROM discoveries d JOIN events e ON e.id=d.event_id
                WHERE d.run_id=? AND d.outcome='new' AND e.rank IS NULL""", (args.run_id,)).fetchone()[0]
            if not run or run['state'] != 'completed' or missing:
                parser.error('Run must be finalized and all its new events ranked.')
        finally:
            con.close()
        if not any(row['event'] == 'sync.completed' for row in rows):
            starts = [row for row in rows if row['event'] == 'sync.started']
            if not starts:
                parser.error('No sync start in the retained log.')
            elapsed = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(starts[0]['timestamp'])).total_seconds()
            sync_log.record('sync.completed', run_id=args.run_id, duration_seconds=round(elapsed, sync_log.SECONDS_PRECISION))
            rows = sync_log.read_records(args.run_id)
    print('Timing log:', sync_log.log_path(args.run_id))
    print('Seconds: retrieval includes HTTP, parsing, retries, and artifact writes.')
    print('Queue is worker wait; stage wait is completed data waiting for source order.')
    print('HTTP and browser times include throttle waits; do not add nested timings.')
    print('{:<32} {:>10} {:>9} {:>11} {:>8} {:>8} {:>8} {:>9} {:>11}  {}'.format(
        'Source', 'Retrieve', 'Queue', 'Stage wait', 'Save', 'HTTP #', 'HTTP sec', 'Browser #', 'Browser sec', 'State'))
    stages = {}
    for row in rows:
        if row['event'] == 'stage.completed':
            stages.setdefault(row['source_id'], []).append(row)
    sources = [row for row in rows if row['event'] in {'source.completed', 'source.failed'}]
    # Match repeated attempts in timestamp order before sorting by duration.
    for row in sources:
        candidates = stages.get(row['source_id'], [])
        row['_stage'] = candidates.pop(0) if candidates else {}
    for row in sorted(sources, key=lambda row: row['duration_seconds'], reverse=True):
        stage = row['_stage']
        print('{:<32} {:>10.3f} {:>9.3f} {:>11} {:>8} {:>8} {:>8.3f} {:>9} {:>11.3f}  {}'.format(
            row['source_id'], row['duration_seconds'], row.get('queue_seconds', 0),
            '{:.3f}'.format(stage.get('stage_wait_seconds', 0)) if stage else 'pending',
            '{:.3f}'.format(stage['duration_seconds']) if stage else 'pending',
            row.get('http_count', 0), row.get('http_seconds', 0),
            row.get('browser_count', 0), row.get('browser_seconds', 0),
            row.get('state', row.get('error_type', ''))))
    for row in rows:
        if row['event'] in {'batch.completed', 'finalize-run.completed', 'rank.completed', 'sync.completed', 'audit.completed', 'fallback.completed'}:
            print('{} {}: {:.3f} seconds'.format(row['event'], row.get('source_id') or '', row['duration_seconds']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
