# Social Crawler web control center

The control center manages local event-ranking criteria, the crawler skill, sources, source health, runs, discoveries, and deduplicated events.

Start it from the repo root with `python3 social.py web`, or run `python3 web/start.py` to also open the control center in the default browser. Both install missing web dependencies before starting the local server.

The server listens only on `127.0.0.1`. It does not expose the application to the local network or the internet.

The local server watches Python modules, Jinja templates, CSS, and JavaScript. It reloads after you save a change, so you do not have to restart it. Refresh the browser to request the changed page or asset.
