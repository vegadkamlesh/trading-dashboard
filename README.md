# Dhan Options Trading Dashboard

A fast, secure, full-stack web dashboard for options trading on Dhan (NIFTY & SENSEX),
built on the **DhanHQ v2 API**.

- **Frontend:** React + Vite + TypeScript + TailwindCSS (fast, tiny bundle)
- **Backend:** Python FastAPI (async) — holds all secrets, never exposed to browser
- **Data:** shared WebSocket feed with REST fallback, staggered option-chain cache,
  plus subscribed historical-data APIs when available.
- **Manual orders:** Dhan **Super Order**, LIMIT or MARKET entry with premium-based
  target and a mandatory stop leg. "No SL" means a **90%-away stop**, not no stop.
- **ORB algo:** regular BUY order, software **spot-based target only by default**,
  and a broker **premium SL 70% away** (algo; manual No-SL stays 90%). Software spot SL / break-even are OFF
  in target-only mode. These are separate
  execution paths; do not confuse their stop rules.

> ⚠️ **Read `SECURITY.md` before running.** This app can place real money orders.

---

## Seedhi Hinglish guide: live data, Data API subscription aur safe trading

### Master ka paid Data API: kya benefit aur app mein kya use hai?

Trading API free hai; paid Data API market/history access deta hai. Paid plan ko
unlimited requests, guaranteed fills, profitable signals ya automatically higher
rate limit **mat samjho**. Specific subscription entitlement Dhan profile/support
se confirm karo. Child ke liye alag Data subscription nahi chahiye.
1 Nov read-only Master profile check ne `dataPlan: Active` confirm kiya; isse
exact purchased tier ya every optional depth endpoint ki entitlement certify nahi hoti.

| Official capability | Benefit / hum kya kar sakte hain | App ka current use |
| --- | --- | --- |
| Live market WebSocket | Tick-by-tick LTP; ticker/quote/full modes; 5 connections/user, 5,000 instruments/connection; subscribe messages max 100 instruments | Ek shared Master ticker feed; index spot + subscribed option premium. Rich quote/full parser abhi implemented nahi |
| REST LTP / OHLC / full quote | Up to 1,000 instruments ek batch; full quote mein OHLC, volume, OI, bid/ask depth | LTP/OHLC + stale-feed fallback; full quote expansion future |
| Option chain + expiry list | All strikes ke LTP, OI, volume, IV, delta/gamma/theta/vega, bid/ask; contract/expiry selection | Live chain, ticket reference, signal inputs; same underlying/expiry 3.1s minimum request gap |
| Historical daily/intraday | Daily to inception; intraday last 5 years, 1/5/15/25/60-minute intervals, max 90 days/request | Opening range, charts, indicators, historical studies; yahi long-lived cache allowed |
| Expired rolling options | Last 5 years; OHLC/IV/OI/volume/spot/strike; max 30 days/request; ATM +/-10 near index expiry, +/-3 other contracts | Expiry study integration; broker DH-905 ho to unavailable/error, invented win-rate nahi |
| 20 / 200-level depth | NSE Equity/Derivatives liquidity analysis; 20-depth max 50 instruments/connection, 200-depth one instrument/connection | Future optional parser/UI; BSE/SENSEX depth supported **nahi** in these endpoints; exact account entitlement confirm karo |
| Instrument master | Correct security ID, exchange, expiry, strike, lot/tick sizes | Contract registry/selection; guessed symbol ya lot se order nahi |

### Trading/account APIs (paid-data benefit se alag)

- **Standard orders, Super Orders:** entry, modification/cancellation, fills; app
  manual Super Order aur ORB standard BUY + protective SL use karta hai.
- **Live order-update WebSocket:** website/app/API-origin orders sabke notifications.
  Master aur Child ki **alag trading-token connections**. Event fresh REST refresh
  trigger hai, holding flat hone ka proof nahi.
- **Portfolio/funds/margin:** real `netQty`, account P&L, holdings aur available funds.
  Funds/positions used; margin-calculator expansion possible.
- **Forever/GTT/OCO, Conditional Triggers:** future strategies possible. Conditional
  conditions currently equities/indices par; automatic integration enabled nahi.
- **Kill Switch, P&L-based Exit / Exit All:** broker risk controls available, lekin
  app ka DISARM broker kill switch nahi. In APIs ko silently activate nahi karte;
  P&L exit user ke target-only policy ko change karega, explicit design/approval chahiye.
- **Statements/ledger:** future historical fill/P&L reconciliation and reports.
  **eDIS:** delivery-equity selling approval, present F&O algo ke liye nahi.
- **Postback:** token-specific webhook alternative. External website exits cover
  karne ke liye app order-update socket + portfolio polling use karta hai.
- **Authentication/profile/static IP:** token expiry, access and order permission
  verify karte hain; IP verdict missing ho to LIVE entry fail safely.

Official references:
[API index/limits](https://dhanhq.co/docs/v2/),
[authentication](https://dhanhq.co/docs/v2/authentication/),
[market feed](https://dhanhq.co/docs/v2/live-market-feed/),
[quotes](https://dhanhq.co/docs/v2/market-quote/),
[option chain](https://dhanhq.co/docs/v2/option-chain/),
[historical](https://dhanhq.co/docs/v2/historical-data/),
[expired options](https://dhanhq.co/docs/v2/expired-options-data/),
[depth](https://dhanhq.co/docs/v2/full-market-depth/),
[order updates](https://dhanhq.co/docs/v2/order-update/),
[portfolio](https://dhanhq.co/docs/v2/portfolio/),
[risk controls](https://dhanhq.co/docs/v2/traders-control/),
[conditional triggers](https://dhanhq.co/docs/v2/conditional-trigger/),
[release notes](https://dhanhq.co/docs/v2/releases/).

### Website se exit kiya: ab dashboard kaise decide karega?

1. Dono configured accounts ka backend positions, standard orders, Super Orders aur
   funds approximately **har 1s** refresh karta hai, account selection/view se alag.
   Network latency/rate cooldown se delay badh sakta hai; fixed 1s guarantee nahi.
2. Dhan order notification aate hi snapshots invalidate hote hain aur fresh REST
   books request hote hain. Har account ka event identity verify hota hai.
3. **Holding = Dhan `netQty`**, saved BUY ya `TRADED` entry record nahi. Full external
   exit par flat; partial exit par broker ki remaining quantity adopt hoti hai.
4. Algo outstanding SL/exit ko verify/cancel karta hai. Unknown protection par
   tracking `attention` reh sakta hai, lekin UI **broker flat / tracking unresolved**
   alag batati hai. Unknown pending SELL ko ignore karke repeat SELL nahi bhejte.
5. Purana prior-day **INTRADAY unknown entry** tabhi retire hota hai jab twice-fresh
   positions flat aur current standard/Super books mein matching active/unknown
   orders/legs nahi. Historical fill/P&L **UNKNOWN** journal event hota hai; fake
   fill/trade/P&L nahi. Same-day/carry/uncertain holdings automatically erase nahi.

Live UI ko REST/socket ka latest in-memory snapshot chahiye; yeh historical cache
nahi, **timestamped short-lived broker snapshot** hai (max 1s reuse for GET readers).
Execution confirmations always fresh reads use karti hain. Header last response
age, resource errors aur order-stream/REST status dikhata hai. Positions >3s old
ya read error = STALE/UNVERIFIED. Purane success ko fresh data bolkar nahi dikhate.
Market feed ONLINE, socket connected aur algo ON **open trade ka proof nahi**.

### Kaunsi value broker ki, kaunsi hamari calculation?

- Broker: quantities, order status, confirmed fill/average price, available funds,
  position P&L, live LTP/spot, option-chain fields.
- Local/derived: OR breakout rules, spot target, lot/risk sizing, signal score,
  quote age, far protective premium trigger, paper trades and historical statistics.
  Dhan app ke custom ORB target/risk rules provide nahi karta.
- Algo card available broker `realizedProfit + unrealizedProfit` ko **Broker contract
  P&L** label se dikhati hai (contract/account total, individual algo fill attribution
  nahi). Broker field unavailable ho to quote-based estimate clearly labelled;
  broker-flat tracking par saved premium profit ko live profit nahi dikhate.
- **Manual:** same requested lots har checked account; Super entry + premium target
  + mandatory SL. No SL checkbox = confirmed/reference premium ke around **90% away**
  protective stop, zero stop nahi. LIMIT input auto-change nahi; MARKET estimate fill nahi.
- **ORB default:** confirmed BUY ke baad protection; software spot SL/break-even OFF,
  spot target first, broker premium SL ~70% below fill, manual EXIT, ORB 15:29 square-off.
  Far SL hit na hone ki guarantee nahi: premium near-zero ho sakta hai.
- Accepted/TRANSIT order != filled; rejection/timeout/partial fill account-wise.
  Same request UUID automatic repeat nahi; Child fail ho to Master rollback nahi.

### Available recent logs: findings aur limits of this audit

25, 27 aur 29 Oct 2026 ke available logs reviewed; missing daily files ke liye
"all seven days clean" claim nahi. 27 Oct Child unknown entry repeated 167 times,
29 Oct unknown entry-cancel warnings 228 times and rolling history DH-905 24 times.

- Order-detail singleton list was rejected by dict-only logic: normalize with exact
  order ID/correlation. Missing detail par fresh current order book mein exact
  identity verify hoti hai; both missing/ambiguous ho to UNKNOWN, never guessed filled.
- Historical pending entry blocked flat recovery: now safety-qualified read-only
  recovery, no duplicate exit and no invented historical P&L.
- Portfolio used to fail entirely on an unknown old manual-exit lookup: actual
  broker rows still display; unresolved exit identity remains retained/logged.
- Market socket immediate-close reconnect reset: now exponential backoff resets
  after actual parsed price data, not just handshake. HTTP401 is actionable and
  pauses credential retries 60s; one account failure does not mask the other.
- Dhan Master IP `Something went wrong`, expired/rejected credentials, insufficient
  funds and Market Closed need broker/user action; app must not bypass these.
- Rolling-history DH-905 remains broker-reproduced even with official example
  numeric security ID and WEEK/MONTH variants. Invalid/auth request now stops that
  study immediately with request context, instead of repeating every chunk.
  Root broker acceptance/entitlement still needs Dhan clarification; unavailable
  study is not a trading signal or fabricated success.

Verification (1 Nov 2026): **143 offline tests**, Python compilation and frontend
TypeScript/Vite production build passed. Mocked browser checks covered standard
book visibility, Master/Child view isolation, automatic external-flat removal,
stale/resource-error banners and broker-flat unresolved tracking without fake P&L.
Real read-only books showed both accounts flat; saved Child pending-entry recovery
passed **in memory** with zero invented trades. Live state/credentials were not
rewritten, no live engine started, and no order/IP/ARM/LIVE action was sent.
Actual exchange-hour fills and order-stream delivery remain deployment checks,
not guaranteed by offline tests. Restart normally to load the new code after
checking broker books and outstanding order permissions.

## Master Dhan + Child Dhan

### Dashboard force stop (Windows)

Double-click **Stop Dashboard.bat** to force-stop this project's backend/frontend
PID trees, including identifiable orphaned workers, with retries. It verifies
ports 8000/5173/5174 and reports failures explicitly; unrelated Vite/Node/Python
apps are not killed. For terminal/automation: `.\Stop-All.ps1 -NoPause`.
If Windows denies access, use **Run as administrator** and inspect the reported PID.
Stopping the app **does not close Dhan holdings or cancel broker orders**; software
target/exit management stops, while existing broker-side orders remain at Dhan.

One backend supports two distinct Dhan client IDs. **Master is the only market-data
source** (option chain, quotes, historical data and one shared WebSocket). Child
uses its own trading credentials for funds, holdings, orders, cancellation and IP
registration. No Child Data API subscription is used.

Configure these values in `backend/.env` (never commit or share the real values):

```dotenv
# Master market Data API
DHAN_CLIENT_ID=your-master-client-id
DHAN_ACCESS_TOKEN=your-master-data-token
# Optional independent Master Trading API token for the SAME client ID.
# Leave blank only if DHAN_ACCESS_TOKEN also has trading permission.
DHAN_MASTER_TRADING_TOKEN=

# Independent Child Trading API (both required)
DHAN_CHILD_CLIENT_ID=your-child-client-id
DHAN_CHILD_ACCESS_TOKEN=your-child-trading-token
```

Partial Child credentials or the same client ID in both slots are rejected at
startup. Restart the backend after changing tokens. Do not replace client IDs
while positions, pending orders or saved account state still belong to the old
account. Newly saved algo state records its client ID and refuses a mismatched
restore; legacy Master state stays in its original location.

For **one Master token for trading + data**, leave `DHAN_MASTER_TRADING_TOKEN`
empty. Then Master trading and all data requests use `DHAN_CLIENT_ID` +
`DHAN_ACCESS_TOKEN`. Child orders/account APIs use only `DHAN_CHILD_CLIENT_ID` +
`DHAN_CHILD_ACCESS_TOKEN`; viewing Child does not switch the market-data identity.
Both accounts use the same Dhan API endpoint URLs, with different authentication
headers and order client IDs. No separate Child data client is created.

### Same static IP on both accounts

Dhan's official FAQ says **eligible family member accounts can share one static
IP for API trading, subject to verification and approval**. Do not assume that
adding the IP independently to both accounts is sufficient.

- [Official eligibility FAQ](https://dhan.co/support/platforms/dhanhq-api/can-multiple-family-member-accounts-use-the-same-static-ip-for-api-trading/)
- [Official mapping request instructions](https://dhan.co/support/platforms/dhanhq-api/how-can-i-request-static-ip-mapping-for-family-member-accounts/)

Send the mapping request from your registered email to **apihelp@dhan.co**, keeping
the respective family accounts' registered emails in CC, and follow Dhan's Family
Static IP Mapping guide. Obtain approval before live shared-IP trading. These
FAQs do not establish eligibility for arbitrary/unrelated accounts; ask Dhan to
confirm your specific pair. Local IP preflight checks do not certify family
mapping approval. This app does not submit the approval request automatically.

### Controls and independent outcomes

- **Trading checkboxes:** select NEW manual/algo entry recipients. Master only,
  Child only, both, or neither. Selection resets to **Master only on restart**.
  Disarm and finish tracked algo positions before changing it.
- **Viewing selector:** selects whose funds, positions, Super Orders, journal,
  history, IP settings and account-wise EXIT are displayed. It does **not**
  change entry recipients. Switching views clears the old account panels/ticket.
- **Manual ticket:** previews each selected account's funds; sends the same
  contract, direction and requested **lots to each account**. One account lacking
  funds does not prevent the other account from submitting.
- **Algo:** edit shared settings from Master view. Shared ARM preflights each
  recipient; a failed account is explicitly reported and not armed, while healthy
  recipients continue. Master coordinates one OR/breakout signal even when its
  trading checkbox is OFF. The contract, direction and spot-target anchor are
  shared; each account sizes from **its own balance/risk/cash budget**, and manages
  its own fills, premium protection and exit. Fill prices/quantities can differ.
  The ON/OFF toggle displays the **shared run**, not the viewed account: ON arms
  checked recipients from either view; OFF disarms all. "Algo ready" lists the
  recipients actually armed, including partial preflight outcomes. Account panels
  and individual position EXIT remain view-specific.
  A list/message-only or malformed Dhan IP response is treated as **unverified**,
  never as permission to trade. Its broker reason is shown for that account;
  healthy recipients can still arm. Unexpected API failures include a traceback
  in the access/general error logs instead of disappearing from request logging.
- One shared signal attempt per day; its day is persisted **before** submissions.
  A rejected, timed-out or partly successful batch is not blindly replayed.
  Fixing a failed account does not automatically resubmit that missed signal.
- **EXIT:** only the viewed account. **EXIT BOTH:** explicitly exits that exact
  contract in both configured accounts, including accounts disabled for new
  entries. **ALGO EXIT BOTH:** requests exit of each account's own tracked algo
  position. Pending/error results remain visible; an accepted exit is not proof
  that the holding is closed.
- Both trades are **not atomic**. A rejection/timeout in one does not auto-close
  the successful account. Check each result and Dhan's holdings before acting.
  A network timeout may mean an order was accepted: never blindly repeat it.
- DRY-RUN/LIVE is global. Mode/config/selection changes coordinate both engines;
  a tracked position in either blocks unsafe changes. Backend startup always
  **disarms new algo entries**, but restores management of existing positions.
  Existing target-only/far-SL and ORB-only 15:29 rules below are unchanged.

### Isolation, API budgets and debugging

Trading clients, read caches, execution locks, request UUID receipts and pending
exit identities are account-scoped, even if both brokers return identical order
IDs. Manual batches persist their recipient snapshot and mode; repeating the same
request ID cannot silently add recipients or turn an old dry-run into a live order.

Order/read budgets are keyed by client ID. Both accounts share Master data,
quote and option-chain gates (limits table below). This prevents in-process
collisions, **not** external broker failures or quota usage by other apps. Run
only one backend process; counters/locks are not shared across multiple workers.

| Purpose | Master | Child |
| --- | --- | --- |
| Manual audit/UUIDs/pending exits | `backend/audit.sqlite3` | `backend/audit-child.sqlite3` |
| Algo state/journal/trades | `backend/data/algo/` | `backend/data/algo/child/` |
| Daily account log | `backend/logs/account-master-YYYY-MM-DD.log` | `backend/logs/account-child-YYYY-MM-DD.log` |

Log directory/retention can be configured. General errors remain in
`errors-YYYY-MM-DD.log`; order attempts also appear in `orders-YYYY-MM-DD.log`.
Search an account log for `batch=`, `correlation=`, `http=`, `code=` or `reason=`.
The UI polls recipient status every 3s; broker account pages poll every 1s and
both configured accounts refresh in the backend even off-screen. No token is returned to the browser.

Offline integration tests exercise real ASGI routes, middleware, persistence,
execution code and authenticated Dhan clients against fake broker transports:

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

These tests do **not** certify real account credentials, permissions, static IP,
broker uptime or simultaneous/identical fills. Start with DRY-RUN, check funds and
IP for **each** account, then explicitly ARM the intended recipients.

## Order execution and stop-loss rules (current implementation)

### ORB automatic BUY

1. Arm the algo from its tab. LIVE arming checks Dhan's registered-IP verdict.
   DRY-RUN simulates orders; it does not submit them.
2. At/after **09:25 IST**, build today's opening range from the first 10
   one-minute candles, **09:15 through 09:24 inclusive** (09:15:00-09:24:59).
   It locks only when both the 09:15 and 09:24 candles are present.
3. The first watched index with spot **strictly above OR high buys a CALL**;
   spot **strictly below OR low buys a PUT**. Equal to a boundary is not a break.
   Max **2 trades per day** (first index to break out wins, the other is locked). Trade 2 arms only after ALL accounts are flat and needs a FRESH breakout (spot must come back inside the OR first); no new entries at/after **15:00**. Product = MARGIN (no broker 3:18/3:19 auto square-off; app exits at 15:29).
   The signal is level-based: starting late can enter an already-broken range.
   Index feed values are rounded to 2 decimals before comparison so float32
   noise (22,752.300781 vs 22,752.30) is not treated as a breakout.
4. ATM is rounded to the strike step. Offset 0 = ATM, +1 = one strike OTM:
   CALL strike = ATM + offset x step; PUT strike = ATM - offset x step.
   Existing saved settings can override defaults (for example, offset +1).
5. Spot and option premium must each have a fresh feed sample (maximum **5s**
   old). Recheck the breakout before submission. A stale chain snapshot must
   not replace live spot. Do not mix an existing broker position in that contract.
6. Persist the order's correlation ID **before** submitting. `TRANSIT`/`PENDING`
   is **not a fill**. Only terminal broker status with positive filled quantity
   and average price opens a managed position. Partial entries cancel their
   remaining BUY quantity first and wait for broker confirmation.
7. Network/5xx uncertainty is retained and resolved by order/correlation ID;
   never blindly resend a possibly accepted BUY or SELL.

### ORB stops, target and exit

**Current default: `target_only = true`** (`ALGO_TARGET_ONLY=true` for fresh
config). Software spot SL and break-even exits are disabled even if old SL
multipliers/break-even settings are present. CALL target = `S + target_mult x R`;
PUT target = `S - target_mult x R`. Manual EXIT and **15:29 ORB time exit remain**.
The broker protective SELL SL is **70% below the confirmed entry premium**:
`max(0.05, tick_round(fill premium x 0.30))`. Rs100 fill -> Rs30 trigger.
This is deliberately far away, **not a guarantee it cannot hit**. You can lose
nearly the entire premium (or worse execution across gaps).

Old saved configs without `target_only` migrate to this default. A tracked old
protective stop is verified/cancelled and fresh holdings checked before a new
far stop is parked; until cancellation is confirmed, **the old stop may still
execute**. Already submitted exits are not undone. No broker changes are made
merely by editing source; restart/reconcile is required.

The formulas below describe **classic mode only** (`target_only = false`),
retained for explicit opt-in and comparison with the existing historical study.
Target-only changes the strategy: the old win-rate/expectancy are not validation.

Let `R = OR high - OR low`, and `S = spot at the shared breakout dispatch`.
Levels stay anchored to that reference, not to a delayed option fill:

| Direction | Software spot stop | Software spot target |
| --- | --- | --- |
| CALL | `S - sl_mult x R` | `S + target_mult x R` |
| PUT | `S + sl_mult x R` | `S - target_mult x R` |

Classic settings: **sl_mult = 0.20**, **target_mult = 2.0**, **breakeven_at = 0 (off)**.
Example: CALL, S=22,750, R=100 -> stop 22,730; target 22,950.
Stop/target use inclusive comparisons on fresh spot samples. If break-even is
enabled, favorable movement of `breakeven_at x R` moves the stop to entry.
No artificial minimum holding time is imposed: a genuine stop hit can exit quickly.

- After confirmed entry, protective SL (default ON) submits a regular
  **STOP_LOSS_MARKET SELL**, not a filled exit. Premium trigger is
  10% of fill premium in target-only mode. **Classic mode only** uses
  `max(0.05, entry premium - abs(S - initial spot stop) x protective_sl_mult x paper delta)`,
  rounded to a 0.05 tick; classic multiplier 2. This delta approximation is
  **not** the same thing as a spot stop. It may hit before the software stop.
- Before parking it, require a fresh option price. If its trigger is already
  crossed, request an exit instead of submitting an immediately-triggerable stop.
  A failed/unknown protective order is shown as a warning; protection is not guaranteed.
- Software stop, target, **15:29 forced exit**, PANIC, and Positions EXIT share
  the algo execution lock. Verify/cancel the protective stop, confirm terminal
  cancellation/fill, then re-read actual holdings before any market SELL.
  If the stop already flattened the position, do **not** sell again.
- Keep `exit_pending` until the broker confirms zero net quantity and no
  outstanding algo SELL can still execute. Rejection/error does not mark a
  holding closed. Partial remaining holdings are rechecked before retrying.
- **Disarm stops new entries, not management of an existing position.**
  DRY-RUN/LIVE and config changes are blocked while a tracked position exists.
  Each position keeps its original execution mode even across restart.
- On restart, resubscribe tracked instruments and reconcile pending orders and
  positions. A new day never silently discards a surviving live position.
  After downtime, level-based signals can still trigger; no historical entry
  price is guaranteed.

**Sizing:** target-only risk/lot estimates **70% of premium x lot size**
(100% if protective SL disabled), not the old tight spot stop. Classic mode
uses spot stop distance x abs(delta) x lot size. Risk lots =
floor(balance x risk% / risk per lot); cash lots = floor(capital budget / premium per lot); capped by
max lots. The existing minimum-lot policy can override risk/capital percentages
when affordable, but **never actual available cash**. If fewer than min lots
are affordable, skip. Defaults: min 1, max 10, 60% capital, effective risk 0.5%.
NIFTY uses the configured registry lot size 65, SENSEX 20; verify current
contract sizes with the exchange/Dhan before LIVE, or set ALGO_LOT_SIZES.
These are risk estimates, not maximum-loss guarantees; gaps/slippage can exceed them.

### Algo 2 — options-flow engine (separate tab, separate switches)

The old ORB algo is now labelled **Algo 1** (unchanged). **Algo 2** lives in
`backend/app/algo2/` and the `🤖 Algo 2` tab. API: `/api/algo2/*`.

- **Own ON/OFF, own PAPER/LIVE, own account ticks** (Master / Child), saved in
  `data/algo2/group.json`. Defaults: OFF + PAPER. Paper/Live change needs OFF + flat.
  Telegram and logs are prefixed `Algo 2 |`; order correlation IDs use `AL2-`.
- **Product INTRADAY only.** Algo 1 must stay on MARGIN (default) — Algo 2 refuses to
  arm otherwise, so broker positions of the two algos never mix. Exit time 15:15
  (before Dhan's ~15:18 auto square-off).
- **Entry:** spot breaks the last closed 5-min candle high + buffer (NIFTY 0.5 / SENSEX
  1.5 pts) for CALL, low − buffer for PUT, and **every enabled condition passes**.
  Strike = ITM with |delta| closest to 0.70. Each condition has its own switch:
  news freeze (±15 min), trade windows 09:45–11:15 & 13:30–15:00, PCR chop band
  0.85–1.10, net delta rising, net GEX < 0, opposite-side OI velocity > +30% (5 min),
  same-side OI unwinding, HSMI sign, RELIANCE+HDFCBANK+ICICIBANK sync, heavyweight
  divergence, futures beyond VWAP ±1.5σ band, futures beyond VAH/VAL, basis, VIX.
  PUT rules mirror CALL rules.
- **Exits:** stop = breakout candle low/high, capped at 0.75% of balance
  (÷ delta × qty). T1 = highest-OI wall if ≥ 2R away, else 2R: book 70% (1 lot =
  full exit), stop → cost, remainder trails VWAP / T2 = max(2R, T1 + R). Data SL:
  same-side OI velocity leads the opposite side by ≥ 10 pp. 20-minute rule: if T1 is
  not reached in 20 min, exit at market. Sizing: max affordable (as Algo 1).
- **Data:** Master Data API only. Option chain is reused from the shared chain engine
  (no extra chain calls); `/marketfeed/ohlc` every 5 s (VIX, index, 6 heavyweights,
  nearest futures); 5-min candles once per closed bar. VWAP / market profile use
  futures candles (index has no volume). HSMI/trio/velocity need ~5 min warm-up after ON.
- **Events:** `data/algo2/events_calendar.json` (seeded with RBI policy / Budget dates;
  add/remove in the tab). NSE corporate events for heavyweights are fetched once a
  day; they are date-only and shown but do not block unless timed.
- **Limits:** no historical backtest is possible (Dhan has no historical chain
  greeks/OI). With all 14 conditions enabled, signals will be rare — run PAPER first.
- Tests: `python -m unittest tests.test_algo2` (from `backend`).
### Manual order path

Manual Buy/Sell uses Super Orders, not the ORB formula. Target is entry reference
premium +/- target%; "No SL" puts the mandatory premium stop 90% away. Without
that option, a supplied stop % is used (bounded 1-95%), otherwise 3%.
A contract currently tracked by the algo cannot receive another manual entry.
A manual SELL opens a short; use **Positions EXIT** to close a holding.
The open manual ticket refreshes the exact contract reference from the shared
chain cache every 3s; the entered LIMIT price stays unchanged unless you press LTP.
Stale/missing quotes disable confirmation. Input changes invalidate the old
preview immediately, and preview failures are shown instead of silently hidden.
For MARKET orders, the server recalculates target/SL from that contract's fresh
cached premium (maximum age 12s) at preview and submission; this is **not the actual
fill price**, so slippage can still cause a rapid target/stop execution.
All orders must have whole exchange lots, tick-aligned positive prices, and
strictly separate entry/target/SL prices after rounding. Manual lots honour the
same `ALGO_LOT_SIZES` override as the algo.
Missing/invalid/current funds errors block submission; start-of-day balance is
not substituted for available funds. Short-option cash is only a lower-bound
estimate, **not** a SPAN/exposure margin approval.
Each ticket keeps a UUID request ID; its intent/receipt is stored in SQLite before
the broker write. Retrying the **same ID** returns its receipt or blocks an
unknown outcome, including after restart. API clients must reuse `requestId`
when retrying; opening a new ticket creates a new intent, so **check Dhan first**
after a timeout. A REJECTED/submitted order is not reported as a confirmed fill.
Exit checks actual side/quantity and cancels confirmed Super Order target/SL
legs before sending a plain MARKET exit. Pending entry/exit or unconfirmed
cancellation blocks another SELL; verify Dhan if the app asks for attention.
Manual exit correlations also persist in SQLite across restarts. An algo entry
checks both holdings and pending standard/Super orders to avoid mixing with a
pending manual entry. Manual cancellation and exits share the execution lock.
**15:29 automatic square-off applies to the ORB position only**; manual Super
Orders retain their broker target/SL and require manual EXIT or broker square-off
rules. Do not assume the app closes every manual position at 15:29.

### Sync, health and verified Dhan limits

- Algo status: frontend **1s** against in-memory backend state; no broker call
  per browser poll. Positions / standard orders / Super Orders / Funds UI: **1s**.
  Both configured accounts independently poll books/funds at ~1s even off-screen;
  account trading-token order streams invalidate snapshots and wake polling.
- Positions/Super Orders LTP is enriched with segment-aware batched quotes
  (maximum 1,000 unique instruments per request), through the shared quote gate.
  Quote errors leave broker holdings/EXIT available and explicitly label prices
  STALE. Holdings, order status, P&L and quotes are **separate broker
  snapshots**, not an atomic tick-for-tick view; broker P&L is not replaced by
  a locally estimated figure. Poll cadence includes request/throttle latency.
- Expiry lists refresh each IST day. Changing expiry invalidates the old chain;
  an older in-flight response cannot overwrite the new selection.
- While an algo position is tracked (even disarmed/pending), reconcile with
  Dhan every **1s** plus request latency. Read errors preserve state and show
  a sync warning. Broker holdings can lag a fill, so fresh fills get a short
  propagation window.
- Spot updates on index feed samples, premium on option samples. WebSocket
  silence >20s starts batched REST fallback, paced at least **1.05s** per quote
  request. A missing/stale (>5s) individual subscription also gets REST recovery
  even if other instruments keep the socket healthy. Cache keys and tick
  consumers include the exchange segment, so an NSE/BSE security-ID collision
  cannot overwrite another contract's price. REST samples do not pretend the
  WebSocket is healthy.
- Feed connected / ALGO ON means service health/arming, **not an open position**.
  Position execution state, quote age and last broker-sync time are separate.
  Failed UI polling explicitly marks the last snapshot stale; focus resumes polling.
- Exit prices from broker fill/position data are preferred. If unavailable, the
  journal marks `exitPriceEstimated`; P&L is gross, before brokerage/slippage.

Official limits rechecked **1 Nov 2026**:
[Dhan v2 limits](https://dhanhq.co/docs/v2/) and
[option-chain-specific limit](https://dhanhq.co/docs/v2/option-chain/).

| API category | Dhan published limit | This process |
| --- | --- | --- |
| Order writes | 10/s, 250/min, 1,000/hour, 7,000/day | shared 0.12s gap + all rolling quotas |
| Chart/data | 5/s, 100,000/day | shared 0.25s gap + daily quota; normal charts also keep existing 1.1s gap |
| Quote REST | 1/s | shared 1.05s gap; batched instruments |
| Non-trading reads | 20/s | per-client-ID 0.06s gap; four account resource polls ~4 reads/s/account plus safety reads; 1s latest-snapshot reuse + single-flight |
| Option chain | one unique request / 3s | per client/underlying/expiry 3.1s gate, plus shared data/daily budget; different chains no longer forced through one 3.1s queue |

429/DH-904 defers that category by Retry-After (minimum 3s). Order writes are
**not automatically replayed**. Bulk history `throttle=False` cannot bypass
the shared data gate. Budgets/cache are **per backend process**, reset on restart,
and do not account for another script/app using the same account. Run one
backend only, leave headroom, and monitor 429s; zero rate-limit errors cannot
be guaranteed. Per-order modification cap is 25; this app does not ratchet
broker stops with modifications.

### Offline regression checks

Startup recovery correction (19 Oct 2026): a restored **prior-day INTRADAY**
position can be retired without looking up an unavailable historical DAY order
only when two fresh position snapshots confirm the contract is flat and fresh
standard/Super Order books contain no active or unknown matching orders/legs.
Same-day, carry or uncertain broker state is not discarded. Since 1 Nov, an old
pending-entry tracker can also recover under these checks; its historical fill/P&L
remains UNKNOWN and is not booked as a completed trade.
Recovered journal exits are explicitly estimated, not claimed historical fills.
The DRY-RUN/LIVE switch attempts this read-only historical recovery before its
open-position guard; a successful mode change disarms the algo, so explicitly
ARM again after reviewing settings. Actual broker/sync errors are shown in Settings.
Startup still uses the configured `.env` defaults; UI mode changes are not
persisted. DRY-RUN on restart is intentional, not an order-execution failure.
The 19 Oct correction passed **84 offline tests** and the frontend production build.

```powershell
Set-Location backend
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Set-Location ..\frontend
npm run build
```

Tests use fake broker responses, temporary/in-memory state and simulated clocks.
They do not place real orders or rewrite the saved trading session.
Verification on 18 Oct 2026: **75 offline regression tests passed**, Python
compilation and the TypeScript/Vite production build passed. Browser-only
fixtures verified reference-price refresh without changing LIMIT input,
preview invalidation/errors, stale quote blocking, request IDs, closed-row EXIT
disablement, target-only display, pending exit protection, changing Spot Now and
position removal after flat sync. These are execution/sync checks, **not live
broker-fill certification or strategy profitability tests**. No live backend was
started for validation; refresh credentials and inspect Dhan holdings/orders
before restarting a previously armed live session.
See [ALGO_STRATEGY.md](./ALGO_STRATEGY.md) for strategy background; study figures
are historical claims, not a fresh profitability validation or a promise.

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

### Telegram updates (optional, private, outbound-only)

1. Telegram mein verified **@BotFather** ko `/newbot` bhejo. Token sirf local
   `backend/.env` mein rakho; chat, screenshots, git, shell command arguments ya
   browser URL mein mat paste karo.
2. Apna **private channel** banao, bot ko admin banao with **Post Messages** only.
   Private channel ka numeric chat ID (usually `-100...`) config mein dalo.
   Personal bot chat bhi works after you send `/start`; use its numeric chat ID.
   Kisi third-party "ID finder" bot ko private channel/token access mat do.
3. `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_ENABLED=true` set karke
   backend restart karo. Missing/invalid enabled configuration startup reject hoti hai.
4. **Settings -> Telegram updates -> Send test**. "Queued" delivery ka proof nahi:
   channel mein message aur **Sent / Last API-confirmed delivery** verify karo.
   Telegram HTTP 400/401/403 par local bot token, chat ID/admin rights check karo.
   UI/token API kabhi bot token ya destination ID return nahi karti.

Telegram receives **fixed safe labels**, UTC timestamps, account aliases and safety
mode: startup/shutdown, algo updates/arm/disarm/confirmed entry/recorded exit,
manual order actions, broker order notifications, feed connection, safety toggles,
broker login failures, server errors and application warnings/errors. These are
not complete fill reports: an order action/update is not proof of a fill or flat
holding; recorded exits may contain estimates in the local journal.
Credentials, raw log/exception messages, account IDs, order IDs, quantities,
prices, balances and P&L are **not sent**. Keep detailed logs/journals locally or
in access-controlled AWS storage. Telegram must not be the trading audit database.

The worker does not block trading on network I/O. Delivery is best-effort: bounded
100-event in-memory queue, 10-minute event expiry, at most three attempts, 429
backoff and minimum 3.1 seconds between sends (including group pacing).
Generic warning/error categories are grouped for 60 seconds, not sent for every
tick/API poll. Queue drops, failures and grouping counters are visible in Settings.
Network retries may duplicate a notification; crashes can lose queued events.
Shutdown drains only a bounded portion of the backlog and logs discarded events.
Default heartbeat is 15 minutes (`TELEGRAM_HEARTBEAT_SECONDS`, minimum 60).
It reports notification-worker activity/counters, **not** broker readiness.
Disabled by default; normal single-user Telegram Bot API messaging is free within
[Telegram limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).
No paid broadcasts or inbound Telegram trading commands are enabled.

### AWS deployment (single private user, Mumbai)

The [AWS deployment guide](deployment/aws/README.md) includes infrastructure
and operator procedures for a private, initially mock-only stack. Files alone
do not prove an AWS stack is provisioned or production-ready. Account creation,
payment verification and a paid-resource estimate/approval are prerequisites.
Use Mumbai (`ap-south-1`), root MFA and a separate temporary deployment role.
Use a separate MFA-protected operator identity with temporary AWS credentials;
never share root credentials or long-lived access keys in chat.
Payment details/OTP remain with the account owner in
[AWS Billing](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/edit-aispl-payment-method.html).
Budgets/alerts are **not a hard spending cap**.
The optional 15:55 IST Telegram AWS usage report includes delayed, estimated
account-wide USD costs and approximate instance activity, not a real-time final
bill. Each Cost Explorer API request costs about USD 0.01; see the AWS guide.

Initial budget-friendly deployment should use one active backend process on an
appropriately sized EC2 instance, stable Elastic IP for Dhan, encrypted persistent
storage, private dashboard access (for example SSM port forwarding), and a built
frontend rather than the Vite development server. Do not expose the existing login
endpoint publicly: it validates the server's Dhan token, not the visitor's identity.
No domain is necessary for private forwarded access.

Before production approval, verify the deployed infrastructure and rollback,
restricted IAM/security groups, secret handling, CloudWatch log
forwarding/alarms, consistent SQLite/state backups and a tested restore.
The external watchdog checks scheduled host and local service health, not
broker-feed freshness or order safety; additional live checks remain necessary.
The app cannot send an outage alert after its own server dies.
Trading audit/journal retention must be decided separately from the current
7-day debug-log retention. Do not restore old trading state blindly: reconcile
broker positions/orders before resuming; do not run two active engines.

Adding 2-4 strategies requires account-level order/rate/capital/risk coordination,
strategy-scoped state and attribution, and restart/reconciliation tests. Existing
in-memory locks do **not** support active-active replicas or independent competing
workers. A single-server deployment is not multi-AZ HA and has downtime risk:
software-managed exits require a running, connected engine.
Cloud readiness, paper/dry-run checks, broker static-IP approval and explicit
operator LIVE/ARM approval are separate gates; deployment never implies consent
to real orders or guarantees trading profitability.

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
| **Option chain** (spot, LTP, IV, OI) | UI **every 5s** | backend configured same-underlying interval, floor 3.5s; unique underlying/expiry gate 3.1s; network/expiry-list latency applies |
| **Trade Signals** (per-strike prediction) | **every 20s** | re-runs the full engine (see below); also on tab focus |
| **Open Positions** (broker P&L) | **every 1s** | broker netQty; timestamps/errors distinguish stale; EXIT only for nonzero qty |
| **Standard / Super Orders** | **every 1s** | separate broker books; filled entry is not current holding |
| **Funds** | **every 1s** | respective trading account; no local balance estimate for live trading |
| **Market News** | **every 5 min** | + on tab focus; manual ⟳ too |
| **Daily OHLC (5-yr history)** | **once per session** | loaded at **login** into a 6h cache |
| **Expired-option history study** | **cached 12h** | warmed at login |
| **Seasonality** | **cached 6h** | uses the same warm history cache |
| **Opening Range (9:15–9:25)** | **cached 6h** | background job (1Y/3Y/5Y); poll progress |

**Prediction inputs (all live):** market trend / RSI / MACD, **live strike price
(LTP)**, IV, delta & theta, strike-vs-spot (moneyness), **news sentiment**, and the
5-yr history win-rate. Every prediction is explainable — click the **ⓘ** on any
strike or signal to see exactly which inputs drove it.

New backend endpoints: `GET /api/intel/guidance`, `/api/intel/history`,

`/api/intel/news`, `/api/intel/recommend`, `/api/intel/seasonality`,
`GET /api/intel/opening-range` (first-10-min high/low study, 1Y/3Y/5Y),
`GET /api/account/cache-state`, `POST /api/account/warm-cache`,
`POST /api/account/squareoff` (all require a session).

### Opening Range study (9:15–9:25)

Pichle **1/3/5 saal** me har din ke **pehle 10 minute (09:15–09:25)** ka high/low:

- **Avg / median / p90 range** (points + %) — aapka stop isse tight nahi hona chahiye
  (opening range ke andar ka stop noise me nikal jaata hai).
- **Opening range ka share of the day's range** — din ki volatility ka early read.
- **Breakout odds** — range ke upar/below break kitni baar hua, aur close band ke
  andar hi raha (fakeout) kitni baar.
- **Distribution** + **recent 20 sessions** table.

5-yr study ~21 chunked Dhan calls hai (90-day/call limit), isliye backend ise
**background job** ki tarah chalata hai aur UI progress dikhata hai — ek baar
compute hone ke baad 6h tak cached (instant).

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
