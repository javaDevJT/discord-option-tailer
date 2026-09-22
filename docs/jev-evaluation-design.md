# Fast entry rules, JEV, and Codex fallback

Local verification on September 22, 2026 passed all 555 tests, including prepared entries using review quotes, bounded order matching after a lost response, durable restart ownership and later sells, cancellation deadlines, observe-only behavior, and the existing rendered UI checks. Tests used fixture broker and model responses. They do not establish live submission or fill latency.

## Routing boundaries

1. **Direct code:** a recognized literal entry with exactly one contract and premium, no unsupported clause, and sufficient explicit facts returns a validated OPEN decision immediately. The current schemas cover clean structured ENTRY cards and complete supported OPEN/BUY text. No model lock, request, or token is involved.
2. **JEV:** a bounded candidate with complete text facts but intent requiring interpretation can use the configured JEV fallback. Native confidence, selected probability, and action/evidence eligibility must meet their configured thresholds. Eligible entry attempts have a 700 ms ceiling, including slot waiting; other JEV attempts use the configured deadline, up to 1,200 ms.
3. **Codex:** missing or contradictory facts, screenshot-dependent contracts, unsupported compound actions, unresolved references, schema failures, timeout, or uncertain JEV output use the subscription-backed evaluator. Recovery assessment remains Codex-only.

Prose profit updates still use model interpretation. Evidence must quote the supplied message text or embeds exactly; rendered custom emoji and text read from an image are not substitutes for a source quote. A rejected evidence result gets one corrective retry with the specific local validation issue and the unchanged source/image context. Recovery uses its own assessment schema during that retry. If validation still fails, the dashboard reports the bounded validation reason and no order is submitted; raw provider output and credentials are never included in that diagnostic.

A direct entry uses positive template matching. Extra comments, unparsed stop or sizing instructions, cancellation, historical/quoted content, conflicting contracts/prices, missing call/put, and incomplete fields do not become buys through a loose keyword match. Current text must independently establish the intended entry. An unrelated earlier image does not invalidate complete current text. An image that may supply a missing expiry or other necessary fact requires image-capable interpretation.

Omitted entry expiry retains the source-date 0DTE/next-listed rule. Only the execution engine resolves the available expiry; no duplicate broker discovery is needed before model interpretation. Exits must identify the exact held contract and expiry. A defaulted exit is not promoted into a new contract.

The normalizer and existing engine still enforce configured channel/author permissions, live versus baseline/edited history, source revision/chronology, durable duplicate claims, and inventory ownership. Direct parsing does not grant authority to arbitrary Discord content or change the broker mode.

## Configuration and frontend

`evaluation.direct_entries` defaults to `true` and is exposed as **Direct entries** in Setup. Disable it to use the selected evaluator for every interpretation. The existing `evaluation.mode` selects Codex fallback, JEV with Codex fallback, or JEV shadow. **JEV shadow always keeps Codex authoritative**, including messages that otherwise qualify for direct entry.

JEV credentials remain optional for direct code. When JEV is selected, its TypeSafe credential is stored privately through Setup or provided by the existing environment variables. Codex continues using the ChatGPT subscription. Missing model authorization does not prevent a complete direct entry from being interpreted; an ambiguous message still requires a usable fallback and reports authorization failures normally.

Parsing confidence describes certainty about the explicit instruction. It is not a forecast of trading profit and does not become a Kelly win probability. Direct entries supply 1.0 parsing confidence, selecting the upper configured confidence-sizing reference before the other deterministic sizing adjustments. Existing affordability, maximum chase, whole-contract trim rounding, and expiry-protection rules continue to run.

## Execution and timing

### Watch preparation and capped entries

A fresh, authorized `on watch`, `eyes on`, or `watching` message naming one option can prepare its exact broker instrument before ENTRY. Preparation is bounded to eight candidates for one hour in the current New York session, refreshes metadata every 30 seconds, and never places an order. An omitted watch expiry uses the existing nearest-listed rule. A later ENTRY must match the source group, symbol, strike, side and expiry semantics; an explicit watch expiry cannot silently override an undated ENTRY. Edited, historical, ambiguous and ticker-only watch messages do not authorize this path. Missing or stale preparation falls back to normal discovery and quote retrieval.

For a matched live ENTRY, the limit is the ENTRY premium multiplied by one plus the configured chase allowance, rounded down to the applicable trading increment. Sizing uses that limit and the fee reserve. The prepared path obtains its timestamped quote from broker review instead of a separate quote call. Current spread, quote age, session, account, source and ownership checks remain required. An ask above the capped limit can leave an order resting briefly without increasing the limit.

Prepared entry orders carry a persistent three-second cancellation deadline. Reconciliation cancels any unfilled remainder after the deadline and applies cumulative fills exactly once, including fills racing cancellation. Cancellation is asynchronous: three seconds is the target before requesting cancellation, not a guaranteed exchange-side expiry. Restart recovers that deadline and reconciles before accepting fresh signals. Review and placement remain provider requests; fixture tests cannot establish subsecond live submission or fill latency.

### Existing execution path

```mermaid
flowchart LR
    A[Live Gateway event] --> B[Persist and check source]
    B --> C{Complete literal entry?}
    C -->|Yes| D[Code parses and validates OPEN]
    C -->|No| E[JEV or configured Codex fallback]
    E -->|Unresolved| F[Codex with context and images]
    D --> G[Serialized execution checks]
    E --> G
    F --> G
    G --> H[Fresh account and contract data]
    H --> I[Broker review and final source guard]
    I --> J[Submit limit order]
```

The direct route does not wait for the Codex lock. A fresh account snapshot starts alongside missing-expiry discovery. Chain metadata and instruments for the exact symbol/strike/type are requested concurrently. The initial instrument query covers only the source date and following six calendar dates; all response pages are read before choosing the earliest qualified contract across matching chains. If no nearby contract exists, later listed dates are queried individually in order. Ambiguity or a matching instrument whose expiry conflicts with its chain listing causes a hold. Cold lookups for an explicit expiry also overlap chain and instrument reads.

Broker reads have a task-local scope covering one serialized decision. Just-resolved contract metadata passes to its first quote lookup. Successful account and quote reads may be reused for at most one second after completion within that scope, subject to their original timestamp checks; the cache does not reset their age. This removes repeated planning-to-submission reads without retaining completed prices or balances for later decisions. Failures, cancellation, completion, or a broker mutation invalidate the scope. Reads outside that scope retain their existing behavior. Position metadata and raw quotes are read concurrently with a shared limit of four outstanding requests. Overlapping portfolio requests share only the currently running fetch.

The exchange calendar is initialized in a background thread during broker startup, before signal processing. Market-session checks still run at decision and submission time. A local cold calendar call took 1.057 seconds versus 0.000115 seconds once initialized; those are CPU measurements, not broker-response measurements.

An offline comparison of the same nearest-expiry entry through a fixture broker reduced the request count from 18 to 9. With a simulated 150 ms delay per request and calendars initialized for both versions, elapsed time through the simulated submission fell from 1.372 to 0.758 seconds. This excludes Discord source verification and real provider variability; it is a request-graph regression check, not a live latency claim.

The September 22 follow-up kept nine calls but removed another serial dependency. With the same 150 ms simulated RPC delay, the same-day fixture fell from 0.828 to 0.639 seconds and the nearest-later-expiry fixture from 0.803 to 0.616 seconds. These measurements also exclude real network and Discord source verification. Broker latency can still keep a real submission above the one-second target.

Live execution performs its authoritative source verification at the final pre-submit boundary after broker review, with local checks before and after. A changed source, kill switch, stale quote, account restriction, reduced affordability, excessive chase, or uncertain previous order still prevents placement. Cancellation drains pending reads. The existing order ledger and idempotency rules remain authoritative.

Quote validation runs inside the parallel quote task. An invalid or over-chase quote can therefore stop the decision and cancel an unfinished account read immediately. An accepted entry still waits for both fresh account and quote results; this early rejection does not authorize a buy from cached account data.

The dashboard distinguishes rules, JEV, and Codex and displays:

- receipt and queue delay;
- rule/model interpretation time;
- expiry discovery, fresh snapshot, quote, and execution wait;
- receipt-to-submission and posted-to-submission when the final placement boundary is reached;
- broker response time/status separately from acceptance or exchange fills.

Missing measurements remain unavailable, not zero. Fast-path coverage includes direct decisions and JEV separately.

Fastest arrival requires the event-driven Gateway transport. New example configurations select Gateway. Existing saved transports are preserved; browser polling defaults to three seconds and cannot supply subsecond arrival. Required remote account, quote, review, and submission work remains outside local parser control. The one-second objective is an operational target, not a proven provider execution guarantee or a reason to skip those checks.

## Observed-message qualification

The September 20 audit found the previous implementation admitted zero actionable decisions from the original exports. The corrected rules were replayed against all 196 messages with the actual shared-context selector. Exactly the expected 12 clean structured entries and one complete plain entry qualify directly. The contradictory watching comment and other unsupported instructions remain outside direct execution.

After those 13 direct entries, 24 messages qualify for a bounded JEV attempt: seven OPEN candidates and 17 WAIT candidates. The other 159 require Codex. These are routing counts, not measured model accuracy or successful trades. Across 1,300 local parser calls, the measured median was 0.038 ms, p95 0.088 ms, p99 0.153 ms, and maximum 0.246 ms; broker and network work are excluded.

Private source exports, exact message examples, and detailed replay evidence are retained locally and excluded from the public repository. Local replay verifies parsing and routing; it does not establish model accuracy, current holdings, live latency, or fills.

## Provider contract references

The native TypeSafe choice contract and authentication were verified against its primary documentation on September 18, 2026:

- [Native quick start](https://docs.typesafe.ai/introduction/quickstart.md)
- [Choice questions and confidence](https://docs.typesafe.ai/primitives/choice.md)
- [State input](https://docs.typesafe.ai/concepts/state.md)

See the [setup guide](container.md#jev-fast-evaluation) for the frontend workflow.
