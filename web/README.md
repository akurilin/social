# Social Crawler web control center

The control center manages local event-ranking criteria, the crawler skill, sources, source health, runs, discoveries, and deduplicated events.

Start it from the repo root with `python3 social.py web`, or run `python3 web/start.py` to also open the control center in the default browser. Both install missing web dependencies before starting the local server.

The server listens only on `127.0.0.1`. It does not expose the application to the local network or the internet.

### Add or remove an event in Apple Calendar

Apple Calendar can sync a saved event to a linked Google account. On macOS,
run `python3 social.py calendars` to list writable calendars. Copy the exact
`Account/Calendar` name into your local `.env` file. The tracked `sample.env`
shows the format without a personal account name:

```sh
SOCIAL_APPLE_CALENDAR=name@example.com/main
```

Replace the example with the exact name shown by the command. Do not add
quotes; the value can include spaces if the account or calendar name has them.

Each user sets this value on their own Mac. The app reads it when a page opens,
so a server restart is not needed. The **Events** table has an **Add** button
in its last column. It shows **Added** and **Remove** when an event is in the
selected calendar. Remove deletes one matching calendar event, but keeps the
crawler record. If several events match, remove one in Apple Calendar.
The app disables **Add** and shows a setup message when the calendar is not
writable or access is missing. If an event has no end time, enter one in the
dialog before you save. The event detail page has the same actions.
The linked account handles later sync. A local save does not confirm that
Google has received the event.

The local server watches Python modules, Jinja templates, CSS, and JavaScript. It reloads after you save a change, so you do not have to restart it. Refresh the browser to request the changed page or asset.

Events and Calendar each save filter selections in your browser. Calendar also
saves the month; Events saves search and sort order. Explicit filter URLs take
priority. Select **Reset filters** to restore that page's defaults.

The Events and Calendar pages show social plans for today and the next 13 days.
Select **Stop refresh** during a check to stop it and keep the last saved result.

Select **Refresh calendar** to read Apple Calendar and check the events with
GPT-5.6 Luna, or Claude Sonnet if Codex CLI is not installed.
Green means a social plan; red means no social plan was found.
Pastel blue means unchecked or unavailable. Time in cafés, libraries and hotel
bars counts, including solo work, study or reading. Online events and
professional gatherings do not count. Page loads and crawler syncs do not
start a check.

Calendar access needs macOS, Apple Command Line Tools (`xcode-select --install`),
and calendar permission. The reader compiles on the first refresh. If access
is denied, allow the reader or its launcher in System Settings > Privacy &
Security > Calendars. Install and sign in to at least one supported client:

- Codex CLI: `codex exec --model gpt-5.6-luna` is the first choice.
  Use `codex login status` to check its sign-in method.
- Claude Code: if `codex` is absent from `PATH`, the app uses
  `claude --print --model sonnet`. The `sonnet` alias selects the latest Sonnet
  supported by the CLI; it currently selects Sonnet 5 with direct Anthropic
  access. Keep Claude Code current. Use `claude auth login` to sign in and
  `claude auth status` to check access.

Both clients reuse their saved sign-in and subscription limits. No API key
setup is needed. An installed Codex client stays selected if its check fails;
the app does not retry that request with Claude. A failed read or model check
turns the window blue. An empty calendar needs neither client.

The check sends calendar titles, times, calendar names, locations, notes and
URLs to the selected model through its CLI. Both clients run in a temporary
folder with session persistence and local instructions disabled. Codex runs
with shell, browser access and plugins disabled. Claude runs in safe mode
with tools disabled; it does not use `--bare`, which requires API credentials.
`social.db` stores only the latest day states, check time, time zone, selected
model (or alias) and status message. Refresh replaces this
result on both pages. New days remain blue until checked. Saved colors refer
to the displayed check time; calendar edits need another refresh.

### Calendar diagnostics

Select **View logs** beside the calendar check, or open
`http://127.0.0.1:8000/calendar-coverage/logs`.
Logs start with the next refresh; earlier errors cannot be recovered.

Each refresh has a run ID. Logs record calendar reads, model selection,
event and prompt sizes, stage times, process exit codes, timeouts, stops,
validation failures, and final day counts. A start with no end record means
work is still active or the server stopped before it could write the result.
CLI errors are reduced to known connection, limit, sign-in, model-access,
and argument-error signals. Calendar text, prompts, full CLI output, and
credentials are not saved. These signals help diagnosis but cannot prove
why a remote model stalled.

The local file is `.cache/logs/calendar-coverage.jsonl`. It rotates at 1 MB
and keeps three backups. **View logs** includes all retained files.
To follow new records from the project folder:

```sh
tail -f .cache/logs/calendar-coverage.jsonl
```

Calendar reads and compilation each have a 120-second limit. Model calls
have a 180-second limit. Failure leaves days unchecked, not empty.
If the log cannot be written, the server prints a warning and continues.

Apply schema 7 with `python3 social.py migrate` before opening an older store.
The migration adds the calendar result table and keeps existing crawl data.

Implementation references: [Apple EventKit](https://developer.apple.com/documentation/eventkit/accessing-the-event-store),
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[Claude CLI output](https://code.claude.com/docs/en/headless#get-structured-output),
and [Claude model aliases](https://code.claude.com/docs/en/model-config#model-aliases).
