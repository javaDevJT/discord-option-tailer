# OpenWoof observability

The optional integration observes the running TrueNAS app. It retains original
console output, logger levels, subprocess capture, request budgets and trading
behavior. It does not send test trades, replay messages, call a model for QA, or
create an OpenWoof API key.

## Runtime and data path

The existing container runs an official Collector under Supervisor. Its OTLP
HTTP/protobuf receiver binds to **127.0.0.1:4318 inside that container**, with no
published receiver port. Python SDKs initialize before application modules and
send there. The Collector alone reads the ingest key and exports over verified
HTTPS to `https://openwoof.javadevjt.tech`, appending `/v1/traces`, `/v1/metrics`
and `/v1/logs`. A file-backed bearer authenticator supplies one Authorization
header; it is not combined with an API-key header or browser credentials.

The Collector is pinned to official release **0.163.2** and its immutable OCI
index digest. Python SDK/exporter **1.45.1** and instrumentation **0.66b1** are
pinned in the `telemetry` extra. The final application image passes the existing
test, secret, SBOM, High/Critical and publication gates as one scanned artifact.

| Actual hook | Signal and boundary |
|---|---|
| Dashboard HTTP handler | SERVER spans, actual response status, duration; accepts valid incoming trace context. Successful health/readiness polling is excluded. |
| HTTPX, aiohttp and urllib client calls | Genuine CLIENT requests, peer, method, observed status/error and elapsed time. No fabricated third-party SERVER spans. |
| Robinhood MCP initialize, list-tools and call-tool awaits | Named protocol operations, actual response/error and tool count; HTTP requests appear as children where context is available. No arguments or account payloads. |
| SQLite cursor calls and public connection execute/executemany/executescript shortcuts | CLIENT statement operations and errors. Parameter values and iterators are not captured or consumed. |
| Message handler and actual account refresh | Business spans around existing work; no additional refreshes or evaluations. |
| Codex interpretation and subprocess communicate/wait | Actual elapsed time, exit code and errors. This does **not** instrument the CLI's internal network activity. |
| Discord library message-receipt callback | Observed CONSUMER handling. aiohttp covers HTTP calls/handshakes; individual Gateway frame round trips are not inferred. |
| Existing notification send | Existing send operation plus actual urllib dependency calls. No extra webhook message is sent for validation. |

Services are `discord-option-tailer-dashboard`, `-worker`, `-notifications`,
`-gateway`, `-desktop`, and `-runtime`. Namespace is `trading`, environment is
`production`, and `service.version` is the full image source revision. The
browser bundle contains no ingest key or exporter. Browser-side rendering is
not instrumented; the dashboard Python server has its own identity.

## Original logs and privacy

Supervisor's process-output events add a tee for all eight existing child
programs. Their `/dev/stdout` destinations and existing stderr merging remain.
Supervisor also retains console output while writing a bounded file for its
own original lifecycle records. No replacement access/completion messages or
sentinels are generated. The Collector/listener's own new diagnostics remain on
the console and cannot recursively re-enter the tee.

The tee assembles complete pretty-printed JSON documents and preserves their
formatted body. Other output, including traceback lines, uses explicitly
declared newline framing. Oversized copies are segmented with ordering metadata;
the original stream is untouched. Per-source sequence, program, PID, stream and
framing attributes support reconstruction. Severity/logger names are retained
where supplied in structured or formatted output; otherwise the source is
identified as `program.stream`. Filelog's own instrumentation scope remains
the real receiver scope. No hidden native LogRecord metadata is invented.
An explicit JSON event timestamp is retained and labeled; otherwise the tee's
observation time is labeled. Logs without a real active trace are retained
without fabricated correlation IDs. Pre-Supervisor bootstrap failures and
final Supervisor teardown after the Collector exits cannot be claimed as
exported runtime coverage.

Only named private fields/patterns are redacted in exported copies: credential
and account-number fields, Bearer/Basic values, URL user-info, private query
parameters and Discord webhook secrets. Nonsecret message bodies, contract
context, reasons, levels, logger names, exception text, SQL structure and benign
pagination parameters survive. For example, a fixture containing
`symbol=SPY`, `limit=100` and `access_token=<fixture>` retains the first two and
redacts the token. Original console/file output is not rewritten.

There is one process-log export path: Supervisor tee → app-owned JSONL →
filelog → targeted privacy transforms → batch → HTTPS exporter. Python does not
add a duplicate log exporter. Span attributes/events receive targeted credential,
private-query and webhook-path filtering in the same Collector. No body-wide
replacement, literal-message allowlist or generic operation rename is used.

Queues, batch sizes, receiver body size, span attributes and file rotation are
bounded. SDK export runs off the entry path; export failure cannot turn an
application return into a failure. The Collector uses a memory limiter and a
finite retry window. Graceful process exit flushes providers; the output tee
stops before the Collector. Existing app retry, timeout, cancellation, logging
and trading policies remain authoritative.

## Operator setup and continuing custody

The app-specific operator helper is bound to `discord-option-tailer` on
`192.168.0.2:8443`. It preserves the complete existing Compose object and changes
only the scanned image, telemetry variables and one read-only secret-file mount.
It checks for concurrent configuration drift and journals before mutation;
uncertain or failed writes are not automatically retried or rolled back.

Prepare the exporter and protected file first. Compile
`ops/openwoof-keychain.swift` into
`artifacts/private/openwoof-deployment/openwoof-keychain`, using a private module
cache. `bun ops/deploy-openwoof.mjs prepare --confirm discord-option-tailer`
creates only the new app-scoped NAS secret directory (root 0700) and empty file
(root 0600), refusing a pre-existing foreign path. The pinned NAS CA is public;
TLS transport verifies that certificate and its expected name.

`bun ops/deploy-openwoof.mjs deliver --confirm discord-option-tailer` validates
that prepared file, closes the NAS session, then presents the hidden Mac dialog
if the exact entry is absent. A fresh NAS connection and file checks follow the
human interaction. The key never appears in argv, environment variables,
stdout, logs, source or the browser. Secret-bearing helper modes require an
inherited private pipe; status-only mode reads metadata without key bytes.

- Keychain service:
  `openwoof/openwoof.javadevjt.tech/truenas-192-168-0-2/discord-option-tailer/production`
- Keychain account: `discord-option-tailer-collector`
- NAS file: `/mnt/Lake/discord-option-tailer-openwoof/openwoof-ingest-key`
- Read-only container mount: `/run/secrets/openwoof-ingest-key`
- Custom exporter input: `OPENWOOF_INGEST_KEY_FILE`

The Collector refuses missing, empty, unreadable or non-owner-only files.
Existing matching Keychain entries are reused; unrelated entries are not read
for convenience, overwritten or rotated. The established NAS credential reader
is reused for operator transport. Continuing app credentials are retained.

Use an existing/admin-created key with `collector:write`, `traces:write`,
`metrics:write`, and `logs:write`. Prefer finite expiry; Never must be explicitly
selected by the owner and remains revocable. Custody names do not enforce
service/environment isolation: the key has Collector-wide write authority and
no UI/query/admin rights. Expiry and scope metadata require owner verification;
the helper does not silently create or query an OpenWoof key.

After exact-source CI and registry verification,
`bun ops/deploy-openwoof.mjs deploy --confirm discord-option-tailer IMAGE PROOF_JSON`
requires the verified immutable app image and successful scan/publication proof.
Subsequent ordinary image-only releases preserve this secret injection.

## Metrics and verification

`relay.operation.calls` is a monotonic cumulative counter, unit `{call}`, for
completed observed operations, including errors/cancellation—not a trade count.
`relay.operation.duration` and `http.server.request.duration` are cumulative
histograms in seconds. `relay.process.uptime` is a gauge in seconds. Official
client instrumentation may also emit its documented HTTP duration instrument.
Labels use bounded operations, outcomes, methods and routes; payloads, account
IDs and message IDs are not metric dimensions. Do not sum cumulative exports as
per-run counts; compare instances and time windows correctly.

The authenticated read-only `/api/telemetry` route reports SDK initialization,
Collector health and raw current-process Collector counters. Receiver acceptance
or exporter sent counts alone do not establish persistence or owner readback.

Run the ordinary suite with `.[browser,robinhood,discord,telemetry,telemetry-test]`
and `node --test ops/test-deploy-openwoof.mjs`. The new tests exercise unchanged
exceptions/cancellation, UTF-8 and JSON fragmentation, targeted redaction,
untraced logs, disk failure, SQLite transaction/iterator semantics and the **real
Supervisor event protocol**. Mac helper self-tests and private-pipe tests do not
read a credential. `ops/test-collector-pipeline.py` runs actual SDKs and the
pinned Collector through an isolated TLS/protobuf fixture with success, failure,
metadata, metric-temporality and exporter-failure checks. Fixtures are explicitly
not production coverage and never contact a paid provider or OpenWoof.

Production acceptance requires the current source/CI/digest/runtime chain,
original local records matched to persisted OpenWoof records, genuine completed
dependency spans and actual metric observations, followed by authorized owner
Authentik readback. Use naturally occurring work. Quiet or unsafe paths stay
UNEXERCISED; absent owner sessions leave readback BLOCKED. Keep failed attempts
separate from later passing evidence. Private release evidence is excluded under
`artifacts/private/` and indexed in the active deployment ledger.

## Official references

- [Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [Python exporters and metric temporality](https://opentelemetry.io/docs/languages/python/exporters/)
- [Collector bearer-token file authentication](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.163.0/extension/bearertokenauthextension)
- [Collector filelog receiver](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.163.0/receiver/filelogreceiver)
- [Supervisor process-output events](https://supervisord.org/events.html)
