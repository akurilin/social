"""Happy-path smoke check for a running events MCP server.

    python3 -m mcp_server.smoke                     # local server
    python3 -m mcp_server.smoke https://<mac>.<tailnet>.ts.net/social/mcp

Prints PASS/FAIL lines and exits nonzero on any failure.
"""

import json
import sys
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8767/mcp"
EXPECTED_TOOLS = {"search_events", "get_event"}


def post(url, message):
    request = urllib.request.Request(
        url, data=json.dumps(message).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return response.status, json.loads(body) if body else None


def call(url, name, arguments):
    _, body = post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}})
    result = body["result"]
    if result.get("isError"):
        raise AssertionError(result["content"][0]["text"])
    return result["structuredContent"]


def main(url):
    checks = []

    def check(label, condition):
        checks.append(condition)
        print("{} {}".format("PASS" if condition else "FAIL", label))

    _, body = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "1"}}})
    check("initialize", body["result"]["serverInfo"]["name"] == "social-events")
    status, _ = post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("notifications/initialized accepted", status == 202)
    _, body = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in body["result"]["tools"]}
    check("tools/list has {}".format(", ".join(sorted(EXPECTED_TOOLS))), EXPECTED_TOOLS <= names)

    found = call(url, "search_events", {"sort": "rank", "limit": 3})
    check("search_events found {} upcoming events".format(found["total"]), bool(found["events"]))
    if found["events"]:
        first = found["events"][0]
        event = call(url, "get_event", {"event_id": first["id"]})
        check("get_event returned {!r}".format(event["title"]), event["id"] == first["id"])

    if all(checks):
        print("All checks passed.")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL))
