#!/usr/bin/env python3
"""Launcher for the web control center that also opens the browser.

Replaces the former start.command: installs missing web dependencies,
starts the local server, and opens http://127.0.0.1:8000 in the default
browser. Equivalent to `python3 social.py web` plus the browser opening.
"""

import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from social import ensure_web_deps


def main():
    ensure_web_deps()
    # Give the server a moment to bind before opening the browser.
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8000")).start()
    from web.__main__ import main as serve

    serve()


if __name__ == "__main__":
    main()
