# Security Guide

This app **places real orders with real money**. Treat it accordingly.

## Golden rules

1. **Dhan secrets never touch the browser.**
   `DHAN_ACCESS_TOKEN`, `DHAN_CLIENT_ID`, and the order confirm PIN live only in
   `backend/.env`. The browser only ever holds an opaque session cookie.

2. **`.env` is git-ignored.** Never commit it.
   ```bash
   git check-ignore backend/.env   # must print the path
   ```

3. **Static IP.** Order APIs require a whitelisted static IP.
   - Your home Airtel fiber IP can change → orders may fail with `DH-903`.
   - Set the IP from the **Connection** panel in the UI, or on web.dhan.co.
   - Keep **Dry-run mode ON** until the app reports a healthy order path.

4. **Dry-run mode.** When enabled, orders are validated + logged but NOT sent
   to Dhan. Start here. Turn it off only when you intend to trade live.

5. **Order confirm PIN.** Server-side gate on every order/super-order/cancel.
   Stored in plaintext in the protected backend `.env`; restrict file access
   and never share it. Required in the confirm popup unless fast orders are on.

6. **Session auth.** All state-changing API calls require a valid session cookie
   (`HttpOnly`, `SameSite=Strict`, `Secure` when served over HTTPS).

7. **CSRF protection.** Every non-GET request must carry the `X-CSRF-Token`
   header matching the session token. The frontend injects it automatically.

8. **Rate limiting.** Backend throttles Dhan calls per its limits
   (Option Chain 1 req / 3s / underlying; Orders 10/s) and returns friendly
   errors instead of getting your account temporarily blocked (`DH-904`).

9. **Input validation.** All request bodies are validated with Pydantic models.
   No raw passthrough to Dhan. Prices/quantity are bounded and sanity-checked.

10. **Audit log.** Every order attempt (dry-run or live) is written to a local
    SQLite DB with timestamp, params, and result — for your EOD summary.

## Network hardening (local deployment)

- Bind backend to `127.0.0.1` by default so only your machine can reach it.
- If you must expose it on LAN, enable the order PIN and set `APP_ALLOWED_ORIGINS`
  to an explicit origin (never `*`).
- Serve over HTTPS if you ever expose it beyond localhost (cookie `Secure`).
- The current login validates the backend's Dhan token, not the identity of a
  website visitor. Do not expose it publicly without a separate identity gate.
  For a single-user AWS installation, prefer private access (such as SSM port
  forwarding) and no public inbound app ports.

## Optional Telegram notifications

- Keep `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the ignored backend `.env`
  or an access-controlled cloud secret, never in frontend configuration or chat.
- Use a private destination. Give a channel bot only the required posting rights.
- Application alerts contain fixed operational labels, timestamps and safe
  account/mode aliases; never raw logs, credentials, order payloads or trading
  financial details. The optional AWS daily usage report intentionally sends
  estimated account-wide AWS costs to the configured private Telegram channel.
  Enable it only when that disclosure is intended.
- Telegram delivery is best-effort and is not a durable trading audit or a risk
  control. The backend must work independently of Telegram availability.
- Monitor host/process failures externally: a stopped server cannot notify you.
- No Telegram commands can place orders or enable LIVE/ARM.

## What this app deliberately does NOT do

- It does not store your Dhan password or TOTP secret.
- It does not enable new algo entries on startup. Explicit operator ARM is
  required; an armed algo can place background orders, and restored positions
  continue to be managed even while new entries are disarmed.
- It does not support multiple active backend replicas. Account execution locks
  and broker rate budgets are in-process; run one active trading backend.
