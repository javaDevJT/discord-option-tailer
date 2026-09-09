# Container setup and dashboard

This container packages the relay worker, a visible Chromium desktop for Discord, the Codex CLI, Robinhood OAuth support, an authenticated monitoring dashboard, and an optional notification worker. The Compose deployment keeps configuration, ledgers, browser state, and provider login state in one persistent Docker volume.

> [!WARNING]
> A fresh volume starts in Shadow mode with live submissions disabled. Live can submit real Robinhood orders, so use the dashboard's pause and confirmation flow only after checking the current account, channels, quotes, and ledger.

## Prerequisites

- Docker Engine or Docker Desktop with Compose.
- A browser for the Discord, Codex, and Robinhood sign-in pages.
- A Discord account and channels you are authorized to read, a ChatGPT subscription that can use Codex, and a Robinhood account eligible for the broker integration.
- A private `.env` file. Do not commit it or replace an existing one without preserving its settings.

No OpenAI API key is needed. Discord input uses the rendered personal browser session, and Robinhood setup uses normal browser OAuth.

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

1. Open **Browser login** and sign in to the personal Discord account in the embedded Chromium desktop. Complete any verification in that browser.
2. Select **Refresh servers**, choose a server, and choose a channel for each of the two rows. The names come from the signed-in browser's rendered sidebar.
3. Set each row to `Signals` when it may supply actionable alerts, or `Context` when it should provide context only. Author restrictions are optional; an unchecked restriction accepts every author in that channel.
4. Choose a poll interval from 2–60 seconds (3 seconds by default), then select **Save channels**.

The advanced channel URL field is a fallback when a channel is not visible in the current directory. The worker requires exactly two distinct channel bindings. The reader does not extract a Discord token or post to the input channels.

### Codex subscription

Select **Start Codex sign-in**, open the displayed device verification link in your own browser, and enter the displayed code. The code belongs to that login attempt and may expire. Codex runs through the existing ChatGPT subscription; the container does not use an OpenAI API key. The subscription's usage limits still apply.

### Robinhood connection

Select **Start Robinhood sign-in**, open the displayed authorization link in your normal browser, and approve the OAuth request. The default callback uses localhost port `8766`. With a remote Docker host, the returned page may show a connection error: copy the complete address from that tab, paste it into **Returned callback URL** in Setup, and select **Finish Robinhood sign-in**. Complete this within the five-minute sign-in window. Setup validates the callback against the active attempt and passes it to the existing state/PKCE flow; the pasted URL is not saved. After authorization, Setup performs account inspection and saves the selected account binding. Choose an explicit account only when more than one eligible account requires a choice.

`RELAY_ROBINHOOD_REDIRECT_URI` can override the callback with an HTTP(S) address ending in `/callback`. The bundled proxy supports callbacks on the dashboard port, but public registration accepting an address does not prove Robinhood will accept it during authorization. A Robinhood-hosted connection-error page occurs before a callback and cannot be completed by pasting that error-page address.

Authentication, account inspection, and order execution are separate stages. Setup does not review, place, cancel, or claim a fill for an order. Keep the relay in Shadow while verifying the connection.

### Discord output notifications

**Setup → Discord output webhook** is an optional output destination. Paste a valid Discord webhook URL, enable notifications, and save. The URL is stored privately and stays hidden after saving; disabling notifications keeps the saved URL, while removing it clears the destination.

Notifications can report authentication assistance and relay actions with a Paper, Shadow, or Live label. They are separate from the personal Discord browser used as input. Message bodies, account identifiers, OAuth codes, and credentials are omitted, and no test post is sent by setup.

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

## Fresh processing and recovery review

After the first login, restart, or reconnect, the visible channel history becomes a context baseline. Baseline rows are recorded for context and are never replayed as fresh entries. New dispatch still requires an eligible `Signals` row, an authorized author, an exact contract, and current source and market data.

Eligible baseline alerts and fresh alerts held because they became stale may be queued for a recovery assessment after the browser returns to the newest messages. Recovery compares the original alert with later same-source context and current account and quote facts. The dashboard labels the result **Potentially viable**, **Invalidated**, **Uncertain**, or **Not actionable**, with timing, evidence, facts, and blockers. Every recovery result is review-only; it has no approval or order-submission path.

Recovery has no age or entry-count cutoff for an assessment, but it is bounded by messages the browser actually observed and by durable per-revision deduplication. It does not fetch every missed Discord message, reconstruct a historical price path, or convert an old alert into a fresh trigger. Edited rows, manual backscroll, imported history, future timestamps, unauthorized authors, and context-only rows are excluded.

## Risk guardrails

The default configuration keeps the confidence-based entry allocation ceiling between 5% and 10% of current equity:

| Guardrail | Default |
| --- | --- |
| Minimum parse confidence | `0.80` |
| Entry ceiling | `5%` at the threshold, `7.5%` at `0.90`, `10%` at `1.00` |
| Same-underlying exposure | `10%` of equity |
| Total option exposure | `20%` of equity |
| Signal freshness | `90` seconds |
| Quote freshness | `15` seconds |
| Maximum spread | `15%` |
| Maximum chase | `5%` above the cited premium |
| Same-day expiry opens | Disabled by default |
| Fee reserve | `$1.00` per contract |

These are maximum allocations, never a required spend or minimum account balance. Current buying power, existing and pending exposure, whole-contract quantity, and fees can reduce an entry to zero. There is no fixed-dollar cap, fixed contract-count cap, daily entry-count limit, or daily gross-entry limit. Optional calibrated quarter-Kelly statistics can only reduce the ceiling.

The execution path accepts single-leg long standard USD equity or ETF options for buy-to-open, reduce, and sell-to-close actions. It requires an exact symbol, absolute expiry, strike, and call or put. Futures, crypto, short positions, spreads, conditional scheduling, averaging in, and stop amendments are held. `UPDATE_STOP` does not install a protective stop.

## Monitor and operate

The dashboard provides an operational snapshot, runtime status for Discord/Codex/Robinhood, observed messages, interpretation states, held decisions, recorded orders, relay-owned positions, and the decision trail. Relay-owned positions represent what this ledger recorded; they are not a complete broker account statement.

Use the dashboard controls for **Pause relay**, **Resume relay**, and **Reconnect**. For container-level checks and logs:

```sh
docker compose ps
docker compose logs --tail=100 relay
docker compose stop
docker compose start
```

Pausing and stopping are safe operational controls for new work. Preserve the Docker volume when restarting or moving the service; it contains the browser profile, Codex subscription state, Robinhood OAuth state, configuration, and mode-specific ledgers.

## Network and security boundary

The dashboard is published on port `8787` and the Robinhood callback on port `8766`; both bind to host loopback by default. HTTP Basic authentication protects the dashboard and browser desktop. If you intentionally expose the service through a private network or reverse proxy, preserve the original Host and browser Origin and add transport protection. Do not publish the browser gateway to the open internet.

Provider credentials remain in the persistent volume and are not copied from the host into the image. The browser reader is best effort: a revoked session, MFA challenge, network interruption, sleeping host, or DOM change can require attention. Reconnect establishes a new context baseline and does not replay earlier messages.

## Build and published image path

`compose.yaml` builds from the repository's `Dockerfile`. The source build is:

```sh
docker compose build
docker compose up -d
```

GitHub Actions publishes `ghcr.io/javadevjt/discord-option-tailer:latest` for Linux `amd64` after the tests and secret scan pass. For deployment without a source build, follow [the TrueNAS guide](truenas.md) using [compose.truenas.yaml](../compose.truenas.yaml).

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
