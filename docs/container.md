# Container setup and dashboard

This container packages the relay worker, a visible Chromium desktop for Discord, the Codex CLI, Robinhood OAuth support, an authenticated monitoring dashboard, and an optional notification worker. The Compose deployment keeps configuration, ledgers, browser state, and provider login state in one persistent Docker volume.

> [!WARNING]
> A fresh volume starts in Shadow mode with live submissions disabled. Live can submit real Robinhood orders, so use the dashboard's pause and confirmation flow only after checking the current account, channels, quotes, and ledger.

## Prerequisites

- Docker Engine or Docker Desktop with Compose.
- A browser for the Discord, Codex, and Robinhood sign-in pages.
- A Discord account and channels you are authorized to read, a ChatGPT subscription that can use Codex, and a Robinhood account eligible for the broker integration.
- A private `.env` file. Do not commit it or replace an existing one without preserving its settings.

No OpenAI API key is needed. Discord input uses the personal account through Gateway or Browser mode; Robinhood setup uses normal browser OAuth.

## Start the container

From the repository directory, create `.env` from the example only when it does not already exist, set a dashboard password, and build from source:

```sh
test -f .env || cp .env.example .env
# Edit .env and set DASHBOARD_PASSWORD.
docker compose config
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8787/healthz
```

Open `http://localhost:8787` and sign in with the dashboard user and password from `.env`. The default HTTP bind is loopback. The health endpoint is a liveness check; the dashboard supplies the setup and runtime state.

### Verify a fresh bootstrap

With a new Docker volume, the dashboard should show **Setup required**, **Shadow** mode, an unbound Robinhood account, and live submissions disabled. The service creates the missing configuration and private state on first start. It does not import host credentials or local browser profiles. If the volume already exists, its configuration and ledger are preserved.

## Configure the integrations in Setup

All provider setup is available from the authenticated **Setup** section. No `docker exec` commands are required.

### Discord input

1. Choose **Gateway (discord.py-self)** in **Discord input**, enter your personal-account token in the password field, and save. The credential is stored privately with owner-only permissions and is never returned by the API. An empty field preserves it. For **Browser** mode, open **Browser login** and complete manual sign-in and any verification in the embedded Chromium desktop.
2. Once connected, select **Refresh servers**, choose a server, and choose a channel for each of the two rows. Gateway uses its cached directory; Browser uses the rendered sidebar.
3. Set each row to `Signals` when it may supply actionable alerts, or `Context` when it should provide context only. Author restrictions are optional; an unchecked restriction accepts every author in that channel.
4. Select **Save channels**. Browser polling uses a base interval of 2–60 seconds (3 seconds by default); Gateway receives live events without message polling.

The advanced channel URL field is a fallback when a channel is not visible in the current directory. The worker requires exactly two distinct channel bindings. Neither reader extracts credentials or posts to the input channels.

Fresh Docker volumes select Gateway. Existing volumes and configurations without a transport keep Browser mode, so an upgrade preserves the saved session. Switch an existing installation explicitly in Setup. Revoked Gateway credentials require a replacement in Setup; the service waits for correction instead of repeatedly retrying an invalid token. It cannot refresh a revoked user token automatically.

Gateway uses `discord.py-self==2.1.0`, keeps subscriptions needed for messages, and disables bulk startup member chunking. History is limited to one page of at most 100 messages per configured channel, fetched sequentially for context. Discovery reads cached servers first, then all readable channels for the selected server. Available author suggestions may be incomplete, and author restrictions remain optional.

On connection, the worker briefly waits for channel-cache hydration, then resolves only the two saved channel IDs through the authenticated SDK if needed. It validates the returned channel ID, guild and message-channel type, then verifies access through bounded history reads. Cached role/member permissions are advisory because an incomplete cache can falsely deny access. Each channel bootstraps independently: an unavailable channel stays blocked while a readable sibling keeps monitoring and can authorize its own actions. HTTP lookup is capped at three attempts per channel per connection, with provider rate-limit delays preserved. Recovery of one channel preserves the other channel's baseline and connection epoch, so it neither replays old entries nor demotes current messages to history. A current gateway heartbeat acknowledgement and successful access confirmation for that message's channel are still required before execution.

Partial monitoring appears as `Monitoring 1/2 configured channels`, with individual channel states in the dashboard and `Partial` in Setup. A saved but unavailable channel does not imply that the Discord session needs reauthentication. Recovery checks operate only on unready channels; an actual disconnect or authentication failure still invalidates the whole connection.

When context history is set to zero, a single-message read probe still checks channel access; its result is discarded. A rate-limit response stops the entire resolution pass, including requests for the other channel, until the provider's delay has passed. Verification challenges require manual assistance and are never solved or retried automatically.

Discord status names the failing channel, operation phase and a diagnostic code: `channel_permission_denied` means an authoritative Discord lookup/read returned HTTP 403 (not an expired token), `channel_not_found` means the saved channel is unavailable, and `channel_auth_required` means the session needs reauthentication. The `lookup` or `history` phase and HTTP status remain visible during retries, so a cache estimate cannot be confused with a refused read. Guild/ID mismatches and unsupported channel types remain blocked. Relevant channel/role access changes can trigger a bounded recheck of a denied channel; its creation event can clear a stale missing-channel error. Otherwise these errors wait for corrected configuration or a new session. These events cannot shorten a provider Retry-After deadline. Transient lookups retry within the fixed limit. The diagnostic includes no token or provider response body. The configured channels and optional author restrictions are preserved.

Application-controlled Discord polling, browser recovery, discovery waits, and history pacing use random delays between 80% and 120% of their base interval. Library reconnect backoff stays under the library's control. Protocol heartbeats, provider rate-limit minimums, navigation deadlines, and trading freshness windows are unchanged. Jitter and the library do not guarantee avoiding Discord restrictions; this implementation adds no proxies, fingerprint spoofing, or CAPTCHA solving.

### Codex subscription

Select **Start Codex sign-in**, open the displayed device verification link in your own browser, and enter the displayed code. The code belongs to that login attempt and may expire. Codex runs through the existing ChatGPT subscription; the container does not use an OpenAI API key. The subscription's usage limits still apply.

### JEV fast evaluation

In **Setup → Signal evaluation**, **Direct entries** enables local parsing of supported complete ENTRY and OPEN alerts before either model. It defaults on and can be disabled in the frontend. Select the fallback evaluator separately: Codex, JEV with Codex fallback, or JEV shadow comparison. Shadow comparison always keeps Codex authoritative. Complete direct entries need neither model credentials nor a model request; keep fallback credentials valid for ambiguous messages.

For JEV, paste a TypeSafe API key into the write-only field and save; blank preserves the existing key. Private runtime storage holds the credential, never configuration JSON or browser responses. Alternatively set `TYPESAFE_API_KEY` or `TYPESAFE_AI_API_KEY`. The synthetic connection test performs no trade or channel-message request. JEV uses `jev-latest`, thresholds of 0.95 native confidence, 0.95 selected probability, and 0.98 eligibility, with the configured deadline capped at 700 ms for entry candidates and 1,200 ms otherwise. Timeout, uncertain intent, or missing facts fall back to subscription-backed Codex.

Literal entries must contain one complete contract and one premium, with no unparsed trading instruction or qualifier. Omitted expiry uses the existing source-date 0DTE/next-listed rule; missing facts that may be pictured require Codex. Unrelated historical images do not block an independent complete entry. Adding to holdings, compound actions, portfolio recaps, risk/sizing qualifiers, and uncertain references do not bypass interpretation. Exact route boundaries are documented in the [evaluation design](jev-evaluation-design.md).

The dashboard identifies Rule evaluation, JEV, and Codex separately. It records interpretation readiness, queue/execution stages, and receipt-to-submission only when the final broker placement boundary is reached; absent measurements remain blank. Fastest arrival requires the event-driven Gateway transport. Browser mode polls every three seconds by default and cannot provide subsecond arrival. Broker reads, review, acceptance, and fills remain external latency; a local parsing benchmark is not a live execution guarantee. Saved transports and credentials are preserved when changing evaluation settings.

### Robinhood connection

Select **Start Robinhood sign-in**, open the displayed authorization link in your normal browser, and approve the OAuth request. The default callback uses localhost port `8766`. With a remote Docker host, the returned page may show a connection error: copy the complete address from that tab, paste it into **Returned callback URL** in Setup, and select **Finish Robinhood sign-in**. Complete this within the five-minute sign-in window. Setup validates the callback against the active attempt and passes it to the existing state/PKCE flow; the pasted URL is not saved. After authorization, Setup performs account inspection and saves the selected account binding. Choose an explicit account only when more than one eligible account requires a choice.

`RELAY_ROBINHOOD_REDIRECT_URI` can override the callback with an HTTP(S) address ending in `/callback`. The bundled proxy supports callbacks on the dashboard port, but public registration accepting an address does not prove Robinhood will accept it during authorization. A Robinhood-hosted connection-error page occurs before a callback and cannot be completed by pasting that error-page address.

Authentication, account inspection, and order execution are separate stages. Setup does not review, place, cancel, or claim a fill for an order. Keep the relay in Shadow while verifying the connection.

### Discord output notifications

**Setup → Discord output webhook** is an optional output destination. Paste a valid Discord webhook URL, enable notifications, and save. The URL is stored privately and stays hidden after saving; disabling notifications keeps the saved URL, while removing it clears the destination.

Notifications can report authentication assistance and relay actions with a Paper, Shadow, or Live label. They are separate from the personal Discord account used as input. Message bodies, account identifiers, OAuth codes, and credentials are omitted, and no test post is sent by setup.

## Choose Shadow, Paper, or Live

### Shadow

Shadow is the safe Docker default. It can use an authenticated account and current quotes to create proposed orders in the Shadow ledger, but it never submits broker orders and does not create simulated fills.

### Paper

Paper is an offline mode for synthetic accounts and quote fixtures. It is selected in the configuration used by the CLI and is exercised by the local demo and fixture tests. Paper fills are deterministic crossing fills; they do not model exchange queues, liquidity, or real slippage.

### Live

Live is available only through an explicit frontend confirmation:

1. Select **Pause relay**.
2. Ensure both channels are saved and Discord, Codex, and Robinhood report connected.
3. Select **Enable Live**, review the warning, and confirm.
4. Wait for the worker to acknowledge Live.
5. Select **Resume relay**.

The worker requires `live` mode and the explicit live-order flag together with its normal source, freshness, quote, account, sizing, review, and kill-switch checks. Every mode has a separate ledger. Open relay positions or unresolved orders can prevent switching away from Live. Pause stops new execution; it does not cancel a broker order or close a position. Reconnect preserves the ledger and browser state.

## Fresh processing and missed exits

After the first login, restart, or reconnect, the observed channel history becomes a context baseline. Baseline rows are recorded for context and are never replayed as fresh entries. New dispatch still requires an eligible `Signals` row, an authorized author, an exact contract, and current source and market data.

Eligible baseline alerts and fresh alerts held because they became stale may be queued for assessment after the reader returns to the newest messages. Startup also checks saved context since each current relay-owned entry, including older ignored/held rows. Recovery compares the original alert with later same-source context and current account and quote facts. Entries remain review-only. A viable missed reduce/close can use the ordinary Paper, Shadow or Live path after current-source verification, position-lifetime and duplicate checks. The dashboard distinguishes catch-up orders from review results.

Recovery has no age or entry-count cutoff for assessment, but is bounded by observed/saved messages and per-revision deduplication. Dispatch needs the exact source revision in the healthy current gateway cache or visible bottom-of-channel browser snapshot, with no newer unseen context. It does not fetch every missed message or reconstruct historical prices. Edited rows, manual backscroll, imported history, future timestamps, unauthorized authors and context-only channels are excluded. Source-readiness holds retry after 30 seconds without repeating the model while context/positions remain unchanged; account/market fact holds retry assessment after five minutes. Submitted/unknown outcomes are reconciled without resubmitting. Canceled/rejected orders require review and are not automatically replaced.

Exit fractions use ceiling arithmetic: half of one sells one, half of two sells one, and half of three sells two. Full-exit language closes the remaining relay-owned quantity. An optional unquantified profit-taking suggestion defaults to half remaining, rounded up. Optional exits require positive estimated net profit at the lower of the current tick-rounded bid and the order limit, after recorded entry cost and a round-trip fee reserve of twice the configured per-contract reserve. The final broker review repeats that check. Explicit exits remain valid even after profit fades. Audit reasons show sale quantity, evaluated price and estimated net profit; a submitted limit order is not a guarantee of a fill.

## Risk guardrails

The default configuration uses a confidence-based sizing reference between 5% and 10% of current equity:

| Guardrail | Default |
| --- | --- |
| Minimum parse confidence | `0.80` |
| Entry sizing reference | `5%` at the threshold, `7.5%` at `0.90`, `10%` at `1.00` |
| Same-underlying sizing reference | `10%` of equity |
| Total option exposure sizing reference | `20%` of equity |
| Signal freshness | `90` seconds |
| Quote freshness | `15` seconds |
| Maximum spread | `15%` |
| Maximum chase | `10%` above the cited premium |
| Same-day expiry opens | Disabled by default; configurable in Setup |
| Fee reserve | `$1.00` per contract |

These percentage allocations determine quantity rather than a minimum account balance. When the reference budget cannot cover one whole contract, an otherwise eligible entry uses exactly one contract if available buying power after any configured cash reserve covers the rounded limit price times 100 plus fees. For example, a $35.32 reference budget and a $144 contract cost yield one contract when at least $144 is available. When the reference budget supports multiple contracts, normal whole-contract sizing applies. Existing and pending exposure still reduce that reference budget. There is no fixed-dollar cap, fixed contract-count cap, daily entry-count limit, or daily gross-entry limit. Optional calibrated quarter-Kelly statistics can reduce the reference budget; a nonpositive calibrated edge still blocks entry. Other eligibility, chase and broker checks remain required.

The execution path accepts single-leg long standard USD equity or ETF options for buy-to-open, reduce, sell-to-close, and native sell-stop actions. It requires an exact symbol, absolute expiry, strike, and call or put. Futures, crypto, short positions, spreads, conditional scheduling, averaging in are held. Missing entry expirations default to 0DTE or the nearest listed expiration at the exact strike/type; dates stated in text, embeds or supplied pictures take precedence. The original message's New York date anchors resolution, and old alerts cannot roll forward. Same-day entry permissions remain in effect.

Model, reasoning, Fast/Standard service, and chase can be saved in **Setup → Signal evaluation**. Saving pauses the relay and waits for the worker to load the configuration. The example uses `gpt-6-astra`, medium reasoning, Fast, and 10% chase on the existing ChatGPT subscription.

Evaluation retries once for transient failures and invalid structured output or evidence; auth, quota, and configuration failures are not retried. Broker submissions are never retried by this mechanism. Failed evaluations include sanitized diagnostics in Messages and Audit. Codex or Robinhood authentication rejection updates Setup and triggers the configured Discord webhook once per incident. Reauthentication failures remain visible across restarts until the affected provider succeeds.

## Monitor and operate

### Codex position monitoring

Codex can attach a `monitor` plan to a `WAIT` decision for a specific, currently owned position from the same Discord source. It chooses a duration (30 seconds to seven days), a polling interval (5–300 seconds), and either a timed reassessment or price/return/quantity conditions. Conditions can watch option bid/ask, underlying price, unrealized return, or held quantity. Any matching condition or elapsed reassessment timer wakes Codex with current broker observations and source context; the trigger is not itself a sell instruction.

Plans and claimed evaluations are saved in SQLite and resumed after restart. Monitoring ends at the deadline, when the entry is no longer owned, or when a newer plan supersedes it. Codex can explicitly continue monitoring within the existing deadline, finish without action, or reduce/close the owned position through the normal execution and source-verification checks. Monitor callbacks cannot open positions. Polling and model evaluation run outside the entry execution lock and yield to fresh messages.

The dashboard's position-monitor list shows the plan, deadline, observations, triggers, decisions, and diagnostic failures. These targeted reads are separate from the ordinary ten-minute account overview. Polling and Codex reassessment require the app and provider credentials to remain available; this is not a broker-native protective order.

Relative dates remain tied to the original Discord message. The current Robinhood tool schema has no underlying daily-low/high or historical-bars fields. A request such as "under today's low" therefore needs a grounded numeric level from source context; unavailable references are reported explicitly, never replaced by the option low or a different day's price. A timed reassessment can continue while that reference is unresolved, but unresolved conditions do not authorize an exit.

### Agent-directed stop losses

Codex interprets stop instructions; stop-bearing messages bypass JEV and literal entry parsing. A partial exit plus a breakeven instruction produces one `REDUCE` decision with `stop_price="breakeven"`. A standalone stop change produces `UPDATE_STOP`. Numeric stops refer to the option premium; an underlying stock-price level is not treated as an option-premium stop.

The contract must match an exact current relay-owned position from the same source. Breakeven uses the relay's actual average entry premium, rounded upward to a valid price tick. Half of two contracts sells one and protects the remaining one; half of one sells that contract and leaves no remainder. A held entry does not create an owned position.

Protection uses broker-reviewed native stop-market sell-to-close orders, good until canceled. If the requested threshold is already reached when placing protection, the remaining contracts are sold with a reviewed market order during the open options session. A stop trigger is not a guaranteed fill price or a guarantee of profit after fees. Confirmed native stops remain with the broker when the relay is offline.

Later trims, full exits and expiry liquidation cancel the existing relay-owned stop and confirm its terminal status before placing a competing sale. A fill racing cancellation updates inventory first. The desired follow-up stop is persisted with the trim order, then armed for the actual remainder after the trim is reconciled. Pending trims or cancellations can leave a gap before replacement protection is active; the timeline reports pending, active, blocked or completed status. Uncertain submissions are never automatically submitted twice.

Restart reconciliation resumes accepted protection requests and observes existing broker stops. A historical standalone stop instruction that was never accepted remains review-only; recovered compound exits still require the existing source and position checks. Pause prevents new changes but does not cancel a confirmed native stop. Shadow only records a proposal.

### Expiry exercise protection

While the worker runs, expiry monitoring checks every 30 seconds during the final hour of the XNYS regular session, including calendar early closes. It manages only identifiable relay-owned standard equity/ETF options expiring that New York date. A fresh Robinhood underlying trade above the strike makes a call in the money; below the strike makes a put in the money. This is independent of the option trade's profit or loss. At-the-money and out-of-the-money holdings remain under observation.

An eligible in-the-money position closes all remaining owned contracts through the existing account, inventory, quote, review, and submission checks. The limit is the current option bid rounded down to its tick; the entry spread limit and profit-only test do not block expiry liquidation. Live enablement, pause/kill switch, and observe-only controls still apply. No Discord message or new model evaluation is needed. The dashboard decision trail identifies internal expiry exercise protection events, and configured webhook notifications identify expiry actions and blocked/unfilled risk.

Order IDs and cumulative fills remain in the durable ledger. Restarting cannot duplicate an accepted or uncertain expiry sale. Existing pending orders are reconciled; partial fills, cancellations, broker rejections, and unknown results with remaining inventory require broker attention instead of blind replacement. A final dispatch hold that proves transport never began may retry with a new audited attempt. The monitor never cancels orders, requests exercise, or submits do-not-exercise instructions.

This is best-effort sale protection, not a guarantee against exercise. The worker must be running and authorized (its existing startup readiness requires Discord, Codex, and Robinhood setup), the order must fill, and an out-of-the-money option can cross its strike late or after regular trading. Remaining inventory after close raises an assistance alert. If preventing any possible exercise is the overriding requirement, separately arrange broker-supported do-not-exercise instructions or adopt an earlier exit for every expiring option; neither is silently enabled by this ITM-only rule.

### Exit intent and profit-taking

Current exit intent is required before reducing a position. A gain report, celebration, unreached target, or approaching market close is context even when it uniquely identifies a profitable relay-owned contract. For example, “DRAM calls up $50 per contract heading into market close; looking for $65 as my first target” yields WAIT with no sale. The shared decision validator also withholds a proposed exit when its current message is a profit/target update without textual exit intent; a model's confidence or identification of the contract cannot override that check. Restart recovery revalidates saved decisions and invalidates status-only exits before requesting a model assessment.

Optional suggestions such as “you can trim or take profits if you'd like” and non-imperative profit-taking such as “locking in this win” remain supported. Current sale reports such as “took 50% here” and “sold the rest” retain their stated meaning, including when the same message describes future targets for the remainder. An unspecified current partial trim still defaults to half the remaining contracts, rounded up: one of one, one of two, two of three. Explicit full exits, native stops, and expiry protection keep their existing behavior.

Explicit current partial instructions such as “Trim contracts here” use the same half-position default when no count or percentage is stated. Explicit directives retain `profit_only=false`; optional suggestions retain the positive net-profit guard. The default applies during normal execution and restart recovery, and invalid stated sizing is held rather than replaced with a default.

Gateway status checks the library's heartbeat acknowledgements locally every 16–24 seconds, even when neither channel has new messages. The detail shows the acknowledgement age. Missing or stale acknowledgements report reconnection and block live source verification; a process heartbeat alone is not proof that Discord is connected. This check does not send extra Discord requests or change protocol heartbeat timing.

Discord's initial navigation has a 60-second network timeout. A slow page keeps the browser open and reports loading or recovery guidance; completing manual sign-in or MFA has no time limit. Use **Back to setup** to leave the browser view without ending the saved session. If the page stays blank, reload it in the embedded browser; use **Reconnect** if the browser worker has stopped.

While a Discord tab shows sign-in, MFA, or CAPTCHA, automatic channel navigation and discovery pause. Loading message lists wait without repeated reloads, and discovery keeps verification tabs open after a request fails. Finish verification in **Browser login**; close any extra sign-in tabs you no longer need. Monitoring resumes with existing messages treated as context. Avoid **Reconnect** or changing channels during verification, since those deliberately restart the browser worker.

The relay also waits for Discord's signed-in sidebar to render: a `/channels/` URL alone does not prove login finished. During manual login it passively records safe diagnostics from failed Discord login requests, such as HTTP status, a numeric Discord error code, CAPTCHA requested/rejected, rate limiting, or a network failure. Setup and Overview retain the last diagnostic through redirects until the signed-in interface is confirmed. These observations do not submit requests, solve challenges, or store credentials or raw response bodies. If verification returns to the QR page, use the displayed code to distinguish a provider response from a loading or network problem.

Setup and the Overview connection cards display provider diagnostics. Errors identify the operation, a stable failure code, and a suggested next step; authentication failures can also include the phase, HTTP status, exception type, and application source location. DNS, certificate, connection, timeout, missing executable, and local permission failures have distinct guidance. Provider response bodies, tokens, and callback codes are withheld. Saved credentials alone do not override a reported runtime failure.

Codex device login has a 15-minute local process deadline, and its device code can expire sooner. Robinhood waits five minutes for its OAuth callback. An expired attempt requires a new sign-in; these deadlines are separate from Discord's unlimited manual login wait.

Expand a message's **Images** section to inspect its attachments. Previews are served through the authenticated dashboard and do not invoke Codex or Robinhood. If a preview fails, its link shows a specific, sanitized error and lets you retry. Discord refreshes signed attachment URLs; the reader retains rendered proxy alternatives and saves URL renewals without issuing another signal evaluation.

The interpreter supplies current-message and reply images to Codex. If an entry cites an older original alert whose pictures were not supplied, it adds those pictures for one second evaluation. Transient image timeouts, network errors, rate limits, and server failures can also use that retry; the total remains two attempts. Missing, inaccessible, unsupported, or oversized pictures produce a specific image error and block the entry. Each evaluation retains the four-image and 16 MiB download limits. Old failed decisions are not automatically replayed by this update.

The dashboard provides an operational snapshot, runtime status for Discord/Codex/Robinhood, observed messages, interpretation states, held decisions, recorded orders, relay-owned positions, and the decision trail. Relay-owned positions represent what this ledger recorded; they are not a complete broker account statement.

Use the dashboard controls for **Pause relay**, **Resume relay**, and **Reconnect**. For container-level checks and logs:

```sh
docker compose ps
docker compose logs --tail=100 relay
docker compose stop
docker compose start
```

Pausing and stopping are safe operational controls for new work. Preserve the Docker volume when restarting or moving the service; it contains the browser profile, Codex subscription state, Robinhood OAuth state, configuration, and mode-specific ledgers.

### Discord updates and message history

Discord can publish `MESSAGE_UPDATE` for an unchanged embed or a renewed attachment URL. The gateway compares normalized content before inventing an edit timestamp or invalidating a pending decision. An unchanged update preserves the original live/baseline eligibility, source revision and existing invalidation state; refreshed media URLs can be retained without creating a second signal. Changed text, embed contents, attachment identity or an explicit edit timestamp still invalidate the old revision. This adds no debounce delay to fresh entries.

If startup or reconnect history restores a previously seen revision, the message snapshot and source generation still refresh, but the existing decision is preserved and the alert is not evaluated or executed again.

Messages show their gateway/browser/import provenance and whether they arrived live, as history or as edits. Context results name the actual gate. When an edited revision replaces a message that was already evaluated, the card also shows the prior action, hold reason and timing. An edited message showing context therefore does not hide its earlier evaluation.

### Execution failure diagnostics

Held and failed actions retain an `execution_diagnostic` on the message decision, visible in the dashboard's expandable execution details and emitted to the worker log. Each diagnostic has a correlation ID, failing stage and named check, exception class, safe error code, broker operation/tool when known, up to sixteen internal source locations, and eight chained causes. Safe observed values include quote prices, account capacity, source/revision comparisons, and message/order identifiers. Unexpected failures before interpretation produces a decision are recorded too.

Every dispatch guard records its pass/fail result, timestamp, duration, and applicable values in `dispatch_checks`. The record distinguishes checks before broker review, after review, and after source verification. Final preflight failures retain the underlying exception instead of only the generic wrapper. A rejected local order is labeled **Not submitted** when the broker trace explicitly proves placement never started. Broker rejection and uncertain submission remain separate outcomes.

The worker also writes structured records to `/data/state/execution-diagnostics.log`, retaining five rotated 10 MiB files plus the active file (up to approximately 60 MiB). Files have owner-only permissions and survive container replacement on the existing `/data` mount. Release images include their Git revision in diagnostics, so retained source locations can be matched to the correct build. Successful log writes are not duplicated into container logs; a disk-write failure falls back to the console. Message decisions and their checks also remain in SQLite across restart. Snapshot/review/placement, reconciliation, expiry-session, discovery, and worker-restart failures use the same safe diagnostic format. Logging adds no broker/Discord requests and never authorizes a retry.

When a later order-status lookup fails, the exact originating message revision displays **Last reconciliation failure**. That diagnostic does not replace the original decision or change the order's lifecycle status. Cancellation failures are logged before the existing final-status lookup, retaining evidence even when a fill wins the cancellation race.

For example, `stage=quote; exception=BrokerError; code=broker_error; tool=get_option_quotes` identifies a quote failure before submission. A `submission` or `result_recording` failure can leave an uncertain order: reconciliation still blocks new orders until its outcome is known. Diagnostics never retry or replay a trade. Expiry protection, missed-signal recovery, and native stop failures also report safe stage details; stop details are retained in their reason.

When reporting a failure, include the diagnostic ID, message ID, decision JSON, and deployed revision. Timing alone does not prove an operation succeeded: durations are recorded even when it raises. Older events retain their original evidence; the new details begin with this release.

Diagnostics exclude exception messages, provider responses, credentials, absolute paths, source text, and local variables. Only bounded, allowlisted metadata and internal `relay/file.py:line:function` locations are exposed.

### Active watch polling

A fresh, authorized on-watch notice starts a ten-minute polling window anchored to the Discord post time. Delayed or duplicate delivery does not restart that window. The relay rotates through distinct prepared contracts, starting one market refresh about every three seconds after the preceding refresh finishes. Each refresh has a ten-second timeout so one slow contract cannot monopolize the rotation. The account snapshot is shared across these watches and refreshed when due, rather than fetched once per contract. More watches, broker latency, or a request-budget wait can reduce the per-contract frequency.

Background metadata and market reads share a broker request budget with execution. The local ceilings are 90 calls per rolling minute in total and 60 background calls per rolling minute, leaving capacity for execution and other foreground reads under the supplied 100/minute upstream limit. The relay does not treat the advertised burst allowance as a sustained polling rate. Broker 429 responses trigger a shared cooldown with jitter; mutation requests are never blindly retried. Other applications using the same account can still consume upstream capacity.

Robinhood's published 100/minute and 300-burst figures are documented for its Crypto Trading API. An identical Agentic MCP quota has not been independently confirmed; the local limits are conservative operating ceilings, not a claim about that endpoint's entitlement. Server throttling always takes precedence.

Only a deterministic OPEN with a current matching watch may use the warmed account snapshot. The handoff expires after ten seconds measured from the read start, validates quote timestamps, and is invalidated by account/order changes. Missing, expired, or invalid data causes a normal fresh read. Source verification, order review, chase, quantity, and risk checks remain in force. This polling does not authorize an entry, alter the ten-minute dashboard balance refresh, or guarantee a fill.

The entry's `entry_preparation.market_refresh` and `watch_market_data` diagnostics record refresh outcomes and cache use/fallback. Contract metadata keeps its existing slower refresh after the hot window ends. App restart does not replay old watch notices into a new hot window.

### Entry price diagnostics

Entry diagnostics show the evaluated ask, cited alert premium, signed percentage deviation, configured chase cap, and rounded order limit. Chase uses `(price / alert premium - 1) × 100`: at a 15% setting, a $1.00 alert permits up to $1.15, including the boundary. Both the ask and the limit after tick rounding must fit the cap, and the check runs again on the final broker-review quote. Exits are not subject to entry chase limits.

### Message evaluation timing

Message and decision rows show **Model evaluation** and **Posted → decision**. Model time uses a monotonic clock and includes preparation, image handling, retries, and validation. Recovery totals the initial interpretation and the current-viability evaluation. Posting-to-decision uses the original Discord timestamp through the recorded outcome, including time offline or waiting, policy checks, and any broker response. It is not order-fill latency. Retry counts and recovered/delayed labels provide context; a long recovery interval does not mean the model spent that entire time running. Historical rows without measurements say **not recorded**; missing or future source timestamps cannot produce a trustworthy reaction interval.

### Account balances and holdings

The account panel shows Robinhood's total account value, cash, reported buying power, cash-only buying power, asset-class totals, and account option positions. The reviewed Agentic API exposes detailed option holdings; other asset types appear as portfolio totals. These holdings are separate from positions owned by the relay's ledger.

The connected worker refreshes this display on its first uncached read, every ten minutes thereafter, and after an order submission attempt or an observed order-status change. Dashboard polling reads only the persisted cache. Restarting the worker retains the ten-minute schedule. Pausing new trading does not stop account refreshes; a disconnected or stopped worker leaves the last cached values visible with their age.

Failed refreshes retain the last successful values and show an error. Option marks may be from the previous market session; their quote times remain visible. This display cache never supplies execution-time balance, inventory, or quote checks.

## Network and security boundary

The dashboard is published on port `8787` and the Robinhood callback on port `8766`; both bind to host loopback by default. HTTP Basic authentication protects the dashboard and browser desktop. If you intentionally expose the service through a private network or reverse proxy, preserve the original Host and browser Origin and add transport protection. Do not publish the browser gateway to the open internet.

Provider credentials remain in the persistent volume and are not copied from the host into the image. The browser reader is best effort: a revoked session, MFA challenge, network interruption, sleeping host, or DOM change can require attention. Reconnect establishes a new context baseline and does not replay earlier messages.

## Build and published image path

The Dockerfile groups runtime installation and source copying into two `RUN` steps, using read-only build-context mounts and the existing `.dockerignore` allowlist. Separate donor stages supply Node/Codex and noVNC's static assets. This limits filesystem snapshots on the isolated TrueNAS builder. Runtime paths, versions and file permissions remain explicit in the Dockerfile.

`compose.yaml` builds from the repository's `Dockerfile`. The source build is:

```sh
docker compose build
docker compose up -d
```

GitHub Actions builds a local OCI archive for Linux `amd64`, generates a Syft SBOM, and rejects every High or Critical Grype finding before publishing `ghcr.io/javadevjt/discord-option-tailer:latest`. Publication verifies the archive checksum, manifest digest, and successful scan of every requested platform, then copies those same bytes to GHCR and verifies each release tag. A failed scan leaves its reports available as workflow artifacts and does not upload the candidate image. Tests and the Git-history secret scan must also pass.

The container uses checksum-pinned Chrome Stable through Playwright's `chrome` channel. Node and Codex are copied from an immutable donor image; npm, npx, Yarn, and unused Apache utilities are absent from the runtime. Native development keeps Playwright's default browser unless `RELAY_BROWSER_CHANNEL` is set. CI refreshes the `runtime` installation stage on every build so a cached dependency layer cannot hide OS package updates. High/Critical findings remain fatal even when an advisory has no available fix.

Storage admission, usage sampling, pressure enforcement and completion reporting
belong to the administrator-installed runner hooks. The workflow uses the
`truenas-discord-option-tailer-storage-11g` runner pool and does not call the legacy
`ci-storage-efficiency start/finish` commands or expect their former JSON reports.
The build job uploads the scanned candidate, then must complete successfully,
including its native final storage check, before a separate hosted publication
job can run. That job downloads the exact artifact ID, rejects an artifact digest
mismatch, and rechecks the archive against its scan receipt before registry copy.
Review runner-owned measurements when dependencies or build steps change. The
candidate artifact is retained for one day; publication receipts retain the
scanned digest, archive checksum, release tags and verified published digests
for fourteen days alongside the separate vulnerability reports.
For deployment without a source build, follow [the TrueNAS guide](truenas.md)
using [compose.truenas.yaml](../compose.truenas.yaml).

## Validation

Run the following after a source build:

```sh
docker compose config
docker compose ps
curl --fail http://127.0.0.1:8787/healthz
python -m unittest discover -s tests -v
```

The repository's offline demo and fixture tests use synthetic messages, quotes, accounts, and browser state. Private exports and real-account evidence are absent from the public tree. These checks validate local behavior and bootstrap health; they do not qualify live Discord delivery, broker order acceptance, fills, or unattended operation.

Common setup failures are visible in the dashboard status. If the reader is not current, use **Reconnect** and complete any browser sign-in. If a mode change is blocked, keep the relay paused until active sign-in jobs finish and the worker reports the requested mode. A missing or stale provider connection must be repaired in its own browser flow before Live can be enabled.
