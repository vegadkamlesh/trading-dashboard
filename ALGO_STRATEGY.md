# ORB Algo — Opening-Range Breakout auto-trader

An automatic intraday options trader for **NIFTY** and **SENSEX**, driven by the
09:15–09:25 opening range. Read the **"Honest verdict"** section before you turn
it on with real money — it is the single most important part of this document.

---

## 1. The strategy (one page)

```
09:15 - 09:25   build the opening range (OR high / OR low) for both indices
09:26           lock the levels, pre-pick the ATM option, size the lots
after 09:26     watch BOTH indices tick-by-tick
                  spot breaks OR HIGH  ->  BUY CALL (ATM)
                  spot breaks OR LOW   ->  BUY PUT  (ATM)
                the FIRST index to break takes the trade;
                the other is locked out for the rest of the day
in the trade    managed on SPOT levels:
                  stop   = entry -/+ (0.2 x OR range)
                  target = entry +/- (2.0 x OR range)
15:12           forced square-off
15:30           session closed, everything archived
```

**One trade per day, whichever index moves first.** That is the whole edge:
the opening range is a volatility filter, and a clean break of it tends to
extend. Everything else in this repo exists to execute that reliably.

---

## 2. The 5-year study — real numbers

Run it yourself: `python run_algo_study.py --years 5` (or the **5-year study**
tab in the Algo page). It sweeps 8 stops x 8 targets over **1,268 sessions per
index** (2021-08-28 → 2026-09-27) of real 1-minute data.

### Opening-range / follow-through behaviour

| | NIFTY | SENSEX |
|---|---|---|
| average opening range | 76.1 pts | 265.5 pts |
| MFE median after breakout | 0.77x OR | 0.73x OR |
| MFE p70 | 1.40x OR | 1.33x OR |
| MFE p80 | 1.93x OR | 1.87x OR |
| MFE p90 | 2.71x OR | 2.75x OR |

Read this carefully: **half of all breakouts never travel even 0.8x the opening
range.** The winners are a small minority that run 2x-3x. That is why the
target is far away and the stop is close — it is a lottery-ticket distribution,
not a smooth curve.

### Expectancy grid (R per trade, gross)

| stop × OR | target 1.0x | 1.5x | **2.0x** | 2.5x |
|---|---|---|---|---|
| 0.15x | 0.244 / 0.188 | 0.269 / 0.190 | 0.280 / 0.216 | 0.257 / 0.168 |
| **0.20x** | 0.144 / 0.153 | 0.183 / 0.178 | **0.225 / 0.205** | 0.193 / 0.175 |
| 0.25x | 0.130 / 0.075 | 0.159 / 0.123 | 0.199 / 0.145 | 0.180 / 0.131 |
| 0.30x | 0.102 / 0.060 | 0.122 / 0.094 | 0.128 / 0.122 | 0.127 / 0.104 |

*(NIFTY / SENSEX)*

Two things fall out of this table:

1. **target = 2.0x OR is the best target at every single stop level**, on both
   indices. That is a consistent, non-cherry-picked result.
2. **The stop barely matters** as long as it is tight. Tightening from 0.30x to
   0.15x roughly doubles expectancy, because the loss size shrinks while the
   win size stays the same.

### What the algo ships with

```
stop   = 0.20 x opening range
target = 2.00 x opening range
```

Why 0.20x and not the table's in-sample best 0.15x: a 0.15x stop on NIFTY is
~11 spot points. That is narrower than realistic intraday noise plus the option
bid/ask you have to cross. The backtest assumes you exit at exactly that price;
in the real market you would not. 0.20x costs about 0.02R of expectancy and
buys you a stop you can actually execute — and it is the **best** configuration
for SENSEX anyway. The study's own recommender applies the same floor, so the
"5-year study" tab and the live defaults always agree.

---

## 3. Honest verdict: the edge is thin, and cost decides everything

This is the part most strategy write-ups leave out.

The shipped configuration earns roughly **+0.21R per trade, gross**, with a
profit factor of ~1.21 and a ~15% win rate. In rupee terms that is the number
that matters, and it is small:

| | NIFTY | SENSEX |
|---|---|---|
| lot size | **65** | 20 |
| average **gross** profit per trade (1 lot) | **₹101** | **₹85** |
| real Dhan round-trip cost (1 lot) | ~₹64 | ~₹52 |
| **net per trade (1 lot)** | **~₹37** | **~₹33** |
| trades per year | 247 | 245 |

> **Break-even cost == the average gross profit per trade.** If a round trip
> costs you more than ₹101 (NIFTY) or ₹85 (SENSEX) *per lot*, the strategy loses.

### Cost is NOT flat across lots — this matters a lot

The broker's flat fee is only part of it; STT and exchange charges scale with
premium turnover. For NIFTY (₹115 in, ₹116.55 out, lot 65):

| lots | qty | flat part (brokerage+GST) | scaling part (STT/exch/stamp) | **total** | **per lot** |
|---|---|---|---|---|---|
| 1 | 65 | ₹47 | ₹17 | **₹64** | ₹64 |
| 2 | 130 | ₹47 | ₹33 | **₹80** | ₹40 |
| 4 | 260 | ₹47 | ₹67 | **₹114** | ₹28 |
| 8 | 520 | ₹47 | ₹134 | **₹181** | ₹23 |

So the "₹80 for one buy + one sell" you see on a contract note is exactly right
for **1 lot** — but the same order pair on **4 lots** costs about **₹114**, not
₹80. Per *lot* it is much cheaper though, which is the real reason size helps.

**NIFTY, 0.2x / 2.0x, 1 lot = 65:**

| round-trip cost | net points (5y) | profit factor | net ₹ (5y) | net ₹ / year |
|---|---|---|---|---|
| ₹0 | +3,888 | 1.24 | +₹126,360 | +₹25,272 |
| ₹50 | +2,219 | 1.13 | +₹72,118 | +₹14,424 |
| ₹64 (real) | ~+1,530 | ~1.09 | ~+₹49,700 | ~+₹9,940 |
| **₹101** | **~0** | **1.00** | **~₹0** | **~₹0** |
| ₹150 | −1,120 | 0.94 | −₹36,400 | −₹7,280 |
| ₹300 | −6,128 | 0.75 | −₹199,160 | −₹39,832 |

**SENSEX, 0.2x / 2.0x, 1 lot = 20:**

| round-trip cost | net points (5y) | profit factor | net ₹ (5y) | net ₹ / year |
|---|---|---|---|---|
| ₹0 | +10,620 | 1.19 | +₹106,201 | +₹21,240 |
| ₹50 | +4,405 | 1.07 | +₹44,051 | +₹8,810 |
| **₹85** | **~0** | **1.00** | **~₹0** | **~₹0** |
| ₹100 | −1,810 | 0.97 | −₹18,099 | −₹3,620 |
| ₹150 | −8,025 | 0.89 | −₹80,249 | −₹16,050 |
| ₹300 | −26,670 | 0.69 | −₹266,699 | −₹53,340 |

**Both indices, 1 lot each, combined:**

| round-trip cost | net per year |
|---|---|
| ₹0 | **+₹46,500** |
| ₹50 | **+₹23,200** |
| ₹64 / ₹52 (real) | **+₹16,000** |
| ₹100 | **−₹50** ← essentially zero |
| ₹150 | **−₹23,300** |

At the **real** Dhan cost (₹64 on NIFTY, ₹52 on SENSEX for 1 lot) this is about
**+₹16,000 a year on 1 lot each** — which sounds fine until you see the
drawdowns in the next section.

### So what does that mean?

The strategy **breaks even at roughly ₹85–116 of round-trip cost per lot**.
Here is what a realistic round trip actually costs on 1 lot of an ATM NIFTY
option (75 qty, ~₹150 premium):

| item | approx |
|---|---|
| brokerage (₹20/order × 2) | ₹40 |
| STT, 0.1% on the sell premium | ₹11–17 |
| exchange + SEBI + stamp + GST (18% on brokerage) | ₹15–20 |
| slippage — you cross the ATM spread twice, 0.5–1 pt | ₹75–150 |
| **total per round trip** | **₹140–225** |

That lands **above** the ₹101 break-even for NIFTY and well above the ₹85 for
SENSEX *if you trade 1 lot*. **At 1 lot this strategy is marginal.**

But there is a much bigger trap than costs, and it is the thing that actually
kills accounts — read the next section.

### The real account-killer: 39–46 losses in a row

Costs are a slow leak. This is the thing that actually wipes you out.

The strategy wins ~15% of the time. That is not a bug, it is the design — it
pays for many small losses with a few large wins. But it means very long losing
streaks are not just possible, they are **mathematically certain**. The expected
longest run of losses in N trades is `ln(N) / ln(1/p)`, which for 1,252 trades
at a 15% win rate is **46**. The simulation measures 46 (NIFTY) and 39 (SENSEX).

Now put ₹30,000 in the account and run the whole 5 years (1,271 NIFTY trades):

| sizing | NIFTY | SENSEX |
|---|---|---|
| ₹30k, **4 lots** ("spend the whole balance") | **💀 wiped** −77% | **💀 wiped** −93% |
| ₹30k, **fixed 1 lot** | **+149%** (₹74,808) | **+151%** (₹75,206) |

Same strategy, same 5 years, same costs. The only difference is how many lots.

Year by year at 1 lot — note it is **not** a smooth ride:

| year | NIFTY | SENSEX |
|---|---|---|
| 2021 | +₹18,375 | +₹14,208 |
| 2022 | +₹18,030 | +₹25,552 |
| **2023** | **−₹26,831** | **−₹14,414** |
| 2024 | +₹19,133 | −₹1,901 |
| 2025 | +₹18,092 | +₹17,590 |
| 2026 (to Sep) | −₹1,991 | +₹4,171 |

In this particular ordering the account never fell below ₹25,000 (−16.7%). But
that is **one path**, and one path tells you nothing about the next one.

### Monte Carlo: shuffle the trade order and see what actually happens

P&L is additive, so the final number does not depend on order — but *survival*
does. Reshuffle the same trades 400–500 times and count how often the account
hits zero before the end:

| account | lots | **wiped out** | worst-case low (5th pct) | median low |
|---|---|---|---|---|
| ₹30k | 1 | **24%** ⚠ | −₹512 (ruined) | ₹11,884 |
| ₹1L | 1 | **0%** ✅ | ₹47,938 (−52%) | ₹81,790 |
| ₹1L | 4 | **15%** ⚠ | −₹1,593 (ruined) | ₹59,636 |
| ₹2L | 2 | **0%** ✅ | ₹1,11,084 (−44%) | ₹1,74,785 |
| ₹4L | 4 | **0%** ✅ | ₹2,45,231 (−39%) | ₹3,59,109 |

Read that table carefully — it is the whole answer:

- **₹30k with 1 lot has a ~1-in-4 chance of ruin.** The +149% headline is real,
  but it is the *median-ish* path. A quarter of orderings die first.
- **₹1L per lot is the point where ruin probability goes to zero.**
- **Scaling faster than ₹1L per lot brings ruin risk straight back** — ₹1L with
  4 lots is 15% ruined even though ₹1L with 1 lot is safe.

So the sizing rule is simply: **≈₹1,00,000 of capital per lot, and re-check it
every time you add size.**

**"But I have ₹30k — how many lots?"** Same test, ₹30k held fixed, 500 shuffles:

| lots on ₹30k | loss per trade | as % of account | this path ended at | **💀 wipe-out chance** |
|---|---|---|---|---|
| **1** | ₹558 | 1.9% | ₹74,808 | **24%** |
| 2 | ₹1,102 | 3.7% | ₹1,79,607 | **42%** |
| 3 | ₹1,646 | 5.5% | ₹2,84,406 | **53%** |
| 4 | ₹2,090 | 7.0% | ₹3,89,205 | **59%** |

The "end" column is the seductive one — 4 lots shows +1,197% and it is a lie,
because **59% of orderings never get there.** More than half. That is not a
strategy, it is a coin flip with extra steps.

> **On ₹30,000: 1 lot.** And understand that even 1 lot is a 1-in-4 gamble —
> the genuinely correct answer is "wait until ₹1,00,000".

"Size by balance" is right in spirit, but the ratio is **₹1L per lot**, *not*
"buy whatever the balance affords". Spending the whole balance on premium is the
single worst option: it was wiped out on both indices in the simulation.

### How the algo actually sizes (implemented)

`AlgoEngine._size_position` floors the risk cap at **`min_lots` (default 1)**,
and allows a single lot whenever the balance can buy one outright — even if the
capital cap would rather it did not. Affordability still has the final say.

| balance | LOTS | why |
|---|---|---|
| ₹5,000 | **0** | `insufficient balance` — 1 lot costs ₹7,475 |
| ₹8,000 | **1** | minimum lot enforced |
| ₹30,000 | **1** | minimum lot enforced |
| ₹1,00,000 | **1** | risk budget agrees |
| ₹2,00,000 | **2** | scaling |
| ₹4,00,000 | **4** | scaling |

`risk_pct` (0.5%) is derived in `config.py` from `algo_streak_budget_pct /
algo_max_loss_streak` = 25 / 50, and `algo_effective_risk_pct` caps it so a stray
`ALGO_RISK_PCT=2` in `.env` cannot silently re-introduce a 4-lot-on-₹1L position.

> ⚠️ **Known trade-off:** forcing 1 lot on a small balance deliberately
> overrides the streak budget. At ₹30,000 that restores the **24% wipe-out risk**
> shown in the table above, and at ₹10,000 it is materially worse. The floor is an
> operator decision, not a safe-sizing recommendation — the safe entry point is
> still ₹1,00,000.

Why 4 lots dies: each losing trade costs about `4 × ₹494 + ₹114 ≈ ₹2,090`. A
46-trade losing streak is therefore `46 × ₹2,090 ≈ ₹96,000` — more than 3x the
account. You are gone long before the streak ends, and the strategy's whole
payoff depends on surviving to the few big winners.

Why "2% risk" also lost money: at ₹30k, 2% risk sizes 1 lot. But as the balance
grows to ₹1L it sizes 4 lots, and *then* the 46-loss streak arrives. Compounding
a 15%-win-rate, fat-tailed strategy accelerates you straight into ruin.

**The sizing rule that follows from this:**

> Lots must be small enough that a **50-trade losing streak** costs no more than
> ~25–30% of the account.

Solving that for NIFTY (loss per lot per trade ≈ ₹511, plus ₹47 flat per trade):

| lots | capital needed |
|---|---|
| 1 | ~₹1,00,000 |
| 2 | ~₹2,00,000 |
| 4 | ~₹3,85,000 |

**A ₹30,000 account is too small for even 1 lot of NIFTY by this rule.** It
survived 2021–2026 in the simulation, but with a 38.8% max drawdown and a
76% peak-to-trough on the smaller sizing — that is luck of the path, not safety.

Run it yourself:

```
python run_algo_account_sim.py --balance 30000 --premium 115 --mode full
python run_algo_account_sim.py --balance 30000 --premium 115 --mode risk --risk 2
python run_algo_account_sim.py --balance 30000 --premium 115 --mode fixed --lots 1
```

### What would actually improve it

1. **More lots.** Brokerage is per-order, so the *fixed* part of the cost does not
   scale with quantity (the variable STT/slippage part does). At 5 lots the
   break-even cost per lot drops by roughly the ₹40 of brokerage, i.e. ~₹150 for
   NIFTY. That is the single biggest lever, and it is why a larger account does
   better here than a small one.
2. **A limit entry at the range edge** instead of a stop-market entry, to avoid
   paying the breakout premium. Our backtest already assumes the ideal fill, so
   this is about not doing *worse* than the model.
3. **A maker/taker check:** if you can get a rebate or near-zero brokerage, the
   ₹0–50 column applies and the edge is real, if small.
4. **A better filter.** The numbers above are the *unfiltered* strategy. Skipping
   low-range days or days with a scheduled event is untested here and is the most
   promising direction.

> **Therefore: run it in DRY-RUN first.** That is the default. The dry-run
> simulates every decision against live prices and writes a full journal and
> trade log, so after two weeks you will have your *own* numbers instead of a
> backtest's. Only then decide.

---

## 4. How the algo actually operates

### Stop / target: software **and** broker-side

The stop and target are **spot levels monitored in software**. On every tick
(from the WebSocket, or the 1-second REST fallback) the engine compares the
index spot against them and fires a **MARKET exit** the moment one is hit. This
is precise, because the strategy is defined on spot — not on the option premium.

That has one weakness: it depends on this process staying alive. So immediately
after the entry fill the engine also **parks a much wider real stop at Dhan**
as a `STOP_LOSS_MARKET` order, at `protective_sl_mult` (default 2x) the software
stop distance, converted to a premium price. If the server dies, you are not
left naked — Dhan closes it.

On a software exit the protective order is cancelled first, then the exit is
sent. The protective stop is deliberately far away so it and the software exit
cannot realistically race.

*(Set `protective_sl: false` in Settings if you don't want an extra order at Dhan.)*

### Restart / crash recovery

State is written to `backend/data/algo/state.json` after every meaningful change
(arm, OR locked, entry, stop move, exit). On startup the engine:

1. reloads the day, phase, OR levels, config and open position from disk
2. **re-syncs with Dhan's real position book** (`POST /api/algo/reconcile`):
   - position we track but Dhan no longer holds → marked closed
   - quantity mismatch → adopts Dhan's quantity
   - position at Dhan we don't track → logs a loud error telling you to square it off
3. resumes monitoring from wherever it left off

OR levels are derived from the day's 1-minute data, so they are reproducible —
a restart never loses the levels. A new calendar day resets everything
automatically.

Caveat: entries are *events*, not state. If the server is down at the exact
moment the range breaks, that entry is missed. The engine only trades while it
is running.

### Journal, EOD and retention

Every action is appended to `backend/data/algo/journal-YYYY-MM-DD.jsonl`.
Closed trades go to `trades.jsonl` (kept forever, for the equity curve).

**Journals older than 7 days are deleted automatically** — at startup and once a
day. That is the retention you asked for: the last week is always on the page,
older ones clean themselves up, and the trade log survives for long-term
debugging.

---

## 5. The Algo page

Everything about the algo lives in one tab, and it is written to be readable at
a glance during market hours:

- **ALGO ON/OFF switch** — the only control you need.
- **"Ab kya ho raha hai"** — the current phase in plain words.
- **"Aage kya hoga"** — the next planned actions (break-even level, square-off
  time, which index is being watched), so you are never guessing.
- **Feed health** — whether you are on the live WebSocket tick stream or the
  1-second REST fallback, plus tick count and last-tick age.
- **One card per index** — OR high/low/range, live spot, a bar showing where
  spot sits inside the range, distance to breakout, the pre-picked ATM strike
  and premium, and the stop/target that *would* apply if it breaks here.
- **Position card** — entry vs live spot, target, stop, premium P&L, R-multiple,
  MFE/MAE, and whether the disaster stop is parked at Dhan.
- **Live log**, **today's trades**, **5-year study**, **Journal (7 din)** and
  **EOD / aaj ka result** sub-tabs.

### Safety

- **DRY-RUN is the default.** Nothing reaches Dhan while it is on; fills are
  simulated from live prices.
- **EXIT NOW / panic button** squares off immediately.
- Disarming does **not** abandon an open position — it keeps managing it, and
  says so in the log.
- The page polls one cheap in-memory endpoint once a second. It never blocks on
  Dhan, so it cannot hang.

---

## 6. Configuration

All settings also live in the **Settings** sub-tab and persist across restarts.

| Setting | Default | Meaning |
|---|---|---|
| `ALGO_INDICES` | `NIFTY,SENSEX` | which indices to watch |
| `ALGO_SL_MULT` | `0.2` | stop = this × opening range |
| `ALGO_TARGET_MULT` | `2.0` | target = this × opening range |
| `ALGO_BREAKEVEN_AT` | `0.0` | move stop to entry after +N × OR (0 = off) |
| `ALGO_ENTRY_CUTOFF` | `15:00` | no fresh entries after this (IST) |
| `ALGO_EXIT_TIME` | `15:12` | forced square-off |
| `ALGO_STRIKE_OFFSET` | `0` | 0 = ATM, 1 = one strike OTM |
| `ALGO_RISK_PCT` | `2.0` | % of balance risked per trade |
| `ALGO_MAX_CAPITAL_PCT` | `60.0` | max % of balance spent on premium |
| `ALGO_MAX_LOTS` | `15` | hard cap |
| `ALGO_MIN_OR_PCT` / `ALGO_MAX_OR_PCT` | `0` | skip days whose OR is too small/large (0 = off) |
| `ALGO_ORDER_TYPE` | `MARKET` | MARKET (fast) or LIMIT |
| `ALGO_PRODUCT_TYPE` | `INTRADAY` | `INTRADAY` or `MARGIN` |
| `ALGO_LOT_SIZES` | *(empty)* | override, e.g. `NIFTY=75,SENSEX=20` |

**Position sizing** uses the live fund balance (or the paper balance in dry-run):
`lots = min(risk_budget / (stop_points × delta × lot), capital_budget / (premium × lot), max_lots)`.
At 2% risk and a 76-point OR, that is about **2% of balance per trade**. The
study's per-trade drawdown (2,700 SENSEX points ≈ ₹27,000/lot, 758 NIFTY points
≈ ₹28,000/lot) is the number to sanity-check your account size against.

> `ALGO_BREAKEVEN_AT` defaults to **off** on purpose: the 5-year numbers were
> measured without it, so the live behaviour matches the validated backtest
> exactly. Turning it on is a reasonable risk choice but it changes the
> distribution and is untested here.

---

## 7. Files

| File | Role |
|---|---|
| `app/algo.py` | the engine: state machine, sizing, entry/exit, recovery |
| `app/feed.py` | WebSocket tick feed + 1-second REST fallback |
| `app/algo_store.py` | state.json, rolling 7-day journal, trade log |
| `app/orb_study.py` | the 5-year sweep (same code the UI tab runs) |
| `app/routers/algo.py` | `/api/algo/*` endpoints |
| `app/orb.py`, `app/opening_range.py` | shared simulation + rate-limited history fetch |
| `run_algo_study.py` | CLI for the study |
| `frontend/src/pages/AlgoPage.tsx` | the whole Algo tab |

---

## 8. Troubleshooting

**"DH-904 / Too many requests"** — Dhan's data API is strict. The historical and
intraday fetches share one global gate (1.1s apart) and back off on 429, so a
few warnings during a long backfill are normal and retried. If it never
recovers, wait a minute.

**Feed says "offline"** — the WebSocket needs the Dhan access token to be valid
and your IP to be whitelisted. The engine automatically falls back to the
1-second REST poller, which is enough for this strategy (a 1-second delay on a
breakout is a real cost though — check the feed badge before trusting a fill).

**"option chain not ready"** — the chain poller refreshes each underlying every
3.5s. If you arm the algo before 09:26 this clears on its own. The engine also
force-refreshes the chain right before entering, so a stale snapshot can't cause
a wrong contract.

**Contract not ready / entry skipped** — the log will say so explicitly. Usually
means no active expiry or no spot yet.

**Nothing happened all day** — correct and common. The trade only fires on a
clean break of the opening range, and only one index gets it.
