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
          nahi. Restart pe <code>.env</code> ke default par wapas aa jate hain.
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

      {/* Positions / EXIT */}
      <Section title="🚪 Open Positions + 1-click EXIT">
        <ul className="list-disc space-y-1 pl-5 text-xs">
          <li>
            <strong>Positions</strong> tab me har position ka <strong>live P&L</strong> dikhta hai
            (auto-refresh ~7s).
          </li>
          <li>
            <strong>EXIT</strong> button dabao → turant <strong>MARKET</strong> order se position
            square-off ho jayegi (on the spot).
          </li>
          <li>P&L auto-refresh hote rehta hai bina rate-limit (429) trigger kiye.</li>
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
          <strong>Ye bug theek ho gaya ✅</strong> — asli problem ye thi ki aapka net{" "}
          <strong>IPv4 + IPv6 dono</strong> use karta hai, aur app kabhi IPv6 se Dhan ko call kar deta
          tha, jisse <em>ipMatchStatus: MISMATCH</em> aata tha (chahe IPv4 register ho). Ab app{" "}
          <strong>hamesha IPv4 se hi baat karta hai</strong>, isliye registered IP se match ho jata
          hai aur <em>ordersAllowed: true</em> milta hai. Kuch karne ki zaroorat nahi — apne aap sahi
          ho gaya.
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
          <li><strong>Option Chain:</strong> live strikes, Buy/Sell, Trade Signals, Seasonality, News.</li>
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
