# Dhan Options Trading Dashboard

A fast, secure, full-stack web dashboard for options trading on Dhan (NIFTY & SENSEX),
built on the **DhanHQ v2 API**.

- **Frontend:** React + Vite + TypeScript + TailwindCSS (fast, tiny bundle)
- **Backend:** Python FastAPI (async) — holds all secrets, never exposed to browser
- **Data:** Free tier only → Option Chain polled every ~3s (rotating across indices)
- **Orders:** Dhan **Super Order** (Entry LIMIT buy + Target %). Stop-loss is auto-set
  to the exchange minimum distance to satisfy Dhan's mandatory SL rule.

> ⚠️ **Read `SECURITY.md` before running.** This app can place real money orders.

---

## Trading Intelligence (data-API powered)

The dashboard uses Dhan's **Data APIs** (5-yr historical + expired options) to
give an explainable, second-opinion **Trade Advisor** right inside the order
popup — so you never trade blind:

- **Advisor verdict** — `BUY / SELL / WAIT` with a **confidence %** and a list of
  plain-English reasons (trend EMA9/21, SMA20/50, RSI, MACD, IV).
- **5-year expired-options study** — empirical stats for the ATM/ATM±N strike from
  past expiry sessions: **win-rate**, **avg best-case swing**, **avg drawdown**.
- **Projected P/L scenarios** — how the option is estimated to move for
  `-2% … +2%` spot moves (uses greeks/delta when available).
- **Risk/Reward slider** — set your profit target and max-loss, see ₹ P/L and the
  risk:reward ratio update live.
- **Market Pulse** — free market headlines (RSS, server-cached). Refreshes only
  **on demand** (button) or per your chosen interval — no constant polling.

### Safety switches (true / false) — flippable live from the Settings tab

| Switch | `true` | `false` |
| ------ | ------ | ------- |
| **`APP_DRY_RUN`** | Orders are validated & logged only — **no real trade** | Orders are **sent to Dhan for real** |
| **`APP_FAST_ORDERS`** | **PIN skipped** — 1 click places the order (scalping) | Every order **requires the PIN** (safe) |

Both can be toggled from the **Settings** tab without editing `.env` or restarting
(a handy **FAST ON/OFF** + **DRY-RUN/LIVE** badge is always visible in the top bar).
Toggles live in memory and reset to the `.env` defaults on restart — so a restart
always returns to the safe configured state.

### Logging (everything saved, auto-trimmed to 7 days)

All activity is written to daily log files under **`backend/logs/`**:

| File | Contents |
| ---- | -------- |
| `app-YYYY-MM-DD.log` | Everything (startup, modes, events) |
| `orders-YYYY-MM-DD.log` | Every order attempt with price/qty/result |
| `errors-YYYY-MM-DD.log` | Only warnings/errors — fast scanning |
| `access-YYYY-MM-DD.log` | Every API call (method, path, status, time) |

Files older than **`LOG_RETENTION_DAYS` (default 7)** are deleted automatically at
startup and once a day, so disk usage stays bounded. Order history also lives in
`backend/audit.sqlite3` (powers the EOD Summary).

### In-app guide

A **Readme** tab inside the dashboard documents everything in **Hinglish** — how to
run the app, where the token goes, what true/false does, where logs are, and how to
place an order.


### Refresh model (everything live, low load by design)







Everything that shows a live number **keeps refreshing on its own** — you never
have to click. The cadences are tuned to stay well under Dhan's rate limits (the
backend serves a warm cache, the frontend polls gently and pauses when the tab is
hidden).

| Data | Auto-refresh | Notes |
| ---- | ------------ | ----- |
| **Option chain** (spot, LTP, IV, OI) | **every 5s** | backend polls Dhan ~3.5s/index, serves warm cache |
| **Trade Signals** (per-strike prediction) | **every 20s** | re-runs the full engine (see below); also on tab focus |
| **Open Positions** (live P&L) | **every 7s** | + one-click EXIT |
| **Super Orders** | **every 5s** | |
| **Market News** | **every 5 min** | + on tab focus; manual ⟳ too |
| **Daily OHLC (5-yr history)** | **once per session** | loaded at **login** into a 6h cache |
| **Expired-option history study** | **cached 12h** | warmed at login |
| **Seasonality** | **cached 6h** | uses the same warm history cache |

**Prediction inputs (all live):** market trend / RSI / MACD, **live strike price
(LTP)**, IV, delta & theta, strike-vs-spot (moneyness), **news sentiment**, and the
5-yr history win-rate. Every prediction is explainable — click the **ⓘ** on any
strike or signal to see exactly which inputs drove it.

New backend endpoints: `GET /api/intel/guidance`, `/api/intel/history`,

`/api/intel/news`, `/api/intel/recommend`, `/api/intel/seasonality`,
`GET /api/account/cache-state`, `POST /api/account/warm-cache`,
`POST /api/account/squareoff` (all require a session).

> The advisor is **rule-based and educational**, not financial advice. It combines
> live indicators with what history actually did — it cannot predict the future
> or protect against gaps.

---

## Quick start (one click)

Double-click a single file — it builds everything, starts the servers, and opens
your browser automatically.

| File | What it does |
| ---- | ------------ |
| **`Dhan Dashboard.bat`** | **All-in-one.** Sets up + starts backend **and** frontend, then opens the browser. Use this normally. |
| **`Dhan Dashboard.bat`** | **All-in-one.** Sets up + starts backend **and** frontend, then opens the browser. |
| `Run Backend.bat`  | Starts **only** the backend (API on :8000). |
| `Run Frontend.bat` | Starts **only** the frontend (UI on :5173) and opens the browser. |

**First run:** if `backend\.env` is missing, the launcher opens the example file
for you — save it as `backend\.env`. For an instant offline demo, set
`DHAN_ACCESS_TOKEN=mock-token` (no real credentials needed).

> The one-click launcher also installs Python deps and `npm install` for you the
> first time, so you don't need to run any commands.

---

## Project layout

Copy-Item backend\env.example.txt backend\.env

