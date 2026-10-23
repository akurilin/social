"""Evaluate robots.txt with RFC 9309 matching rules."""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit


# Keep every printable ASCII character so only other octets are percent-encoded.
_SAFE_ASCII = "".join(chr(code) for code in range(0x21, 0x7F))


class RobotsRules:
    """Answer whether one user agent may fetch a URL.

    The longest matching rule wins, and an allow rule wins a tie. Python's
    urllib.robotparser applies the first matching line instead, which denies
    files such as "Disallow: /" followed by "Allow: /customer".
    """

    def __init__(self, groups=()):
        self.groups = list(groups)

    @classmethod
    def parse(cls, lines):
        groups, agents, rules, in_rules = [], [], [], False
        for line in lines:
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if in_rules:
                    groups.append((agents, rules))
                    agents, rules, in_rules = [], [], False
                agents.append(_product_token(value))
            elif field in ("allow", "disallow") and agents:
                in_rules = True
                if value:
                    rules.append((field == "allow", value))
        if agents:
            groups.append((agents, rules))
        return cls(groups)

    def can_fetch(self, user_agent, url):
        parts = urlsplit(url)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        if path == "/robots.txt":
            return True
        token = _product_token(user_agent)
        rules = [rule for agents, group in self.groups if token in agents for rule in group]
        if not any(token in agents for agents, _ in self.groups):
            rules = [rule for agents, group in self.groups if "*" in agents for rule in group]
        best = None
        for allow, pattern in rules:
            if _matches(pattern, path):
                candidate = (len(pattern), allow)
                if best is None or candidate > best:
                    best = candidate
        return True if best is None else best[1]


def _product_token(value):
    return value.split("/", 1)[0].strip().lower()


def _matches(pattern, path):
    pattern = quote(pattern, safe=_SAFE_ASCII)
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    expression = ".*".join(re.escape(piece) for piece in pattern.split("*"))
    return re.match(expression + ("$" if anchored else ""), path) is not None
