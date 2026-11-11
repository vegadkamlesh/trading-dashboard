import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AppSettings } from "../api/types";
import { useAuth } from "../hooks/useAuth";

// Poori guide — Hinglish me. Sab kuch ek jagah: app kaise chalaye, token kaha,
// logs kaha, true/false ka matlab, order kaise lagaye, safety.
export function Readme() {
  const { dryRun, fastOrders } = useAuth();
  const [info, setInfo] = useState<AppSettings | null>(null);

  useEffect(() => {
    api.get<AppSettings>("/api/settings").then(setInfo).catch(() => {});
  }, []);

  const host = info?.host ?? "127.0.0.1";
  const port = info?.port ?? 8000;
  const logDir = info?.logDir ?? "backend/logs";
  const retention = info?.logRetentionDays ?? 7;

  return (
    <div className="mx-auto max-w-3xl space-y-5 pb-10 text-sm leading-relaxed text-slate-300">
      <div>
        <h2 className="text-lg font-bold text-slate-100">📖 Poori Guide (Hinglish)</h2>
        <p className="text-xs text-slate-500">
          Sab kuch ek jagah — jab bhi bhoolo, yahi page khol lo.
        </p>
      </div>

      {/* Current status */}
      <Section title="🔴 Abhi kya chal raha hai (Live status)">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <Badge
            label="DRY-RUN / LIVE"
            value={dryRun ? "DRY-RUN (no real trade)" : "LIVE (real trade!)"}
            danger={!dryRun}
          />
          <Badge
            label="FAST ORDERS"
            value={fastOrders ? "ON (1-click, no PIN)" : "OFF (PIN lagega)"}
            warn={fastOrders}
          />
        </div>
      </Section>

      <Section title="Master paid Data API: benefits aur kya use ho raha hai">
        <ul className="list-disc space-y-2 pl-5 text-xs">
          <li><strong>Market data:</strong> tick-by-tick spot/option LTP; REST batched LTP/OHLC/quote
            (up to 1,000 instruments/request); option chain IV, greeks, OI, volume, bid/ask, expiry list.
            App Master ticker feed, chain aur LTP/OHLC fallback use karta hai.</li>
          <li><strong>Historical:</strong> daily to inception; last 5 years intraday
            1/5/15/25/60-minute candles, max 90 days/request. Expired rolling options max 30 days/request,
            OHLC/IV/OI/spot/volume. OR, charts aur history studies ke liye; long cache sirf historical.</li>
          <li><strong>Future:</strong> richer quote/full-feed parser, liquidity filters, 20/200-level
            depth (NSE only, SENSEX nahi), more backtested strategies. Depth entitlement Dhan se confirm
            karo; purchased Data plan unlimited calls ya profit guarantee nahi.</li>
          <li><strong>Trading APIs free aur separate:</strong> orders/Super Orders, portfolio/funds,
            live order updates used. GTT/OCO, conditional triggers, margin calculators, ledger,
            broker kill switch/P&amp;L exit future options hain; silently enabled nahi. DISARM kill switch nahi.</li>
          <li><a className="text-sky-300 underline" href="https://dhanhq.co/docs/v2/"
            target="_blank" rel="noreferrer">Official Dhan API docs / limits</a>.
            Master data subscription dono views ko feed karti hai; Child token trading/account-only.</li>
        </ul>
      </Section>

      <Section title="Master Dhan + Child Dhan: toggles, same signal, separate accounts">
        <ul className="list-disc space-y-2 pl-5 text-xs">
          <li><strong>Data:</strong> market data, quotes, history aur ek WebSocket sirf Master se.
            Child ka Data API nahi chahiye. Dono ka Trading API/client ID alag hai.</li>
          <li><strong>Trading checkboxes = NEW entry recipients.</strong> Master only, Child only,
            both ya none. Viewing dropdown sirf kiska funds/orders/positions/IP/journal dikhana hai;
            recipient selection nahi badalta. View change par purana ticket/panel clear hota hai.</li>
          <li><strong>Manual:</strong> same contract/direction/lots har selected account ko.
            Funds preview account-wise. Ek ke funds kam ya rejection ho to doosra continue;
            successful trade automatically reverse nahi hota. Dono results alag check karo.</li>
          <li><strong>Algo:</strong> Master view se shared config edit karo. ARM har recipient ka IP
            check karta hai; failed account clearly report aur disarm, healthy account continue.
            Master trading OFF ho tab bhi Master signal coordinator rehta hai, Master trade nahi.
            Same contract/direction/target anchor; lots har account ke apne balance se.
            Fill price aur premium SL account ke actual fill ke hisab se alag ho sakte hain.
            ON/OFF toggle shared hai: kisi bhi view se ON = checked recipients arm,
            OFF = sab disarm. Algo ready list actual armed recipients dikhati hai;
            Viewing se toggle ka shared status nahi badalta.</li>
          <li><strong>EXIT:</strong> viewed account only. EXIT BOTH exact contract dono configured
            accounts mein; ALGO EXIT BOTH unke apne tracked algo positions mein. Entry OFF karna
            existing holding management/exit band nahi karta. Exit pending/error ko CLOSED mat samjho.</li>
          <li><strong>Restart:</strong> selection Master-only; new algo entries disarmed, old positions
            reconcile/manage hote hain. Dono account ki tracked position par unsafe mode/config/selection
            changes blocked. Ek shared signal attempt/day; reject/timeout par blind replay nahi.
            Dono fills atomic ya same price par hone ki guarantee nahi.</li>
          <li><strong>Backend .env:</strong> DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN = Master data;
            optional DHAN_MASTER_TRADING_TOKEN = Master trading (same client ID).
            DHAN_CHILD_CLIENT_ID + DHAN_CHILD_ACCESS_TOKEN = Child trading. Dono Child values fill karo,
            distinct client ID use karo, phir restart. Secrets frontend/Git mein mat dalo.
            Master ka same token trading + data dono ke liye use karna ho to
            DHAN_MASTER_TRADING_TOKEN blank rakho.
            Har account ka funds aur static IP check karke hi LIVE/ARM karo.</li>
          <li><strong>Same static IP:</strong> Dhan ke official FAQ ke hisab se eligible family accounts
            verification/approval ke baad ek IP share kar sakte hain. Registered email se
            apihelp@dhan.co ko mapping request bhejo, respective family accounts ke registered emails
            CC mein rakho. Sirf dono mein IP register karna approval nahi hai. Apne accounts ki eligibility
            Dhan se confirm karo; app ka IP preflight approval certify nahi karta.{" "}
            <a className="text-sky-300 underline"
              href="https://dhan.co/support/platforms/dhanhq-api/can-multiple-family-member-accounts-use-the-same-static-ip-for-api-trading/"
              target="_blank" rel="noreferrer">Official Dhan FAQ</a>.</li>
          <li><strong>Debug:</strong> account-master-YYYY-MM-DD.log / account-child-YYYY-MM-DD.log;
            batch/correlation ID aur broker reason se trace karo. Separate audit-child.sqlite3 aur
            data/algo/child state. Data gates shared, trading budgets account-wise; ek backend process
            hi chalao. Offline tests real credentials, broker availability ya fills certify nahi karte.</li>
        </ul>
      </Section>

      <Section title="🧩 Algo 1 + Algo 2: common rules">
        <ul className="list-disc space-y-2 pl-5 text-xs">
          <li>Do tab: <b>🤖 Algo 1</b> (ORB) aur <b>🤖 Algo 2</b> (options-flow). Dono ka apna ON/OFF aur apna PAPER/LIVE.</li>
          <li>Master + Child: same signal, har account apne balance se size. Market data sirf Master Data API se; Child sirf trading.</li>
          <li>Product alag: Algo 1 = MARGIN (exit 15:29), Algo 2 = INTRADAY (exit 15:15). Kabhi mix nahi.</li>
          <li>Balance shared hai. <b>Algo 2 ko priority</b> (LIVE): Algo 1 ka trade margin rok raha ho aur Algo 2 ka signal aaye → Algo 1 turant market exit (ALGO2_PRIORITY), SELL fill hote hi Algo 2 entry (~0.5–1 s, Dhan ke confirmation pe depend).</li>
          <li><b>Default:</b> dono algo ON + LIVE, fast orders ON (APP_DRY_RUN=false, APP_FAST_ORDERS=true, ALGO_AUTO_ARM=true). Restart pe Telegram: "Algo 2 ON - LIVE after server start".</li>
          <li>Same Telegram channel; Algo 2 ke messages "Algo 2" se shuru. Algo 1 journal 7 din; Algo 2 journal + history hamesha save.</li>
          <li>AWS server Mon–Fri 09:00 start, 16:00 stop (NSE holiday skip). Stop square-off nahi karta.</li>
        </ul>
      </Section>

      <Section title="🤖 Algo 1 (ORB): BUY, SL, target aur exit ka exact flow">
        <ol className="list-decimal space-y-2 pl-5 text-xs">
          <li>
            <strong>Range:</strong> 09:25 IST par pehle 10 minute (09:15:00–09:24:59, 10 candles)
            ka high/low lock hota hai.
            Spot high se strictly upar = CALL BUY; low se strictly neeche = PUT BUY.
            First index trade leta hai; doosra lock. Default 15:00 se fresh entry band.
            Index quotes 2 decimals pe compare hote hain, float noise breakout nahi banta.
          </li>
          <li>
            <strong>Contract:</strong> offset 0 = ATM; +1 = one strike OTM.
            Spot aur option premium 5 seconds se purane hon to fresh entry wait karti hai.
            Entry bhejne se pehle breakout recheck hota hai.
          </li>
          <li>
            <strong>Order accepted != filled:</strong> TRANSIT/PENDING par position filled nahi
            maani jaati. Broker ka confirmed filled quantity + average price chahiye.
            Partial BUY ka remainder cancel-confirm hone ke baad actual filled units manage hote hain.
            Unknown network result par correlation ID se check hota hai, blind duplicate order nahi.
          </li>
          <li>
            <strong>Target-only (default ON):</strong> software spot SL aur break-even exit OFF.
            R = OR high - OR low, S = shared breakout dispatch ka spot.
            CALL target = S + 2R; PUT target = S - 2R (default multiplier).
            Example CALL S=22,750, R=100: target 22,950. Spot 22,730 ya neeche aane
            se software stop exit nahi hoga. Fresh spot target hit par market exit request.
          </li>
          <li>
            <strong>Classic mode only:</strong> Target-only untick karoge tab 0.20R spot SL
            aur configured break-even apply ho sakte hain. Study results classic mode ke hain;
            target-only performance unse assume mat karo.
          </li>
          <li>
            <strong>Protective premium SL:</strong> confirmed fill ke baad Dhan pe pending
            STOP_LOSS_MARKET SELL at 35% below entry premium (NSE LPP ?40% se zyada door SL reject karta hai), minimum 0.05, tick 0.05. Backup: premium 50% gira to algo khud MARKET exit (SOFTWARE_SL).
            Rs100 BUY ho to far SL Rs10. Close software SL nahi. Large loss possible;
            SL hit na hone ki guarantee nahi, gap mein aur loss ho sakta hai.
            Pending SELL dikhna matlab BUY ke turant baad actual SELL fill hona nahi hai.
            Trigger already crossed ho to safe exit request hoti hai.
          </li>
          <li>
            <strong>Safe exit:</strong> protective SL cancel/fill confirm → actual broker qty recheck
            → zaroorat ho tab MARKET SELL. Duplicate PANIC / auto / Positions EXIT serialized hain.
            Exit PENDING ya rejected ho to dashboard CLOSED nahi bolta.
            Broker zero qty aur pending SELL clear hone par trade journal mein close hota hai.
            Default ORB forced exit 15:29. Manual Super Orders par ye app-wide timer nahi hai.
            Broker/connection failure ho to Dhan par manually check karo.
          </li>
          <li>
            <strong>Lots:</strong> risk budget, premium affordability aur max lots se size.
            Default minimum 1 lot risk/capital % ko override kar sakta hai, actual cash ko nahi.
            Default max 10, capital 60%, effective risk 0.5%. Saved Settings defaults se alag ho sakti hain.
            Target-only risk estimate 35% premium loss per lot leta hai, purana tight spot-stop estimate nahi.
            Registry NIFTY 65 / SENSEX 20 ko LIVE se pehle current contract se verify karo.
          </li>
          <li>
            <strong>OFF / restart:</strong> Disarm sirf fresh BUY band karta hai; existing holding ka
            stop/target management aur broker sync continue. Open position par mode/config change blocked.
            Restart pending order IDs aur holdings reconcile karta hai; next day surviving holding
            silently delete nahi hoti. Server bandh ho to software SL/target nahi chalega.
            Purani saved policy target-only mein migrate hoti hai; old broker SL cancel-confirm hone
            tak purana SL still execute ho sakta hai. Broker warning check karo.
            Prior-day INTRADAY stale tracking tabhi recover hoti hai jab do fresh holdings checks
            flat aur current order/leg books safe hon. Unknown/same-day holding delete nahi hoti.
            Recovered exit price estimated hota hai, actual historical fill nahi.
            Restart par configured DRY-RUN default normal hai. Mode switch pe algo disarm hota hai;
            settings verify karke explicitly ARM karo. Failure ka actual reason Settings mein dikhega.
          </li>
        </ol>
      </Section>

      <Section title="🤖 Algo 2 (options-flow): alag ON/OFF aur alag Paper/Live">
        <ul className="list-disc space-y-2 pl-5 text-xs">
          <li>Algo 2 ka apna tab, ON/OFF, PAPER/LIVE aur Master/Child ticks. <b>Default: ON + LIVE</b> — server start pe khud arm, signal pe real order. Paper↔Live sirf OFF aur flat hone par.</li>
          <li>Sirf <b>INTRADAY</b>; Algo 1 MARGIN pe rehna chahiye warna Algo 2 ARM nahi hoga.</li>
          <li>Trade time: 09:45–15:00. 09:45 se pehle aur 15:00 ke baad entry nahi; SL/target hit nahi hua to 15:15 pe exit. News event ke ±15 min entry nahi (events tab mein add/remove).</li>
          <li>Entry: last closed 5-min candle high + buffer (NIFTY 0.5 / SENSEX 1.5) toota = CALL; low − buffer = PUT. <b>🔒 4 gates hamesha pass</b> (News, Trade window, Heavyweight divergence, VIX) <b>+ 🎯 10 score conditions mein se kam se kam 7</b> (Settings → "Min score", badal sakte ho; score conditions ke ON/OFF switch). OI velocity 10%, VWAP band 1.0 SD. Strike ITM, delta ~0.70.</li>
          <li><b>Permanent log (kabhi delete nahi):</b> <code>data/algo2/history/YYYY-MM-DD/</code> — reports.jsonl (har 5 min report), metrics.jsonl (har minute data), chain.jsonl (har minute raw option chain ATM ±10: OI, LTP, IV, greeks), breakouts.jsonl (har breakout liya/skip + 20 min baad result). Algo 2 trade journal bhi kabhi delete nahi.</li>
          <li><b>Daily cache update:</b> har trading din 15:31 ke baad (16:00 stop se pehle) us din ki 1-min candles — NIFTY/SENSEX spot, futures (OI ke saath), VIX, 6 heavyweights — <code>candles/</code> mein, aur <code>day_summary.json</code> (breakouts, score distribution, kaunsa rule fail, skip hua par 2R hit hota, trades/P&amp;L). ~11 calls, 3 s gap, market time mein kabhi nahi. Rate-limit pe 10 min baad retry; server band tha to agle start pe 09:15 se pehle catch-up.</li>
          <li>SL: breakout candle ka low (CALL) / high (PUT). 0.75% capital cap default OFF. Broker pe 2× doori pe safety SL. Data SL (OI trap) default OFF.</li>
          <li>Target: <b>2R pe poora exit</b>. OI-wall target, 70% partial booking aur 20-min rule code mein hain par default OFF (Settings se ON kar sakte ho).</li>
          <li><b>Priority over Algo 1</b> (Settings switch, default ON, sirf LIVE): balance kam ho aur Algo 1 ki LIVE position ho → Algo 1 ka parked broker SL order <b>ek hi call mein MARKET mein modify</b> (same order, double SELL nahi), fill ka pata Dhan order-update stream se turant, TRADED hote hi Algo 2 BUY (funds update ke liye 5 tries, 0.1 s gap). Modify fail ho to safe exit (SL cancel + MARKET SELL). Lagbhag 5 broker round-trip ≈ 0.5–1 s; har step Dhan confirm karta hai, isliye second ke andar ki guarantee nahi — Telegram pe asli ms dikhega. 6 s mein SELL fill nahi hua to Algo 2 skip + error. OFF/PAPER mein sirf "insufficient balance" skip.</li>
          <li>Din mein trade limit nahi (default OFF), par ek time pe ek hi position — pehla band hone ke baad hi naya signal. "SL ke baad stop" default OFF.</li>
          <li><b>Notifications (quiet):</b> entry, T1, exit, SL turant. Baaki sirf har 5 min report — spot, triggers, conditions pass/fail, position, aur us 5 min mein skip hue breakouts. "Aage kya hoga" bhi sirf 5 min pe ya entry/exit pe update hota hai.</li>
          <li>Data sirf Master Data API; option chain shared, extra calls nahi. Historical backtest possible nahi (Dhan ke paas purana chain greeks/OI nahi).</li>
        </ul>
      </Section>

      <Section title="Manual orders: reference, retries aur safety">
        <ul className="list-disc space-y-2 pl-5 text-xs">
          <li>Ticket ka exact-contract Ref LTP shared chain se 3s pe refresh hota hai. LIMIT price khud change nahi hota; LTP button se reset karo. Stale quote ya preview error par Confirm disabled hai.</li>
          <li>MARKET submit pe server fresh cached premium (maximum 12s old) se target/SL calculate karta hai, actual fill se nahi. Slippage ke kaaran quick target/stop fill phir bhi possible hai.</li>
          <li>Whole exchange lots aur 0.05 tick validate hote hain. Rounding se target/SL entry ke barabar ya invalid ho to order reject hota hai. Manual aur algo lot override same hai.</li>
          <li>Current balance unknown/malformed ho to order send nahi hota. Start-of-day balance available cash nahi hai. Short SELL ke liye premium estimate full exchange margin approval nahi hai.</li>
          <li>Har ticket ka request ID aur broker correlation SQLite mein save hota hai. Same ID retry pe duplicate BUY/SELL nahi; unknown result pe block. Naya ticket naya intent hai: timeout ke baad Dhan check kiye bina reopen/reorder mat karo.</li>
          <li>Manual EXIT identity restart ke baad bhi recover hoti hai. Pending entry pe pehle entry cancel karo; protective legs terminal-confirm hone aur actual holdings check ke baad exit order send hota hai.</li>
          <li>Manual target/SL broker ke Super Order legs hain. 15:29 app timer sirf ORB holding ke liye hai; manual trades mein EXIT aur broker square-off rules follow karo.</li>
        </ul>
      </Section>

      <Section title="Live sync aur Dhan API pacing">
        <ul className="list-disc space-y-2 pl-5 text-xs">
          <li>Algo UI, Positions, standard/Super Orders aur Funds 1s poll. Dono configured accounts
            backend mein ~1s refresh hote hain, selected/viewed na ho tab bhi. Request latency/cooldown
            se delay badh sakta hai; exact 1s guarantee nahi.</li>
          <li>Alag Master/Child trading-token order-update sockets website/app/API orders ki
            notification par fresh REST refresh trigger karte hain. Event alone holding flat ka proof nahi.</li>
          <li>Website se exit ke baad Dhan netQty priority hai: partial exit par remaining qty,
            full exit par broker FLAT. Unverified outstanding SL/exit tracking attention mein reh sakti
            hai, open holding nahi. Unknown SELL par duplicate SELL nahi.</li>
          <li>Algo P&amp;L mein available broker realized + unrealized contract total preferred.
            Broker field missing ho to quote estimate clearly labelled; broker-flat par saved
            premium profit ko live profit nahi dikhate. Missing order detail ko fresh current
            book se exact ID/correlation par verify karte hain, guessed fill nahi.</li>
          <li>Purana prior-day INTRADAY pending entry twice-fresh flat holdings aur no active/unknown
            current orders/legs ke baad safely retire hota hai. Historical fill/P&amp;L UNKNOWN,
            fake completed trade nahi; same-day/carry automatically clear nahi.</li>
          <li>Header last broker-response age, per-resource errors aur stream/REST fallback dikhata hai.
            Latest GET snapshots max 1s share hote hain, execution fresh reads use karti hai.
            &gt;3s age/error = STALE/UNVERIFIED, old success ko live mat samjho.</li>
          <li>Feed ONLINE ya ALGO ON ka matlab open trade nahi. Position state, spot age, broker-sync time alag dekho. Stale/error banner ignore mat karo.</li>
          <li>Positions/Super Orders LTP segment-aware batched quotes se aata hai. Quote failure pe holdings/EXIT available rehte hain, price STALE dikhta hai. Broker P&amp;L, holdings aur quote snapshots same instant ke hone ki guarantee nahi.</li>
          <li>Expiry list har IST day refresh hoti hai. Expiry change par old chain invalidate hoti hai; purana slow response new selection overwrite nahi karta.</li>
          <li>WebSocket silent ho to batched quote fallback ~1–2s. Spot quote 5s se stale ho to software price-trigger evaluation wait karti hai; protective broker SL verify karo.</li>
          <li>Socket healthy ho par ek option ka quote missing/5s stale ho to us subscription ka REST recovery hota hai. Feed cache aur tick consumers exchange segment + security ID use karte hain; NSE/BSE same ID ke prices mix nahi hote.</li>
          <li>Published limits (1 Nov 2026): orders 10/s, 250/min, 1,000/hour, 7,000/day; data 5/s, 100,000/day; quotes 1/s; non-trading 20/s; option chain unique request / 3s.</li>
          <li>Shared backend gates: orders per client 0.12s + rolling quotas; Master data 0.25s +
            daily quota; quotes 1.05s; account reads per client 0.06s. Option-chain unique
            client/underlying/expiry 3.1s + data budget; different chains ab same 3.1s queue mein nahi.</li>
          <li>HTTP401 invalid/expired token par 60s retry pause; market socket reconnect backoff
            handshake par reset nahi, valid price data ke baad reset. No blind order replay.</li>
          <li>Available 25/27/29 Oct logs mein unknown entry/detail, old cancel, auth/feed/IP aur
            history errors mile. Master IP error Dhan verification maangta hai; Market Closed/funds
            guards bypass nahi. Rolling history DH-905 official-shape read-only request par bhi aaya:
            study explicit unavailable, invalid request chunks repeat nahi; broker clarification pending.</li>
          <li>429 par category cooldown (Retry-After, minimum 3s). Order auto-replay nahi. Ek hi backend chalao; doosre apps ki calls aur restart ke pehle usage local budgets mein count nahi hote.</li>
          <li>Journal P&amp;L gross hai; broker fill unavailable ho to exitPriceEstimated mark hota hai. Fees/slippage aur historical study profit guarantee nahi hain.</li>
        </ul>
      </Section>

      {/* App run */}
      <Section title="▶️ App kaise chalaye">
        <ol className="list-decimal space-y-1 pl-5">
          <li>
            Project folder kholo: <code className="code">My_Project\Trading</code>
          </li>
          <li>
            <code className="code">Dhan Dashboard.bat</code> pe <strong>double-click</strong> karo.
            Ye backend + frontend dono start karega aur browser kholega.
          </li>
          <li>
            Ya alag-alag: <code className="code">Run Backend.bat</code> aur{" "}
            <code className="code">Run Frontend.bat</code>.
          </li>
          <li>
            Browser me khulega: <code className="code">http://127.0.0.1:5173</code>
          </li>
          <li>Band karne ke liye dono black windows close kar do.</li>
        </ol>
        <p className="mt-2 text-xs text-slate-500">
          Backend address: <code className="code">http://{host}:{port}</code> — ise direct browser me
          na kholo, sirf frontend use karta hai.
        </p>
      </Section>

      {/* Token */}
      <Section title="🔑 Dhan Token kaha daalein">
        <p>
          File: <code className="code">backend\.env</code> (ye file chhupi hui hoti hai, git me nahi
          jati).
        </p>
        <p className="mt-2">Uske andar ye 2 line apne value se badlo:</p>
        <pre className="mt-2 overflow-auto rounded bg-slate-950 p-3 text-xs text-green-300">{`DHAN_CLIENT_ID=1234567890
DHAN_ACCESS_TOKEN=eyJ...aapka-pura-token...`}</pre>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
          <li>Token poora copy karo — bina quotes, bina space, ek hi line me.</li>
          <li>
            Token lo: <strong>web.dhan.co → My Profile → DhanHQ Trading APIs → Access Token</strong>.
          </li>
          <li>Token ~30 din me expire hota hai. DH-901 aaye to naya token daalo.</li>
          <li>
            Sirf UI try karna hai? <code className="code">DHAN_ACCESS_TOKEN=mock-token</code> daal do —
            offline demo data chalega (asli trading nahi).
          </li>
        </ul>
      </Section>

      {/* True/False switches */}
      <Section title="🎚️ True / False ka matlab (sabse important)">
        <div className="overflow-hidden rounded border border-slate-800">
          <table className="w-full text-xs">
            <thead className="bg-slate-900 text-slate-400">
              <tr>
                <th className="px-3 py-2 text-left">Setting</th>
                <th className="px-3 py-2 text-left">Value</th>
                <th className="px-3 py-2 text-left">Kya hoga</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-t border-slate-800">
                <td className="px-3 py-2" rowSpan={2}>
                  <strong>APP_DRY_RUN</strong>
                  <div className="text-[10px] text-slate-500">(UI switch too)</div>
                </td>
                <td className="px-3 py-2 font-mono text-green-400">true</td>
                <td className="px-3 py-2">
                  Orders sirf <strong>simulate + log</strong> honge. Dhan par nahi jayenge. Paisa safe.
                </td>
              </tr>
              <tr className="border-t border-slate-800">
                <td className="px-3 py-2 font-mono text-red-400">false</td>
                <td className="px-3 py-2">
                  Orders <strong>REAL Dhan</strong> par jayenge. Asli paisa lagega. Dhyan se!
                </td>
              </tr>
              <tr className="border-t border-slate-800">
                <td className="px-3 py-2" rowSpan={2}>
                  <strong>APP_FAST_ORDERS</strong>
                  <div className="text-[10px] text-slate-500">(UI switch too)</div>
                </td>
                <td className="px-3 py-2 font-mono text-green-400">true</td>
                <td className="px-3 py-2">
                  Order <strong>PIN skip</strong> — 1 click = order. Scalping ke liye. Fast!
                </td>
              </tr>
              <tr className="border-t border-slate-800">
                <td className="px-3 py-2 font-mono text-slate-300">false</td>
                <td className="px-3 py-2">
                  Har order pe <strong>PIN</strong> maangega. Safe, par slow.
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Dono switches <strong>Settings tab</strong> se live flip kar sakte ho — restart ki zaroorat
          nahi. Restart pe <code>.env</code> ke default par wapas aa jate hain. Code default:
          <code>APP_DRY_RUN=false</code> (LIVE) + <code>APP_FAST_ORDERS=true</code>.
        </p>
      </Section>

      {/* Logging */}
      <Section title="📝 Logs (debug ke liye — sab save)">
        <p>
          Sab kuch automatic save hota hai: <code className="code">{logDir}</code>
        </p>
        <div className="mt-2 overflow-hidden rounded border border-slate-800">
          <table className="w-full text-xs">
            <thead className="bg-slate-900 text-slate-400">
              <tr>
                <th className="px-3 py-2 text-left">File</th>
                <th className="px-3 py-2 text-left">Kya likha hota hai</th>
              </tr>
            </thead>
            <tbody>
              <LogRow name="app-YYYY-MM-DD.log" desc="Sab kuch — app start, mode, sab events" />
              <LogRow name="orders-YYYY-MM-DD.log" desc="Har order attempt — price, qty, result (yaha dekho trade hua ya nahi)" />
              <LogRow name="errors-YYYY-MM-DD.log" desc="Sirf galtiyaan (errors/warnings) — jaldi scan ke liye" />
              <LogRow name="access-YYYY-MM-DD.log" desc="Har API call — method, path, status, time" />
            </tbody>
          </table>
        </div>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
          <li>
            <strong>Kitne din rakhta hai:</strong> last <strong>{retention} din</strong>. Purane logs
            app start hote waqt + har roz auto-delete ho jate hain (disk bharhne na paaye).
          </li>
          <li>
            Roz ki nayi file banti hai (date ke naam se). Aaj ka file{" "}
            <code className="code">app-{new Date().toISOString().slice(0, 10)}.log</code>.
          </li>
          <li>
            Order audit SQLite me bhi: <code className="code">backend\audit.sqlite3</code> (EOD Summary
            tab isse banata hai).
          </li>
        </ul>
      </Section>

      {/* Order kaise lagaye */}
      <Section title="🛒 Order kaise lagaye (Buy/Sell)">
        <ol className="list-decimal space-y-1 pl-5">
          <li>
            <strong>Option Chain</strong> tab kholo → index chuno (NIFTY/SENSEX).
          </li>
          <li>
            Jis strike par trade karni hai, uske <strong>B</strong> (BUY) ya <strong>S</strong> (SELL)
            button pe click karo. (Ya <strong>Trade Signals</strong> me <em>trade</em> button dabao.)
          </li>
          <li>
            Popup me: <strong>LIMIT price khud daalo</strong> (ya <em>LTP</em> button se live price),
            <strong> Target %</strong> chuno (2.5 / 5 / 10 / 15 / 20 / 25 ya manual).
          </li>
          <li>
            <strong>Stop Loss:</strong> default <strong>OFF</strong> — matlab SL nahi lagega, exit tum
            khud <em>Open Positions</em> se karoge. (Badalna ho to "Add stop loss" tick karo.)
          </li>
          <li>
            <strong>Fast Orders ON</strong>? → seedha Confirm. <strong>OFF</strong>? → PIN daalo phir
            Confirm.
          </li>
        </ol>
      </Section>

      {/* Trade Signals */}
      <Section title="🎯 Trade Signals (per-strike BUY recommendation)">
        <ul className="list-disc space-y-1 pl-5 text-xs">
          <li>
            Har strike ke liye algo batata hai — <strong>BUY CE / BUY PE / WAIT</strong>, saath me{" "}
            <strong>profit probability %</strong> aur <strong>confidence %</strong>.
          </li>
          <li>
            Chain me bhi har strike ke saath chhota badge: <span className="text-green-400">▲85%</span>{" "}
            (buy) ya <span className="text-slate-400">▬9%</span> (avoid).
          </li>
          <li>
            <strong>ⓘ button</strong> dabao to detail khulti hai: <em>ye kaise aaya, kyu decide hua</em>{" "}
            — RSI, trend, MACD, theta, IV, saare reasons.
          </li>
          <li>
            <strong>Theta warning:</strong> expiry paas aate hi time-decay premium kha jata hai — algo
            batata hai kitna risk.
          </li>
          <li>
            Ye <strong>probability-based, explainable</strong> hai — 100% guarantee NAHI.
          </li>
        </ul>
      </Section>

      {/* Seasonality + News */}
      <Section title="📅 Seasonality & 📰 News (sentiment)">
        <ul className="list-disc space-y-1 pl-5 text-xs">
          <li>
            <strong>Seasonality:</strong> pichle 5 saal ka same-date + weekday (Mon/Tue…) pattern —
            "aaj historically up ya down?"
          </li>
          <li>
            <strong>News:</strong> headlines har <strong>5 min auto-refresh</strong>, har news pe{" "}
            <span className="text-green-400">BULLISH ▲</span> /{" "}
            <span className="text-red-400">BEARISH ▼</span> / NEUTRAL tag + "why" keywords.
          </li>
          <li>
            Top pe overall market stance (BULLISH/BEARISH + counts).
          </li>
        </ul>
      </Section>

      {/* Opening Range */}
      <Section title="🕘 Opening Range (9:15–9:25)">
        <ul className="list-disc space-y-1 pl-5 text-xs">
          <li>
            Pichle <strong>1 / 3 / 5 saal</strong> me har din ke <strong>pehle 10 minute</strong>{" "}
            (09:15–09:25) ka <strong>high aur low</strong> ka study.
          </li>
          <li>
            <strong>Avg / median range</strong> (points + %) — aapka stop isse tight nahi rakhna
            chahiye, warna noise me nikal jayega.
          </li>
          <li>
            <strong>Breakout odds:</strong> range ke upar/below break kitni baar hua, aur close
            band ke andar (fakeout) kitni baar.
          </li>
          <li>
            <strong>Distribution + recent 20 sessions</strong> table — aaj ka range compare karne
            ke liye.
          </li>
          <li>
            Pehli baar compute hone me ~10–20s lagta hai (background job + progress bar), phir{" "}
            <strong>6 ghante tak cached</strong> — turant khulta hai.
          </li>
        </ul>
      </Section>

      {/* Positions / EXIT */}
      <Section title="🚪 Open Positions + 1-click EXIT">
        <ul className="list-disc space-y-1 pl-5 text-xs">
          <li>
            <strong>Positions</strong> tab me har position ka <strong>live P&L</strong> dikhta hai
            (auto-refresh ~3s).
          </li>
          <li>
            <strong>EXIT</strong> button exit request bhejta hai; broker fill tak wait karo.
            Algo position ho to wahi shared safe-exit flow chalega. Manual Super Order ke target/SL
            cancel-confirm hone chahiye; pending entry/exit ho to duplicate order block hota hai.
          </li>
          <li>Zero qty rows CLOSED hain aur EXIT disabled hai. API error aaye to stale data ko live mat samjho.</li>
        </ul>
      </Section>

      {/* IP fix */}
      <Section title="🌐 'Invalid IP' error — kaise theek karein (zaroori!)">
        <p className="text-xs">
          Dhan order placement ke liye aapke <strong>outbound IP ka whitelisted</strong> hona zaroori
          hai. Agar IP match na ho to order pe{" "}
          <code className="code">DH-905 Invalid IP</code> aata hai.
        </p>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs">
          <li>
            <strong>Settings</strong> tab kholo → <strong>"Static IP"</strong> card dekho.
          </li>
          <li>
            Aapka <em>detected IP</em> aur <em>Dhan par registered IP</em> dikhega. Match nahi ho raha
            to peela warning aayega.
          </li>
          <li>
            <strong>"Register my IP"</strong> button dabao — auto detect karke Dhan par set kar dega.
          </li>
          <li>Phir order dobara try karo.</li>
        </ol>
        <p className="mt-2 text-[11px] text-amber-400">
          Note: IP badal sakta hai (router restart / ISP change) — tab dobara register karo. Dhan web
          (My Profile ▸ Static IP) se bhi set kar sakte ho.
        </p>
        <div className="mt-3 rounded border border-sky-500/30 bg-sky-500/10 p-3 text-[11px] text-sky-200">
          App outbound connections <strong>IPv4 pe pin</strong> karta hai, lekin isse valid token,
          registered static IP ya order acceptance guarantee nahi hoti. Dhan ka actual rejection
          message check karo. DH-901 par token renew karo; DH-905 par IP/input verify karo.
          Sirf getIP match dekhkar order filled ya broker bug assume mat karo.
        </div>
      </Section>

      {/* Advisor */}
      <Section title="🧠 Trade Advisor (guidance)">
        <ul className="list-disc space-y-1 pl-5 text-xs">
          <li><strong>Verdict:</strong> BUY / SELL / WAIT + confidence %.</li>
          <li><strong>Reasons:</strong> trend, RSI, MACD, IV — Hinglish/English me.</li>
          <li><strong>5-yr history:</strong> past me is strike ne kitne % baar profit diya (win-rate).</li>
          <li><strong>Scenarios:</strong> agar spot +1%/-1% hua to option kitna move karega (approx).</li>
          <li><strong>Risk/Reward</strong> tab me slider se profit/loss dekh sakte ho.</li>
        </ul>
        <p className="mt-2 text-xs text-amber-400">
          ⚠️ Ye sirf guidance hai — koi guarantee nahi. Apna decision khud lo.
        </p>
      </Section>

      {/* Market Pulse */}
      <Section title="📰 Market Pulse (news)">
        <ul className="list-disc space-y-1 pl-5 text-xs">
          <li>Free RSS se market news aata hai (ET, Moneycontrol, etc.).</li>
          <li>
            Har <strong>5 minute</strong> auto-refresh hota hai (tab wapas focus karne pe bhi).
            Manual refresh bhi hai.
          </li>
        </ul>
      </Section>

      {/* Tabs */}
      <Section title="📑 Tabs ka matlab">
        <ul className="list-disc space-y-1 pl-5 text-xs">
          <li><strong>Option Chain:</strong> live strikes, Buy/Sell, Trade Signals, Seasonality, Opening Range, News.</li>
          <li><strong>Positions:</strong> live P&L + 1-click EXIT.</li>
          <li><strong>Super Orders:</strong> lagaye gaye super orders (target + SL legs).</li>
          <li><strong>Funds:</strong> account balance.</li>
          <li><strong>EOD Summary:</strong> din-bhar ke order attempts ka summary.</li>
          <li><strong>Settings:</strong> Dry-Run / Fast-Orders switches + Static IP register.</li>
          <li><strong>Readme:</strong> yahi page.</li>
        </ul>
      </Section>

      {/* Safety */}
      <Section title="🛡️ Safety tips">
        <ul className="list-disc space-y-1 pl-5 text-xs">
          <li>Pehle <strong>DRY-RUN</strong> me practice karo, phir LIVE karo.</li>
          <li>Scalping khatam? <strong>Fast Orders OFF</strong> kar do — galti se click na lage.</li>
          <li>Token kisi ko na do — usse asli order lag sakta hai.</li>
          <li>`.env` file kabhi git/GitHub par upload na karo.</li>
          <li>Market band hone ke baad app close kar do.</li>
        </ul>
      </Section>

      <p className="pt-2 text-center text-xs text-slate-600">
        Banne wala app — apne risk par use karo. Trade safe, trade smart. 🙏
      </p>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
      <h3 className="mb-2 text-sm font-bold text-slate-100">{title}</h3>
      {children}
    </div>
  );
}

function Badge({
  label,
  value,
  danger,
  warn,
}: {
  label: string;
  value: string;
  danger?: boolean;
  warn?: boolean;
}) {
  return (
    <div
      className={
        "rounded border px-3 py-2 " +
        (danger
          ? "border-red-700/60 bg-red-950/30"
          : warn
          ? "border-amber-700/50 bg-amber-950/20"
          : "border-slate-800 bg-slate-950/40")
      }
    >
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div
        className={
          "text-xs font-bold " + (danger ? "text-red-300" : warn ? "text-amber-300" : "text-green-300")
        }
      >
        {value}
      </div>
    </div>
  );
}

function LogRow({ name, desc }: { name: string; desc: string }) {
  return (
    <tr className="border-t border-slate-800">
      <td className="px-3 py-2 font-mono text-sky-300">{name}</td>
      <td className="px-3 py-2">{desc}</td>
    </tr>
  );
}
