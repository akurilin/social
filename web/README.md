# Social Crawler web control center

The control center manages local event-ranking criteria, the crawler skill, sources, source health, runs, discoveries, and deduplicated events.

Start it from the repo root with `python3 social.py web`, or run `python3 web/start.py` to also open the control center in the default browser. Both install missing web dependencies before starting the local server.

The server listens only on `127.0.0.1`. It does not expose the application to the local network or the internet.

The local server watches Python modules, Jinja templates, CSS, and JavaScript. It reloads after you save a change, so you do not have to restart it. Refresh the browser to request the changed page or asset.

The Events and Calendar pages show social plans for today and the next 13 days.
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

Apply schema 7 with `python3 social.py migrate` before opening an older store.
The migration adds the calendar result table and keeps existing crawl data.

Implementation references: [Apple EventKit](https://developer.apple.com/documentation/eventkit/accessing-the-event-store),
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[Claude CLI output](https://code.claude.com/docs/en/headless#get-structured-output),
and [Claude model aliases](https://code.claude.com/docs/en/model-config#model-aliases).
