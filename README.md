# NSE MCP Dashboard

A small local web app that talks to your two NSE MCP servers:

- **Bhavcopy (EOD)** — `https://mcp.nseindia.in/bhavcopy/cm/mcp`
- **CM Market Data** — `https://msc.nseindia.in/cmmkt/mcp`

You switch between them with the tabs at the top. The app doesn't assume
what tools each server exposes — on load it calls the server's `list_tools`
and builds the tool dropdown from whatever comes back, so it keeps working
even if the server's tool set changes.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Then open **http://127.0.0.1:8000** in your browser.

## Using it

**Bhavcopy / CM Market Data tabs** — explore raw MCP tools:
1. Pick a tab, pick a tool from the dropdown (description + argument names, if
   published, are filled in automatically).
2. Fill in argument values as JSON and click **Run**. List-of-records results
   render as a table; anything else renders as formatted JSON.

**Portfolio Analysis tab** — runs a fixed 3-step pipeline against the
Bhavcopy server's confirmed tools, per stock:

1. `get_stock_history` — daily OHLCV, chained across 3-month chunks to
   cover `ANALYSIS_HISTORY_MONTHS` (6 by default) → feeds the price chart
   and this app's own SMA20/SMA50/RSI(14)/MACD calculations (`technical.py`).
2. `get_ltp_by_date` — last traded price on the latest trading day in the
   history (the last working day on file), used as the price for the
   quantity/invested/leftover math. That date is shown as the **LTP date**
   in the summary table and on each card, with an "(Nd old)" flag if the
   data on file is more than 3 days behind today.
3. `moving_average` — the server's own 20-day and 50-day SMA, shown
   alongside this app's chart-computed SMA line as a cross-check.

To use it: enter your **total capital** and a handful of stocks, by company
name or symbol (see below), then click **Run analysis**. Capital splits
equally across them. Per stock
you get a price chart with SMA20/SMA50 overlays, an RSI(14) chart, the
LTP/MA20/MA50 figures, and the quantity/invested/leftover breakdown — plus
a summary table and totals across all stocks.

**Symbol lookup.** Each row calls the server's `search_symbols` tool as you
type (or on Enter) and resolves the text to an NSE symbol. One clear match
(or an exact symbol) is picked automatically; several matches show as chips
to choose from. The tool matches on **ticker text, not full company names**,
so "Reliance Industries" works (falls back to "Reliance" → `RELIANCE`) but
"Infosys" finds nothing because the ticker is `INFY`. In that case the row
tells you to try the ticker or a shorter keyword. Tickers with no trades in
the last 30 days (delisted/renamed) are hidden.

`ANALYSIS_HISTORY_MONTHS` near the top of `server.py` controls how much
history is fetched/charted; raise it for a longer-run SMA50/RSI view (each
extra 3 months costs one more `get_stock_history` call per stock).

The indicators (`technical.py`) are the standard SMA/EMA/RSI/MACD formulas
implemented from scratch with no extra dependency — good for a quick visual
read, not a substitute for a proper charting/trading platform.

### Trade setup: Entry / Stop loss / Target / No trade

Each stock also gets a rule-based setup from `generate_trade_signal()` in
`technical.py`. Four checks, one point each:

1. **Trend** — 20-day SMA above 50-day SMA
2. **Strength** — last close above 20-day SMA
3. **Momentum** — MACD line above its signal line
4. **RSI zone** — RSI(14) between 40 and 65

It returns **No trade** if the trend check fails, if RSI > 70 (overbought),
or if fewer than 3 of 4 checks pass. Otherwise it returns a **long** setup:

- **Entry** = last close
- **Stop loss** = the lower of the 10-day swing low and 2% below the 20-day
  SMA (falls back to 3% below entry if that isn't below entry)
- **Target** = entry + 2× the entry-to-stop risk (2:1 reward:risk)

Long setups only — there's no short logic. The thresholds are simple
defaults; tweak them in `technical.py`. This is a mechanical heuristic for
informational use, not investment advice, and it hasn't been backtested.

## Notes / things to check on your end

- I don't have live network access to your two MCP endpoints from here, so
  I couldn't confirm their exact tool names, required auth, or argument
  shapes. The app is built to auto-discover tools rather than hard-code
  them, so it should work as-is once you run it — but if either server
  needs an API key or auth header, add it to the `streamablehttp_client(...)`
  calls in `server.py` (it accepts a `headers=` argument).
- If a call errors out, the error message from the server is shown in the
  status line — that's usually the fastest way to see what argument names
  it actually wants.
- To add more NSE MCP servers later, just add an entry to the `SERVERS`
  dict in `server.py` and a matching tab in `static/index.html`.
