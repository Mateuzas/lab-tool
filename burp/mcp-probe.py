#!/usr/bin/env python3
"""Minimal MCP-over-SSE client for checking the Burp MCP Server extension (stdlib only).

Usage: python3 burp/mcp-probe.py [tool_name [json_args]]
With no tool, lists the tools. Only use read-only tools here.
"""
import json
import queue
import sys
import threading
import urllib.request

BASE = "http://127.0.0.1:9876"
events = queue.Queue()


def read_sse(path):
    resp = urllib.request.urlopen(BASE + path, timeout=180)
    event, data = None, []
    for raw in resp:
        line = raw.decode().rstrip("\r\n")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "" and data:
            events.put((event, "\n".join(data)))
            event, data = None, []


def wait(kind, want_id=None):
    while True:
        event, data = events.get(timeout=180)
        if kind == "endpoint" and event == "endpoint":
            return data
        if kind == "message" and event in (None, "message"):
            msg = json.loads(data)
            if msg.get("id") == want_id:
                return msg


def main():
    threading.Thread(target=read_sse, args=("/",), daemon=True).start()
    endpoint = wait("endpoint")
    url = endpoint if endpoint.startswith("http") else BASE + endpoint
    next_id = [0]

    def rpc(method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            next_id[0] += 1
            body["id"] = next_id[0]
        req = urllib.request.Request(url, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=180).read()
        return None if notify else wait("message", next_id[0])

    init = rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "kali-lab-probe", "version": "0"}})
    print("server:", init["result"].get("serverInfo"))
    rpc("notifications/initialized", notify=True)

    if len(sys.argv) == 1:
        for t in rpc("tools/list")["result"]["tools"]:
            print(f"- {t['name']}: {t.get('description', '')[:90]}")
        return
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    result = rpc("tools/call", {"name": sys.argv[1], "arguments": args})
    print(json.dumps(result.get("result", result), indent=1)[:4000])


if __name__ == "__main__":
    main()
