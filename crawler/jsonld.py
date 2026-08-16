"""Generic HTML and JSON-LD extraction helpers."""

from __future__ import annotations

import json
from html.parser import HTMLParser


class HtmlDocument(HTMLParser):
    """Collect JSON-LD, visible text, and links without a browser."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.json_ld_blocks = []
        self.links = []
        self._json_ld_parts = None
        self._ignored_depth = 0
        self._text_parts = []
        self._link = None
        self.feed(html)
        self.close()

    @property
    def text(self):
        return "\n".join(part for part in self._text_parts if part)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script":
            if values.get("type", "").lower() == "application/ld+json":
                self._json_ld_parts = []
            else:
                self._ignored_depth += 1
            return
        if tag == "style":
            self._ignored_depth += 1
            return
        if tag == "a":
            self._link = {"href": values.get("href", ""), "text_parts": []}

    def handle_endtag(self, tag):
        if tag == "script":
            if self._json_ld_parts is not None:
                self.json_ld_blocks.append("".join(self._json_ld_parts).strip())
                self._json_ld_parts = None
            elif self._ignored_depth:
                self._ignored_depth -= 1
            return
        if tag == "style" and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if tag == "a" and self._link is not None:
            self.links.append({
                "href": self._link["href"],
                "text": " ".join(self._link["text_parts"]).strip(),
            })
            self._link = None

    def handle_data(self, data):
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)
            return
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        self._text_parts.append(value)
        if self._link is not None:
            self._link["text_parts"].append(value)


def load_json_ld(document):
    """Return each valid top-level JSON-LD value from an HTML document."""

    values = []
    for block in document.json_ld_blocks:
        if not block:
            continue
        try:
            values.append(json.loads(block))
        except json.JSONDecodeError:
            continue
    return values


def find_typed_nodes(value, type_name):
    """Find JSON-LD objects whose @type matches type_name."""

    found = []

    def visit(item):
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        raw_types = item.get("@type")
        types = raw_types if isinstance(raw_types, list) else [raw_types]
        if any(_type_matches(value, type_name) for value in types):
            found.append(item)
        for child in item.values():
            visit(child)

    visit(value)
    return found


def _type_matches(value, expected):
    if not isinstance(value, str):
        return False
    return value == expected or value.rstrip("/").rsplit("/", 1)[-1] == expected
