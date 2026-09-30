#!/usr/bin/env python3
"""
nse_mcp_watch.py - poll the NSE MCP servers until they respond, then fetch a stock price.

Usage (Windows cmd, from your project folder):
    python nse_mcp_watch.py --symbol RELIANCE --date 30-Sep-2026
    python nse_mcp_watch.py --symbol TCS --interval 60

Needs only the Python standard library (no pip installs). No token required.
Beeps and prints a message when a server connects. Press Ctrl+C to stop.
"""
import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

SERVERS = {
    "bhavcopy":  "https://mcp.nseindia.in/bhavcopy/cm/mcp",
    "cm-market": "https://mcp.nseindia.in/cmmkt/mcp",
}
# Words used to pick the right tool from each server's tools/list
TOOL_HINTS = {
    "bhavcopy":  ["bhavcopy", "historical", "history", "eod"],
    "cm-market": ["ltp", "quote", "last_price", "live"],
}
BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": "curl/8.4.0",
}


def now():
    return dt.datetime.now().strftime("%H:%M:%S")


def beep():
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 300)
    except Exception:
        print("\a", end="")


class Session:
    def __init__(self, url, timeout):
        self.url, self.timeout, self.sid, self.n = url, timeout, None, 0

    def _post(self, payload):
        headers = dict(BASE_HEADERS)
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            ctype = r.headers.get("Content-Type", "")
            body = r.read().decode("utf-8", "replace")
        return parse_body(body, ctype)

    def rpc(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}
        resp = self._post(msg)
        if resp is None:
            raise RuntimeError("empty response")
        if "error" in resp:
            raise RuntimeError(json.dumps(resp["error"]))
        return resp.get("result", {})

    def notify(self, method):
        try:
            self._post({"jsonrpc": "2.0", "method": method})
        except Exception:
            pass

    def connect(self):
        self.rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "nse-watch", "version": "1.0"},
        })
        self.notify("notifications/initialized")


def parse_body(body, ctype):
    body = body.strip()
    if not body:
        return None
    if "event-stream" in ctype or body.startswith(("event:", "data:")):
        last = None
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                    if "result" in obj or "error" in obj:
                        last = obj
                except ValueError:
                    pass
        return last
    return json.loads(body)


def pick_tool(tools, hints):
    for hint in hints:
        for t in tools:
            if hint in (t["name"] + " " + t.get("description", "")).lower():
                return t
    return None


def build_args(schema, symbol, date_str):
    args = {}
    for key in schema.get("properties", {}):
        k = key.lower()
        if any(s in k for s in ("symbol", "ticker", "scrip", "stock", "security", "company")):
            args[key] = symbol
        elif "date" in k or "from" in k or "to" == k or "start" in k or "end" in k:
            args[key] = date_str
        elif k == "series":
            args[key] = "EQ"
    return args


def date_variants(text):
    variants = [text]
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            d = dt.datetime.strptime(text, fmt)
            for out in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                v = d.strftime(out)
                if v not in variants:
                    variants.append(v)
            break
        except ValueError:
            continue
    return variants


def show_result(result):
    for c in result.get("content", []):
        if c.get("type") == "text":
            txt = c["text"]
            print(txt[:3000] + ("\n...[truncated]" if len(txt) > 3000 else ""))


def try_server(name, url, symbol, date_text, timeout):
    s = Session(url, timeout)
    s.connect()
    print(f"[{now()}] {name}: CONNECTED")
    beep()
    tools = s.rpc("tools/list").get("tools", [])
    print(f"[{now()}] {name}: {len(tools)} tools -> " + ", ".join(t["name"] for t in tools))
    with open(f"nse_tools_{name}.json", "w", encoding="utf-8") as f:
        json.dump(tools, f, indent=2)

    tool = pick_tool(tools, TOOL_HINTS[name])
    if not tool:
        print(f"[{now()}] {name}: no matching tool found; see nse_tools_{name}.json")
        return
    schema = tool.get("inputSchema", {})
    print(f"[{now()}] {name}: calling '{tool['name']}' for {symbol}")
    last_err = None
    for date_str in date_variants(date_text):
        args = build_args(schema, symbol, date_str)
        try:
            result = s.rpc("tools/call", {"name": tool["name"], "arguments": args})
        except Exception as e:
            last_err = str(e)
            continue
        if result.get("isError"):
            last_err = json.dumps(result.get("content"))[:300]
            continue
        print(f"--- {name} / {tool['name']} / args={args} ---")
        show_result(result)
        return
    print(f"[{now()}] {name}: tool call failed ({last_err}).")
    print(f"    Input schema for '{tool['name']}': {json.dumps(schema)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="RELIANCE")
    ap.add_argument("--date", default="30-Sep-2026")
    ap.add_argument("--interval", type=int, default=120, help="seconds between attempts")
    ap.add_argument("--timeout", type=int, default=30)
    a = ap.parse_args()

    pending = dict(SERVERS)
    attempt = 0
    print(f"Watching {', '.join(pending)} for {a.symbol} on {a.date}. Ctrl+C to stop.")
    while pending:
        attempt += 1
        for name, url in list(pending.items()):
            try:
                try_server(name, url, a.symbol.upper(), a.date, a.timeout)
                del pending[name]
            except urllib.error.HTTPError as e:
                print(f"[{now()}] #{attempt} {name}: HTTP {e.code}")
            except Exception as e:
                print(f"[{now()}] #{attempt} {name}: {type(e).__name__}: {e}")
        if pending:
            time.sleep(a.interval)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
