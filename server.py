"""
NSE MCP Dashboard
------------------
A small local web app for a fintech analyst to talk to two NSE MCP servers:

  - bhavcopy (end-of-day settlement data): https://mcp.nseindia.in/bhavcopy/cm/mcp
  - CM market data (capital market):        https://msc.nseindia.in/cmmkt/mcp

The Bhavcopy/CM Market Data tabs are a generic explorer: pick any tool the
server publishes, fill in arguments as JSON, run it.

The Portfolio Analysis tab is a fixed pipeline against three confirmed
Bhavcopy tools: get_stock_history -> get_ltp_by_date -> moving_average.

Run:
    pip install -r requirements.txt
    python server.py
Then open http://127.0.0.1:8000
"""

import datetime as dt
import itertools
import json
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import technical

app = FastAPI(title="NSE MCP Dashboard")

# The two servers you gave me. Add more here if needed.
SERVERS: dict[str, str] = {
    "bhavcopy": "https://mcp.nseindia.in/bhavcopy/cm/mcp",
    "cm": "https://msc.nseindia.in/cmmkt/mcp",
}

_ID_COUNTER = itertools.count(1)
_INIT_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "nse-mcp-dashboard", "version": "0.1.0"},
}

# ---------------------------------------------------------------------------
# Portfolio Analysis pipeline, against the Bhavcopy server's confirmed tools:
#   get_stock_history(symbol, months, endDate) -> {"data": [...], "next_end_date": ...}
#   get_ltp_by_date(symbol, date)              -> a number (resolves to last working day)
#   moving_average(symbol, days)               -> a number
# ---------------------------------------------------------------------------
ANALYSIS_SERVER = "bhavcopy"
ANALYSIS_HISTORY_MONTHS = 6  # total months of history to fetch & chart (fetched in <=3-month chunks)


class CallToolRequest(BaseModel):
    server: str
    tool: str
    arguments: dict[str, Any] = {}


class StockAllocation(BaseModel):
    symbol: str
    allocation: float = 0


class AnalyzeRequest(BaseModel):
    stocks: list[StockAllocation]


def _to_float(value: Any) -> Optional[float]:
    """Coerce a tool's returned value (number or numeric string) to float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _extract_records(content: Any, path: str) -> list[dict[str, Any]]:
    """Pull a list of record dicts out of a tool response. Used by the generic
    Bhavcopy/CM explorer tabs — the Portfolio Analysis pipeline reads
    get_stock_history's known 'data' field directly instead.

    If `path` is given (dot-separated keys), descend through it first.
    Otherwise, if the response is already a list, use it as-is; if it's a
    dict, use the first value that looks like a list of records.
    """
    node = content
    if path:
        for part in path.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise ValueError(
                    f"result_path '{path}' not found in response (stopped at '{part}')"
                )

    if isinstance(node, list) and node and isinstance(node[0], dict):
        return node
    if isinstance(node, dict):
        for value in node.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value

    raise ValueError(
        "Could not find a list of records in the tool response. Check the tool's "
        "output shape on the tool's own tab, then set 'result_path' accordingly."
    )


def _server_url(server_key: str) -> str:
    if server_key not in SERVERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown server '{server_key}'. Known: {list(SERVERS)}",
        )
    return SERVERS[server_key]


def _describe_error(exc: BaseException) -> str:
    """Flatten (possibly nested) TaskGroup/ExceptionGroup errors into one
    readable string, so 'unhandled errors in a TaskGroup' turns into the
    actual underlying failure (SSL, auth, timeout, DNS, etc.)."""
    parts: list[str] = []

    def walk(e: BaseException) -> None:
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                walk(sub)
        else:
            parts.append(f"{type(e).__name__}: {e}")

    walk(exc)
    return " | ".join(parts) if parts else str(exc)


def _looks_empty(value: Any) -> bool:
    """True for None/{}/[] or a dict that's just {'type': ...} with nothing else."""
    if value in (None, {}, []):
        return True
    if isinstance(value, dict) and set(value.keys()) <= {"type"}:
        return True
    return False


async def _raw_mcp_request(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    params: dict[str, Any],
    session_id: Optional[str],
    expect_response: bool = True,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Send one JSON-RPC message over MCP's streamable-HTTP transport.

    Returns (result, session_id). Tolerant of both a plain JSON response
    and an SSE (`text/event-stream`) response, since servers may use either.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
    if expect_response:
        payload["id"] = next(_ID_COUNTER)

    resp = await client.post(url, json=payload, headers=headers)
    resp.raise_for_status()

    new_session_id = resp.headers.get("mcp-session-id", session_id)
    if not expect_response:
        return None, new_session_id

    content_type = resp.headers.get("content-type", "")
    data: Optional[dict[str, Any]] = None
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[len("data:") :].strip()
                if not chunk:
                    continue
                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
        if data is None:
            raise RuntimeError(f"Could not parse SSE response for '{method}'")
    else:
        data = resp.json()

    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")

    return data.get("result"), new_session_id


async def _mcp_handshake(client: httpx.AsyncClient, url: str) -> Optional[str]:
    _, session_id = await _raw_mcp_request(client, url, "initialize", _INIT_PARAMS, None)
    await _raw_mcp_request(client, url, "notifications/initialized", {}, session_id, expect_response=False)
    return session_id


async def _list_tools(url: str) -> list[dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            session_id = await _mcp_handshake(client, url)
            result, _ = await _raw_mcp_request(client, url, "tools/list", {}, session_id)
            tools = (result or {}).get("tools", [])
            return [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description") or "",
                    "input_schema": t.get("inputSchema") or {},
                }
                for t in tools
            ]
    except BaseException as exc:  # noqa: BLE001 - unwrap TaskGroup, re-raise readable
        raise RuntimeError(_describe_error(exc)) from exc


async def _call_tool(url: str, tool_name: str, arguments: dict[str, Any]) -> Any:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            session_id = await _mcp_handshake(client, url)
            result, _ = await _raw_mcp_request(
                client, url, "tools/call", {"name": tool_name, "arguments": arguments}, session_id
            )
            result = result or {}

            # Some servers put the real payload in `structuredContent` (part
            # of the newer MCP spec, meant for structured/tabular data) and
            # leave `content` as a minimal placeholder block. Prefer it when
            # present and non-empty.
            structured = result.get("structuredContent")
            if not _looks_empty(structured):
                return {"is_error": bool(result.get("isError", False)), "content": structured}

            # MCP tool results are a list of content blocks (text/json/etc).
            # We parse them leniently: servers don't always match the spec
            # exactly (e.g. a 'text' block missing its 'text' field), so we
            # fall back to whatever text-like field is actually present
            # rather than hard-failing on strict validation.
            blocks: list[Any] = []
            for block in result.get("content", []) or []:
                if not isinstance(block, dict):
                    blocks.append(block)
                    continue
                if block.get("type") == "text":
                    text = block.get("text")
                    if text is None:
                        text = block.get("data") or block.get("value") or block.get("content")
                    if text is None:
                        blocks.append(block)  # nothing text-like found; show raw block
                        continue
                    try:
                        blocks.append(json.loads(text))
                    except (json.JSONDecodeError, TypeError):
                        blocks.append(text)
                else:
                    blocks.append(block)

            friendly = blocks if len(blocks) != 1 else (blocks[0] if blocks else None)

            if _looks_empty(friendly):
                # Neither structuredContent nor content had anything usable.
                # Hand back the *entire* raw result so its real shape is visible
                # instead of guessing at more field names blind.
                return {
                    "is_error": bool(result.get("isError", False)),
                    "content": result,
                    "note": (
                        "Could not find a populated 'content' or 'structuredContent' field "
                        "— showing the full raw MCP result below so you can see its actual shape."
                    ),
                }

            return {
                "is_error": bool(result.get("isError", False)),
                "content": friendly,
            }
    except BaseException as exc:  # noqa: BLE001 - unwrap TaskGroup, re-raise readable
        raise RuntimeError(_describe_error(exc)) from exc


@app.get("/api/servers")
def get_servers():
    return {key: url for key, url in SERVERS.items()}


@app.get("/api/tools")
async def get_tools(server: str):
    url = _server_url(server)
    try:
        tools = await _list_tools(url)
    except Exception as exc:  # noqa: BLE001 - surface connection errors to UI
        raise HTTPException(status_code=502, detail=f"Could not reach {url}: {exc}")
    return {"server": server, "url": url, "tools": tools}


@app.post("/api/call")
async def call_tool(req: CallToolRequest):
    url = _server_url(req.server)
    try:
        result = await _call_tool(url, req.tool, req.arguments)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Call failed: {exc}")
    return result


async def _fetch_history(url: str, symbol: str, total_months: int) -> list[dict[str, Any]]:
    """Chain get_stock_history calls (max 3 months each) into one ascending,
    oldest-first list of {date, close, ...} records."""
    records: list[dict[str, Any]] = []
    end_date = "today"
    months_left = total_months
    while months_left > 0:
        chunk = min(3, months_left)
        raw = await _call_tool(url, "get_stock_history", {"symbol": symbol, "months": chunk, "endDate": end_date})
        if raw.get("is_error"):
            raise RuntimeError(f"get_stock_history failed: {raw.get('note') or raw.get('content')}")
        content = raw.get("content")
        data = content.get("data") if isinstance(content, dict) else None
        if not data:
            break
        records = data + records  # this chunk is older than what we already have
        months_left -= chunk
        next_end = content.get("next_end_date")
        if not next_end:
            break
        end_date = next_end
    return records


async def _search_symbols(url: str, query: str) -> list[dict[str, Any]]:
    raw = await _call_tool(url, "search_symbols", {"query": query})
    if raw.get("is_error"):
        raise RuntimeError(f"search_symbols failed: {raw.get('note') or raw.get('content')}")
    content = raw.get("content")
    return (content.get("results") if isinstance(content, dict) else None) or []


@app.get("/api/search")
async def search_symbol(query: str):
    """Resolve a typed company name/keyword to NSE symbols via search_symbols.

    The server matches on ticker text (not full company names), so if the full
    query finds nothing we retry with each word, longest first. Symbols that
    haven't traded in the last 30 days are dropped (delisted/renamed tickers).
    """
    url = _server_url(ANALYSIS_SERVER)
    q = query.strip()
    if not q:
        return {"query": q, "tried": [], "results": []}

    # Fallback words in the order typed: the brand is usually the first word
    # ("HDFC Bank" -> HDFC), and generic words like "Bank" match unrelated tickers.
    words: list[str] = []
    for w in q.replace("-", " ").split():
        if len(w) >= 3 and w.lower() not in (x.lower() for x in words):
            words.append(w)
    attempts = [q] + [w for w in words if w.lower() != q.lower()]

    tried: list[str] = []
    found: dict[str, dict[str, Any]] = {}
    try:
        for attempt in attempts:
            tried.append(attempt)
            for r in await _search_symbols(url, attempt):
                found.setdefault(r["symbol"], r)
            if found:
                break  # first attempt that returns anything wins
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Symbol search failed: {exc}")

    cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    active = [r for r in found.values() if str(r.get("last_date", "")) >= cutoff]
    q_upper = q.upper().replace(" ", "")
    active.sort(key=lambda r: (r["symbol"] != q_upper, len(r["symbol"]), r["symbol"]))

    return {
        "query": q,
        "tried": tried,
        "hidden_inactive": len(found) - len(active),
        "results": active[:10],
    }


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    url = _server_url(ANALYSIS_SERVER)
    today = dt.date.today().isoformat()
    results = []

    for stock in req.stocks:
        symbol = stock.symbol.strip().upper()
        entry: dict[str, Any] = {"symbol": symbol, "allocation": stock.allocation}
        try:
            # 1) Price history -> chart series + our own SMA/RSI/MACD
            records = await _fetch_history(url, symbol, ANALYSIS_HISTORY_MONTHS)
            if not records:
                raise ValueError("get_stock_history returned no data for this symbol.")

            dates = [str(r.get("date", "")) for r in records]
            closes: list[Optional[float]] = [_to_float(r.get("close")) for r in records]
            if all(c is None for c in closes):
                raise ValueError("get_stock_history returned records with no usable 'close' values.")

            # 2) Last traded price as of the last working day
            # The last record in the history is the latest trading day on file, so
            # ask for the LTP on exactly that date and report it as the LTP date.
            ltp_date = next((d for d in reversed(dates) if d), today)
            ltp_raw = await _call_tool(url, "get_ltp_by_date", {"symbol": symbol, "date": ltp_date})
            if ltp_raw.get("is_error"):
                raise RuntimeError(f"get_ltp_by_date failed: {ltp_raw.get('note') or ltp_raw.get('content')}")
            latest_price = _to_float(ltp_raw.get("content")) or technical.latest_value(closes)

            # 3) Moving averages, computed server-side (cross-check against our own SMA line)
            ma20_raw = await _call_tool(url, "moving_average", {"symbol": symbol, "days": 20})
            ma50_raw = await _call_tool(url, "moving_average", {"symbol": symbol, "days": 50})
            ma20_tool = None if ma20_raw.get("is_error") else _to_float(ma20_raw.get("content"))
            ma50_tool = None if ma50_raw.get("is_error") else _to_float(ma50_raw.get("content"))

            macd_line, macd_signal = technical.macd(closes)
            sma20_series = technical.sma(closes, 20)
            sma50_series = technical.sma(closes, 50)
            rsi_series = technical.rsi(closes, 14)
            trade_signal = technical.generate_trade_signal(
                closes, sma20_series, sma50_series, rsi_series, macd_line, macd_signal
            )

            try:
                ltp_age_days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(ltp_date[:10])).days
            except ValueError:
                ltp_age_days = None

            qty = int(stock.allocation // latest_price) if latest_price else None
            invested = round(qty * latest_price, 2) if qty else 0.0
            leftover = round(stock.allocation - invested, 2)

            entry.update(
                {
                    "dates": dates,
                    "close": closes,
                    "sma20": sma20_series,
                    "sma50": sma50_series,
                    "rsi": rsi_series,
                    "macd_line": macd_line,
                    "macd_signal": macd_signal,
                    "moving_average_20": ma20_tool,
                    "moving_average_50": ma50_tool,
                    "latest_price": latest_price,
                    "ltp_date": ltp_date,
                    "ltp_age_days": ltp_age_days,
                    "quantity": qty,
                    "invested": invested,
                    "leftover": leftover,
                    "trade_signal": trade_signal,
                }
            )
        except Exception as exc:  # noqa: BLE001 - keep going for the other symbols
            entry["error"] = str(exc)
        results.append(entry)

    return {"results": results}

import recommend  # next to `import technical`

class RecommendRequest(BaseModel):
    budget: float
    existing: list[dict[str, Any]] = []   # [{symbol, dates, close}, ...] from /api/analyze

@app.post("/api/recommend")
async def recommend_stocks(req: RecommendRequest):
    url = _server_url(ANALYSIS_SERVER)
    try:
        return await recommend.recommend(url, req.existing, req.budget, _fetch_history, _call_tool)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Recommendation failed: {exc}")





STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)

