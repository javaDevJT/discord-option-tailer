# Robinhood Agentic integration

The relay uses Robinhood's Agentic MCP endpoint, `https://agent.robinhood.com/mcp/trading`, through the Python MCP SDK. It binds the account returned by authenticated discovery. No account, credential, balance, or captured brokerage response ships with the project.

## Authorization

Start Robinhood sign-in from **Setup** and complete authorization in your normal browser. The default redirect is `http://127.0.0.1:8766/callback`. On a remote deployment, your browser may show an unreachable localhost page after authorization: copy that complete returned URL into **Returned callback URL** in Setup and select **Finish Robinhood sign-in**. The relay validates the active attempt and completes its internal callback. `RELAY_ROBINHOOD_REDIRECT_URI` can override the redirect when the provider permits your deployment URL. Codex and Robinhood do not use the internal Discord browser. See the [TrueNAS guide](truenas.md) for remote setup.

OAuth uses PKCE. Registration and tokens are stored atomically with owner-only permissions in the persistent data directory. The MCP SDK handles refresh. If renewed authorization is required, the background worker reports assistance rather than opening an unattended login flow. The optional Discord output webhook reports detected credential and connection failures.

Account selection requires authenticated eligibility fields and an unambiguous eligible account. A display name alone does not establish eligibility. Keep the account binding and its mode-specific ledger together when migrating an existing installation.

## Reads and order lifecycle

The normalized adapter exposes `snapshot()`, `quote(contract)`, `review(order)`, `submit(order, before_submit=None)` and `order_status(broker_uuid)`.

1. Revalidate the bound account and fetch equity, spendable funds, positions and pending orders.
2. Resolve the exact underlying, expiry, strike and call/put instrument. Check quote identity, bid/ask, broker timestamp, tick size, currency, multiplier and tradability.
3. Apply source ownership, whole-contract sizing and exposure controls, then verify the originating Discord row again.
4. In Shadow mode, save a proposal. In Live mode, persist order intent and obtain a broker-native review matching the intended account, contract, quantity and price.
5. Recheck the pause switch, source revision, newer messages, quotes, available funds and inventory immediately before submission.
6. Persist the returned broker order identity and reconcile cumulative fills. An uncertain dispatch is not automatically submitted again.

The adapter pins the schemas of its qualified broker tools. A changed schema blocks the affected operation until its compatibility is reviewed. A discovered tool or successful login does not prove that an order will be accepted or filled. Existing account holdings do not automatically become relay-owned positions.

The account and portfolio schemas observed on September 9, 2026 changed only their display guidance about limited-margin features. Both original and reviewed hashes are accepted; input fields, output structure, order schemas and response validation are unchanged. Unreviewed changes still stop the affected operation. The offline regression fixtures contain schema metadata only, without account responses or credentials.

Account equity uses the portfolio total, not only the stock/ETF component. Spendable funds are constrained by reported and unleveraged buying power. Account snapshot age starts at the beginning of its fetch; option quotes use the provider's timestamp. Session gating uses the installed exchange calendar and conservatively stops at 16:00 New York time.

## Modes and allocation

To permit 0DTE entries, enable **Allow same-day (0DTE) entries** in Setup and select **Save permission**. When the relay is running, the button reads **Pause and save** and pauses before saving. Wait for the saved confirmation, then select **Resume relay** when ready. Editing the checkbox does not change the saved permission until you save; the selected mode, credentials and other risk rules remain intact.

When an actionable new entry omits expiry, the interpreter requests the nearest listed expiration. The broker selects 0DTE when the exact standard contract is listed, otherwise the first later expiration at the same symbol, strike and call/put. An explicit text, embed or pictured date is preserved even if unavailable; it never silently rolls to another date. Exits inherit the owned position's expiry. Defaults use the original message's New York date, and earlier-day implicit entries cannot be revived with a newly listed contract. The existing same-day permission gate remains authoritative.

Current-message and directly replied-to Discord attachment pictures are passed to Codex through native image input using the existing subscription. Supported PNG/JPEG/WebP images are fetched only from approved Discord CDN attachment paths, bounded in count and size, and kept in private temporary files. Signed URLs never enter model text. Unreadable or unsupported pictures stop interpretation; filenames cannot supply an expiration. Image-only alerts without an accompanying textual action remain held.

- **Paper:** explicit local quote fixtures and simulated fills.
- **Shadow:** authenticated account/quote reads and saved proposals, without order submission.
- **Live:** real orders only when both the configured mode and live-order gate are enabled.

Each mode has a separate ledger. Fresh Docker installations begin in Shadow with no account binding and live orders disabled. Use the frontend's pause and mode controls to change an existing installation.

The default allocation reference increases from 5% of equity at 0.80 interpretation confidence to 10% at 1.00 confidence. Existing exposure, available funds and estimated costs can reduce that budget. The default same-underlying sizing reference is 10%, total option exposure reference is 20%, and local cash reserve is zero. If the percentage budget cannot cover one whole contract, an otherwise eligible entry uses exactly one when available buying power after the configured cash reserve covers its rounded limit cost and fees. The percentage targets may be exceeded by that one-contract fallback; buying power cannot.

Above the one-contract fallback, quantity is the whole-contract floor of the reference budget, including the configured per-contract fee reserve. Optional calibrated quarter-Kelly sizing can reduce the reference; a nonpositive calibrated edge still blocks entry. Interpretation confidence is not a measured probability of profitable trading. There is no fixed contract-count or daily entry-count limit.

Missed-signal recovery produces timestamped assessments only. It does not bypass the fresh-message execution gate or submit historical orders. See [container operation](container.md) for recovery and monitoring behavior.

## Optional command-line inspection

The frontend performs normal setup. For local diagnostics against a deliberately configured private account:

```sh
python -m relay broker-login
python -m relay broker-discover
python -m relay broker-inspect
```

`broker-inspect --bind` writes a private Shadow configuration for an unambiguously selected eligible account. It refuses to bind an account into `config.example.json`. These commands may create sensitive local reports; keep them outside Git. The public regression suite uses synthetic responses and sends no brokerage orders.
