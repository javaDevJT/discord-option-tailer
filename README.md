# Discord Option Tailer

Discord Option Tailer watches exactly two configured Discord channels through a personal-account Gateway session (`discord.py-self`) or a manually signed-in browser. Complete literal entries use deterministic code without a model call. Other messages use optional JEV with subscription-backed Codex fallback. Existing option risk checks and a mode-bound SQLite ledger govern execution.

Fresh Docker volumes start in **Shadow** mode. Shadow can read current broker data and save proposed orders, but it never submits them. **Live** is an explicit, paused setup action that can submit real Robinhood orders. Missed entries remain review-only; verified missed exits can close existing relay-owned contracts in Live.

## Project index

The [account monitoring guide](docs/container.md#account-balances-and-holdings) explains cached balances, account option holdings, and refresh timing.

- [Container setup and dashboard](docs/container.md): start the service and complete Discord, Codex, Robinhood, notification, and trading-mode setup in the frontend.
- [TrueNAS deployment](docs/truenas.md): deploy the published image with persistent private storage.
- [TrueNAS Compose file](compose.truenas.yaml): image-based deployment with no source build.
- [Broker integration](docs/robinhood-integration.md): authorization, account inspection, and execution checks.
- [Agent-directed stops](docs/container.md#agent-directed-stop-losses): compound trims, breakeven protection, cancellation and restart behavior.
- [Fast entry evaluation design](docs/jev-evaluation-design.md): watch preparation, capped limit entries, direct entry rules, JEV/Codex boundaries, and timing checks.
- [Configuration template](config.example.json): CLI and offline-rehearsal defaults.
- [Container environment](.env.example): dashboard binding and password settings.
- [Relay source](relay/): Gateway and browser readers, interpreter, ledger, broker adapter, dashboard, and worker.
- [Synthetic checks](tests/): offline unit and fixture tests.

Private account exports, browser profiles, OAuth state, databases, and local credentials are runtime data and are intentionally absent from the public source tree. The public documentation contains no account state or real message samples.

## How it works

Expiry-day monitoring closes remaining in-the-money relay-owned options independently of Discord. See [expiry exercise protection](docs/container.md#expiry-exercise-protection) for timing, restart behavior, alerts, and fill limitations.

1. The Gateway reader receives live message events through `discord.py-self==2.1.0`; the browser fallback reads the rendered DOM. Both use the personal account and never post to the input channels. Configure the transport and optional private Gateway credential in **Setup → Discord input**.
2. The relay stores normalized messages. With Direct entries enabled, supported complete ENTRY and OPEN alerts are parsed without model calls; other messages use the configured fallback evaluator. Codex uses the user's ChatGPT subscription; optional JEV uses a separate TypeSafe credential entered in Setup. JEV shadow keeps Codex authoritative.
3. Deterministic checks require a clear standard option contract, an allowed source, fresh inputs, current quotes, account-relative sizing, and a mode-specific ledger.
4. The dashboard shows messages, interpretations, holds, recorded orders, relay-owned positions, and recovery assessments. Each newly evaluated message shows model evaluation duration and elapsed time from posting to the recorded decision. An optional Discord webhook reports selected setup assistance and relay actions.
5. Robinhood access uses its normal browser OAuth flow and a configurable callback. Passwords and MFA stay with Robinhood; setup performs account inspection before any execution mode can be selected.

## Fresh messages and recovery

The first history observed after login, reconnect, or restart is a context baseline. It is recorded for interpretation context and is never replayed as a new entry. Gateway history is bounded to one page of at most 100 messages per configured channel, requested sequentially. A fresh eligible alert must still pass the normal freshness and source checks before it can reach the mode-specific order path.

When the reader returns to the newest messages, eligible baseline alerts and alerts held because they became stale receive a separate recovery assessment. Startup also checks saved context after the current relay-owned entry for previously missed exits. Historical entries remain review-only. A viable exit can use the normal mode-specific order path only after checking current source content, later context, the exact position lifetime, remaining ownership, current quotes/account data and the absence of a consumed or later exit. The dashboard identifies executed catch-up exits separately from review-only assessments.

Recovery is bounded by the saved messages and current reader cache; it does not fetch complete Discord history. A temporarily unavailable source keeps a viable exit pending with a 30-second retry and no repeated model evaluation while its context and position remain unchanged. Newly observed context discards the cached assessment and queues a fresh one. Unavailable account/market facts defer assessment for five minutes. Expired contracts, ownership mismatches and already-consumed exits cannot be replayed. Uncertain submissions are reconciled without resubmitting; canceled or rejected orders are not automatically replaced.

Edited alerts, manual backscroll, imported history, future timestamps, channels configured only for context, and unauthorized authors stay out of execution. Visible embed text and metadata are available to the interpreter; image-only instructions are held.

## Supported actions and limits

Fractional exits round up only the fractional remainder: **50% of 1 → sell 1; 50% of 2 → sell 1; 50% of 3 → sell 2**. Explicit full exits sell all remaining relay-owned contracts. Optional current language such as “you can trim/take profits if you'd like” is actionable only when the current tick-rounded sell price exceeds the recorded entry cost plus twice the configured per-contract fee reserve. An unspecified optional trim sells half remaining, rounded up. Explicit exits such as “took 50% here” or “all out” retain their stated size and do not require a profit. Future conditions and performance recaps are not immediate sale instructions.

The trading path supports single-leg, long, standard USD equity or ETF options: buy to open, reduce, and sell to close. It requires an exact symbol, absolute expiry, strike, and call or put. Futures, crypto, shorts, spreads, conditional scheduling, averaging in, and stop amendments are held. `UPDATE_STOP` does not install a protective stop. Missing entry expirations default to 0DTE or the nearest listed expiration at the exact strike/type; dates stated in text, embeds or supplied pictures take precedence. The original message's New York date anchors resolution, and old alerts cannot roll forward. Same-day entry permissions remain in effect.

The default sizing references and eligibility checks are:

| Guardrail | Default |
| --- | --- |
| Minimum parse confidence | `0.80` |
| Confidence-based entry sizing | `5%` at the threshold, rising to `10%` at confidence `1.00` (`7.5%` at `0.90`) |
| Same-underlying sizing reference | `10%` of equity |
| Total option exposure sizing reference | `20%` of equity |
| Buying-power reserve | `0%` local reserve; broker restrictions still apply |
| Signal age | At most `90` seconds for a fresh action |
| Quote age | At most `15` seconds |
| Maximum spread | `15%` |
| Maximum chase above cited premium | `10%` |
| Same-day expiry entries | Disabled by default; configurable in Setup |
| Fee reserve | `$1.00` per contract in the default configuration |

The 5%–10% range and percentage exposure limits determine whole-contract quantity. If that reference budget is below one contract, an otherwise eligible entry uses **exactly one contract** when available buying power covers the rounded limit price times 100 plus the fee reserve. This fallback may exceed the percentage targets; it never exceeds buying power after any configured cash reserve. Once the reference budget supports whole contracts, normal sizing applies. There is no fixed-dollar cap, fixed contract-count cap, daily entry-count limit, or daily gross-entry limit. Optional calibrated quarter-Kelly statistics can reduce the reference budget; a nonpositive calibrated edge still blocks entry. Chase, quote freshness, signal eligibility and broker restrictions remain required.

In **Setup → Signal evaluation**, select the Codex model, reasoning effort, Fast or Standard service, and maximum chase percentage. Saving pauses the relay. Settings remain editable while a paused worker needs authentication; Resume still requires the worker to load the saved settings and recover. The example configuration uses `gpt-6-astra`, medium reasoning, and Fast with the existing ChatGPT subscription; no API key is required. Chase controls the maximum entry premium above the alert price, independently of position sizing.

Evaluation retries once for transient provider, network, timeout, or invalid-output failures, including evidence validation. Authentication, quota, and configuration errors report immediately. Failed evaluations show a sanitized cause and attempt count; no broker order is retried by this mechanism. The same handling applies to read-only recovery assessments. When Codex or Robinhood rejects authentication, Setup shows reauthentication required and the configured Discord webhook sends one alert per incident. Saved credentials alone do not clear the alert; a successful provider operation does.

## Trading modes

| Mode | Data | Result |
| --- | --- | --- |
| `paper` | Synthetic account and quote fixtures | Simulated fills in an offline ledger; no broker calls |
| `shadow` | Authenticated account and current quotes | Proposed orders are recorded; no submissions or simulated fills |
| `live` | Authenticated account and current quotes | Real submissions are possible only after every execution check and explicit confirmation |

Each mode uses its own ledger. The frontend changes between Shadow and Live only while the relay is paused, requires a worker acknowledgement, and keeps Live disabled until the user confirms it. Pausing stops new execution; it does not cancel an existing broker order or close a position. A recorded proposal, broker review, accepted order, or simulated fill is not by itself proof of a real fill.

## Quick start with Docker

Install Docker with Compose, choose a dashboard password, and start from the repository directory:

```sh
test -f .env || cp .env.example .env
# Edit .env and set DASHBOARD_PASSWORD.
docker compose config
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8787/healthz
```

Open `http://localhost:8787` and sign in with the dashboard credentials from `.env`. The default bind is loopback. A new volume bootstraps the frontend with **Setup required**, **Shadow** mode, no bound Robinhood account, and live submissions disabled. Existing volume state is preserved on restart.

Use the dashboard's **Setup** section to complete the four integrations:

1. **Discord:** open **Browser login**, sign in to the personal account, refresh the server list, choose one channel for each of the two rows, and save. Choose `Signals` for actionable channels and `Context` for context-only channels. Author restrictions are optional; an unchecked restriction accepts all authors in that channel. The browser profile remains in the volume.
2. **Codex:** start device sign-in, open the displayed verification link in the user's own browser, and enter the displayed code. The container keeps the subscription login in its persistent data volume and does not ask for an API key.
3. **Robinhood:** start sign-in and open the displayed authorization link in your normal browser. After approval, if the returned localhost page cannot connect, copy its complete address into **Returned callback URL** in Setup and select **Finish Robinhood sign-in**. This completes a remote deployment without a tunnel; see [TrueNAS setup](docs/truenas.md) for the optional redirect override. Account inspection and binding finish in Setup. Choose an explicit account only when the eligible-account choice is not unique. Setup does not place an order.
4. **Notifications:** optionally save a Discord output webhook. It is a separate output destination from the personal Discord reader, remains hidden after saving, and reports only supported setup assistance and relay actions.

Leave the relay in Shadow while validating channels, provider connections, and fresh-message behavior. To use Live later, follow the paused **Enable Live** confirmation flow in the dashboard and wait for the worker to acknowledge the mode before resuming.

## Source installation and validation

For offline work without Docker, use Python 3.11 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[browser,robinhood,discord]'
python -m playwright install chromium
python -m relay doctor
python -m relay demo
python -m unittest discover -s tests -v
```

The demo and fixture tests use synthetic messages, quotes, accounts, and browser state. They exercise the local safety paths without signing in to a provider or sending a live order. If you supply local export files, use the `relay replay` subcommand with a separate database; it performs import or historical analysis only and never submits historical orders.

## Container images and deployment notes

GitHub Actions runs the synthetic tests and scans Git history for secrets before publishing `ghcr.io/javadevjt/discord-option-tailer:latest` for Linux `amd64`. Main builds also receive a full commit `sha-...` tag. Use [the TrueNAS guide](docs/truenas.md) and [image-based Compose file](compose.truenas.yaml) to deploy it. For local source builds, use `docker compose up -d --build` with `compose.yaml`.

The container stores configuration, ledgers, the Chromium profile, Codex subscription state, and Robinhood OAuth state in one private Docker volume. The dashboard and browser gateway are password-protected; keep the published ports on a trusted loopback or private network. Runtime recovery can reconnect the browser, but a sleeping host, revoked login, MFA challenge, network outage, or rapidly changing DOM can interrupt observation. No unattended-uptime, execution-quality, or profitability claim is made.

For routine operation:

```sh
docker compose logs --tail=100 relay
docker compose stop
docker compose start
```

See [Container setup and dashboard](docs/container.md) for the frontend workflow, recovery semantics, mode controls, notifications, persistence, and troubleshooting.
