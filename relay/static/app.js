const RELAY_READ_TIMEOUT_MS = 10_000;

function relayReadTimeoutSignal() {
  return AbortSignal.timeout(RELAY_READ_TIMEOUT_MS);
}

(() => {
  "use strict";

  const API = Object.freeze({
    status: "/api/status",
    evaluationMetrics: "/api/evaluation/metrics",
    messages: "/api/messages",
    orders: "/api/orders",
    events: "/api/events",
    positions: "/api/positions",
    account: "/api/account",
  });
  const PAGE_SIZE = 25;
  const expandedImagePreviews = new Set();
  const failedImagePreviews = new Set();
  const STALE_AFTER_MS = 120_000;
  const RECOVERY_STATES = Object.freeze([
    ["recovery_pending", "Recovery pending"],
    ["recovery_evaluating", "Recovery evaluating"],
    ["recovery_review", "Recovery review"],
    ["recovery_error", "Recovery error"],
  ]);
  const RECOVERY_STATUS_LABELS = Object.freeze({
    viable: "Potentially viable",
    invalidated: "Invalidated",
    uncertain: "Uncertain",
    not_actionable: "Not actionable",
  });

  const state = {
    autoRefresh: true,
    refreshTimer: null,
    refreshing: false,
    cycle: 0,
    lastSync: null,
    status: null,
    statusError: null,
    evaluationMetrics: null,
    evaluationMetricsError: null,
    panelErrors: new Set(),
    filters: { q: "", channelId: "", state: "" },
    orderStatus: "",
    pages: {
      messages: { offset: 0, nextOffset: null, page: 1 },
      orders: { offset: 0, nextOffset: null, page: 1 },
      events: { offset: 0, nextOffset: null, page: 1 },
    },
    loaded: { messages: false, orders: false, positions: false, events: false, account: false },
    data: { messages: [], orders: [], relationOrders: [], positions: [], events: [], account: null },
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  class ApiError extends Error {
    constructor(status) {
      super(`HTTP ${status}`);
      this.status = status;
    }
  }

  function node(tag, className, content) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined && content !== null) element.textContent = String(content);
    return element;
  }

  function append(parent, ...children) {
    children.filter(Boolean).forEach((child) => parent.appendChild(child));
    return parent;
  }

  function setText(selector, value) {
    const target = $(selector);
    if (target) target.textContent = value == null || value === "" ? "—" : String(value);
  }

  function safeString(value, fallback = "") {
    if (value === null || value === undefined) return fallback;
    if (typeof value === "string") return value;
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    return fallback;
  }

  function firstValue(...values) {
    return values.find((value) => value !== null && value !== undefined && value !== "");
  }

  function humanize(value, fallback = "Unknown") {
    const text = safeString(value, "").replace(/[-_]+/g, " ").trim();
    if (!text) return fallback;
    return text.replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function normalized(value) {
    return safeString(value, "").toLowerCase().replace(/[-\s]+/g, "_");
  }

  function classForState(value) {
    const stateName = normalized(value);
    if (["recovery_pending", "recovery_evaluating", "uncertain"].includes(stateName)) return "is-held";
    if (["recovery_review", "viable"].includes(stateName)) return "is-recovery";
    if (["recovery_error", "invalidated"].includes(stateName)) return "is-error";
    if (stateName === "not_actionable") return "is-context";
    if (["held", "pending", "waiting", "review", "evaluating"].includes(stateName)) return "is-held";
    if (["evaluated", "interpretation_ready"].includes(stateName)) return "is-context";
    if (["paper_order", "shadow_proposal", "shadow_order", "broker_order", "filled", "open", "partially_filled", "submitted"].includes(stateName)) return "is-order";
    if (["context", "duplicate", "canceled", "cancelled", "expired"].includes(stateName)) return "is-context";
    if (["rejected", "unknown", "error", "failed"].includes(stateName)) return "is-error";
    return "";
  }

  function statusPill(value) {
    const pill = node("span", `status-pill ${classForState(value)}`, humanize(value));
    return pill;
  }

  function decisionPill(value) {
    const pill = node("span", `decision-label ${classForState(value)}`, humanize(value));
    return pill;
  }

  function formatDate(value, withSeconds = false) {
    if (!value) return "Time unavailable";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return safeString(value, "Time unavailable");
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: withSeconds ? "2-digit" : undefined,
    }).format(date);
  }

  function formatRelative(value) {
    if (!value) return "time unavailable";
    const timestamp = new Date(value).getTime();
    if (Number.isNaN(timestamp)) return "time unavailable";
    const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
    if (seconds < 10) return "just now";
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.round(minutes / 60);
    return `${hours}h ago`;
  }

  function formatNumber(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number) ? new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(number) : safeString(value, "—");
  }

  function formatDuration(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "age unavailable";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.round(minutes / 60);
    if (hours < 48) return `${hours}h`;
    return `${Math.round(hours / 24)}d`;
  }

  function formatMoney(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return safeString(value, "—");
    return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(number);
  }

  function shortId(value) {
    const text = safeString(value, "");
    if (text.length < 15) return text;
    return `${text.slice(0, 8)}…${text.slice(-5)}`;
  }

  function valueText(value, fallback = "") {
    if (value === null || value === undefined) return fallback;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value)) return value.map((item) => valueText(item)).filter(Boolean).join(" · ");
    if (typeof value === "object") {
      return Object.entries(value)
        .map(([key, item]) => `${humanize(key)}: ${valueText(item)}`)
        .filter(Boolean)
        .join(" · ");
    }
    return fallback;
  }

  function formatContract(contract) {
    if (!contract) return "Contract not recorded";
    if (typeof contract === "string") return contract;
    const symbol = firstValue(contract.symbol, contract.underlying, contract.ticker, contract.root_symbol);
    const expiry = firstValue(contract.expiry, contract.expiration, contract.expiration_date);
    const strike = firstValue(contract.strike, contract.strike_price);
    const right = firstValue(contract.right, contract.option_type, contract.type);
    const parts = [symbol, expiry, strike !== undefined ? `$${formatNumber(strike)}` : undefined, right]
      .filter((part) => part !== null && part !== undefined && part !== "")
      .map((part) => safeString(part));
    return parts.length ? parts.join(" · ") : valueText(contract, "Contract not recorded");
  }

  function countValue(value) {
    if (typeof value === "number") return value;
    if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
    if (value && typeof value === "object") return Object.values(value).reduce((sum, item) => sum + (Number(item) || 0), 0);
    return null;
  }

  function countFor(key) {
    const counts = state.status?.counts || {};
    return countValue(firstValue(counts[key], state.status?.[key]));
  }

  function readDecision(event) {
    if (!event) return null;
    const decision = event.decision;
    if (decision && typeof decision === "object") return decision;
    if (typeof decision === "string") return { action: decision };
    return {};
  }

  function decisionAction(event) {
    const decision = readDecision(event);
    return firstValue(decision.action, decision.intent, decision.operation, decision.type, event?.action);
  }

  function decisionReason(event) {
    const decision = readDecision(event);
    return firstValue(event?.reason, decision.reason, decision.rationale, decision.summary);
  }

  function decisionActionLabel(event) {
    if (event?.state === "error" && !decisionAction(event)) return "Evaluation failed";
    if (normalized(event?.state) === "evaluating") return "Interpretation in progress";
    if (["evaluated", "interpretation_ready"].includes(normalized(event?.state))) return `Interpretation ready${decisionAction(event) ? ` · ${humanize(decisionAction(event))}` : ""}`;
    return humanize(decisionAction(event), "No action");
  }

  function decisionEvidence(event) {
    const decision = readDecision(event);
    return firstValue(event?.evidence, decision.evidence, decision.supporting_evidence, decision.context);
  }

  function recoveryAssessment(event) {
    const decision = readDecision(event);
    const recovery = firstValue(event?.recovery, decision.recovery);
    return recovery && typeof recovery === "object" && !Array.isArray(recovery) ? recovery : null;
  }

  const EVALUATION_IN_FLIGHT_STATES = new Set([
    "observed", "pending", "submitting", "recovery_pending", "recovery_evaluating",
  ]);

  function timingObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  }

  function evaluationTimingStages(event) {
    const decision = readDecision(event);
    const recovery = recoveryAssessment(event);
    const stages = [];
    const direct = timingObject(firstValue(decision?.evaluation_timing, event?.evaluation_timing));
    const recoveryTiming = timingObject(recovery?.evaluation_timing);
    if (direct) stages.push({ kind: "direct", timing: direct });
    if (recoveryTiming) stages.push({ kind: "recovery", timing: recoveryTiming });
    return stages;
  }

  function evaluationTimingPending(event) {
    const stateName = normalized(firstValue(event?.state, readDecision(event).state));
    return EVALUATION_IN_FLIGHT_STATES.has(stateName);
  }

  function formatEvaluationDuration(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) return "unavailable";
    if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
    if (seconds < 60) {
      const precision = seconds < 10 ? 1 : 0;
      return `${Number(seconds.toFixed(precision))}s`;
    }
    const minutes = seconds / 60;
    if (minutes < 60) return `${Number(minutes.toFixed(minutes < 10 ? 1 : 0))}m`;
    const hours = minutes / 60;
    return `${Number(hours.toFixed(hours < 10 ? 1 : 0))}h`;
  }

  function formatEvaluationPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return "unavailable";
    const percent = number <= 1 ? number * 100 : number;
    return `${Number(percent.toFixed(percent < 10 ? 1 : 0))}%`;
  }

  function evaluationTimingValue(event) {
    const stages = evaluationTimingStages(event);
    if (stages.length) return stages[stages.length - 1].timing;
    return {};
  }

  function evaluationTimingPart(timing, key, label) {
    const value = Number(timing?.[key]);
    return Number.isFinite(value) && value >= 0 ? `${label} ${formatEvaluationDuration(value)}` : "";
  }

  function renderEvaluationTiming(parent, event) {
    if (evaluationTimingPending(event)) return;
    const stages = evaluationTimingStages(event);
    const timing = evaluationTimingValue(event);
    const stateName = normalized(firstValue(event?.state, readDecision(event).state));
    const hasTiming = stages.length > 0;
    if (!hasTiming && !["evaluating", "evaluated", "interpretation_ready", "error", "failed", "recovery_error"].includes(stateName)) return;

    const trace = node("div", "evaluation-trace");
    const ruleEvaluation = stages.some(({ timing: stage }) => stage.evaluator === "rules"
      || stage.route === "direct" || stage.model === "deterministic-entry-v1");
    append(trace, node("div", "evaluation-trace-heading", ruleEvaluation ? "Rule evaluation" : "Model interpretation"));

    const labels = [];
    const recovery = isRecoveryEvent(event);
    if (stages.some(({ kind, timing: stage }) => kind === "recovery" || stage.path === "recovery")) labels.push("Recovered");
    if (stages.some(({ timing: stage }) => stage.delayed === true)) labels.push("Delayed");
    if (stages.some(({ timing: stage }) => stage.clock_uncertain === true)) labels.push("Clock uncertain");
    if (recovery && stages.length === 1) labels.push("1 stage recorded");

    const modelDurations = stages
      .map(({ timing: stage }) => Number(stage.model_duration_seconds))
      .filter((value) => Number.isFinite(value) && value >= 0);
    const ruleDurations = stages
      .map(({ timing: stage }) => Number(stage.rules_duration_seconds))
      .filter((value) => Number.isFinite(value) && value >= 0);
    const modelSummary = ruleEvaluation
      ? ruleDurations.length
        ? `Rule evaluation ${formatEvaluationDuration(ruleDurations.reduce((sum, value) => sum + value, 0))}`
        : "Rule evaluation not recorded"
      : modelDurations.length
        ? `Model evaluation ${formatEvaluationDuration(modelDurations.reduce((sum, value) => sum + value, 0))}`
        : "Model evaluation not recorded";

    const attempts = stages
      .map(({ timing: stage }) => Number(stage.attempts))
      .filter((value) => Number.isInteger(value) && value >= 1 && value <= 2)
      .reduce((sum, value) => sum + value, 0);
    if (attempts > 1) labels.push(`${Math.min(4, attempts)} attempts`);

    let latestPosted = null;
    stages.forEach(({ timing: stage }, index) => {
      const posted = Number(stage.posted_to_decision_seconds);
      if (!Number.isFinite(posted) || posted < 0) return;
      const decisionAt = new Date(stage.decision_at || "").getTime();
      const candidate = { posted, decisionAt: Number.isFinite(decisionAt) ? decisionAt : null, index };
      if (!latestPosted) {
        latestPosted = candidate;
        return;
      }
      if (candidate.decisionAt !== null && latestPosted.decisionAt !== null) {
        if (candidate.decisionAt >= latestPosted.decisionAt) latestPosted = candidate;
      } else if (candidate.decisionAt === null && latestPosted.decisionAt === null) {
        if (candidate.index >= latestPosted.index) latestPosted = candidate;
      } else if (candidate.decisionAt === null || latestPosted.decisionAt === null) {
        latestPosted = candidate;
      }
    });
    const posted = latestPosted
      ? `Posted → decision ${formatEvaluationDuration(latestPosted.posted)}`
      : "Posted → decision not recorded";
    append(trace, node("p", "evaluation-timing", [...labels, modelSummary, posted].join(" · ")));

    const stageSummaries = stages.map(({ kind, timing: stage }) => {
      const label = kind === "recovery" || stage.path === "recovery" ? "Recovery" : "Direct";
      const parts = [
        evaluationTimingPart(stage, "model_duration_seconds", "Evaluator"),
        evaluationTimingPart(stage, "rules_duration_seconds", "Rules"),
        evaluationTimingPart(stage, "interpretation_seconds", "Interpretation"),
        evaluationTimingPart(stage, "posted_to_decision_seconds", "Posted → decision"),
      ].filter(Boolean);
      return parts.length ? `${label} ${parts.join(" · ")}` : `${label} timing recorded`;
    });
    if (stageSummaries.length) append(trace, node("p", "evaluation-stages", `Stages ${stageSummaries.join(" · ")}`));

    const provider = firstValue(timing.evaluator, timing.provider, timing.evaluator_name, timing.path);
    const route = firstValue(timing.route);
    const fallback = firstValue(timing.fallback_reason);
    const model = firstValue(timing.model);
    const providerParts = [
      provider ? `Evaluator ${humanize(provider)}` : "",
      route ? `Route ${humanize(route)}` : "",
      model ? `Model ${safeString(model)}` : "",
      fallback ? `Fallback ${humanize(fallback)}` : "",
    ].filter(Boolean);
    if (providerParts.length) append(trace, node("p", "evaluation-provider", providerParts.join(" · ")));
    if (timing.jev_shadow_action) append(trace, node("p", "evaluation-provider", `JEV shadow ${humanize(timing.jev_shadow_action)} · ${timing.jev_shadow_agrees ? "agrees with Codex" : "differs from Codex"}`));

    const confidenceParts = [
      timing.semantic_confidence !== undefined ? `${route === "shadow" ? "JEV shadow semantic" : "Semantic"} ${formatEvaluationPercent(timing.semantic_confidence)}` : "",
      timing.allocation_confidence !== undefined ? `Allocation ${formatEvaluationPercent(timing.allocation_confidence)}` : "",
      timing.selected_probability !== undefined ? `Selected ${formatEvaluationPercent(timing.selected_probability)}` : "",
      timing.native_confidence !== undefined ? `Native ${formatEvaluationPercent(timing.native_confidence)}` : "",
      timing.eligibility_probability !== undefined ? `Eligibility ${formatEvaluationPercent(timing.eligibility_probability)}` : "",
    ].filter(Boolean);
    if (confidenceParts.length) append(trace, node("p", "evaluation-confidence", confidenceParts.join(" · ")));

    const detailParts = [
      evaluationTimingPart(timing, "queue_wait_seconds", "Queue"),
      evaluationTimingPart(timing, "posted_to_receipt_seconds", "Posted → received"),
      evaluationTimingPart(timing, "preparation_seconds", "Preparation"),
      evaluationTimingPart(timing, "jev_duration_seconds", "JEV"),
      evaluationTimingPart(timing, "codex_duration_seconds", "Codex"),
      evaluationTimingPart(timing, "interpretation_seconds", "Interpretation"),
      evaluationTimingPart(timing, "posted_to_interpretation_seconds", "Posted → interpretation"),
      evaluationTimingPart(timing, "execution_wait_seconds", "Execution wait"),
      evaluationTimingPart(timing, "execution_seconds", "Execution"),
      evaluationTimingPart(timing, "snapshot_seconds", "Snapshot"),
      evaluationTimingPart(timing, "quote_seconds", "Quote"),
      evaluationTimingPart(timing, "source_verification_seconds", "Source verification"),
      evaluationTimingPart(timing, "contract_resolution_seconds", "Contract resolution"),
      evaluationTimingPart(timing, "submission_seconds", "Submission"),
      evaluationTimingPart(timing, "received_to_submission_seconds", "Received → submission"),
      evaluationTimingPart(timing, "posted_to_submission_seconds", "Posted → submission"),
    ].filter(Boolean);
    if (timing.interpretation_ready_at) detailParts.push(`Ready ${formatDate(timing.interpretation_ready_at, true)}`);
    if (detailParts.length) append(trace, node("p", "evaluation-detail", detailParts.join(" · ")));

    if (!providerParts.length && !confidenceParts.length && !detailParts.length && !stageSummaries.length) {
      append(trace, node("p", "evaluation-detail", stateName === "evaluating" ? "Interpretation in progress." : "Interpretation ready; timing not recorded."));
    }
    append(trace, node("p", "evaluation-boundary", "Final broker result is recorded separately from model interpretation."));
    append(parent, trace);
  }

  function isRecoveryEvent(event) {
    const stateName = normalized(firstValue(event?.state, readDecision(event).state));
    const recovery = recoveryAssessment(event);
    return stateName.startsWith("recovery_") || Boolean(recovery && recovery.execution !== "exit");
  }

  function recoveryStatusLabel(value) {
    const statusName = normalized(value);
    return RECOVERY_STATUS_LABELS[statusName] || humanize(value, "Assessment unavailable");
  }

  function recoveryStatusPill(value) {
    return node("span", `status-pill ${classForState(value)}`, recoveryStatusLabel(value));
  }

  function recoveryFact(parent, label, value) {
    if (value === undefined || value === null || value === "") return;
    append(parent, node("div", "recovery-fact", `${label} / ${value}`));
  }

  function renderRecoveryAssessment(parent, event, recovery, stateValue) {
    const facts = recovery?.facts && typeof recovery.facts === "object" ? recovery.facts : {};
    const outcome = firstValue(recovery?.status, "");
    const heading = node("div", "recovery-heading");
    append(heading, node("span", "recovery-label", "Recovery assessment"));
    if (outcome) append(heading, recoveryStatusPill(outcome));
    append(parent, heading, node("p", "recovery-boundary", "Assessment only · no order submitted."));

    const assessedAt = firstValue(recovery?.evaluated_at, facts.evaluated_at);
    const originalTimestamp = firstValue(recovery?.original_timestamp, facts.original_timestamp);
    const age = firstValue(recovery?.signal_age_seconds, facts.signal_age_seconds);
    const timing = [
      assessedAt ? `Assessed ${formatDate(assessedAt, true)}` : "Assessment time unavailable",
      age === undefined || age === null ? "Signal age unavailable" : `Signal age ${formatDuration(age)}`,
      originalTimestamp ? `Original ${formatDate(originalTimestamp, true)}` : "Original time unavailable",
    ].join(" · ");
    append(parent, node("p", "recovery-meta", timing));
    renderEvaluationTiming(parent, event);

    const details = node("div", "recovery-facts");
    const quote = facts.quote && typeof facts.quote === "object" ? facts.quote : null;
    if (quote) {
      const quoteText = [
        formatContract(quote.contract),
        `Bid ${formatMoney(quote.bid)}`,
        `Ask ${formatMoney(quote.ask)}`,
        quote.tradable === true ? "Tradable" : quote.tradable === false ? "Not tradable" : "Tradability unknown",
        quote.timestamp ? `at ${formatDate(quote.timestamp, true)}` : "quote time unavailable",
      ].join(" · ");
      recoveryFact(details, "Quote at assessment", quoteText);
    }
    if (facts.market_open !== undefined) {
      recoveryFact(details, "Market", facts.market_open === true ? "Open" : facts.market_open === false ? "Closed" : "Unknown");
    }
    if (facts.equity !== undefined || facts.buying_power !== undefined || facts.affordable_quantity !== undefined) {
      const funds = [
        facts.equity !== undefined ? `Equity ${formatMoney(facts.equity)}` : "",
        facts.buying_power !== undefined ? `Buying power ${formatMoney(facts.buying_power)}` : "",
        facts.affordable_quantity !== undefined ? `Affordable quantity ${formatNumber(facts.affordable_quantity)}` : "",
      ].filter(Boolean).join(" · ");
      recoveryFact(details, "Account facts", funds);
    }
    if (facts.snapshot_timestamp) recoveryFact(details, "Snapshot", formatDate(facts.snapshot_timestamp, true));
    if (facts.context_truncated !== undefined) recoveryFact(details, "Observed context", facts.context_truncated ? "Truncated" : "Window not truncated; full history is unverified");
    if (facts.context_changed !== undefined) recoveryFact(details, "Context changed", facts.context_changed ? "Yes" : "No");
    if (details.hasChildNodes()) append(parent, details);

    const reason = firstValue(recovery?.reason, decisionReason(event));
    if (reason) append(parent, node("p", "decision-reason", valueText(reason)));

    const blockers = Array.isArray(facts.blockers) ? facts.blockers.filter((item) => typeof item === "string" && item.trim()) : [];
    if (blockers.length) {
      const block = node("div", "recovery-blockers");
      append(block, node("div", "evidence-label", "Blockers"));
      const list = node("ul", "recovery-list");
      blockers.forEach((item) => list.appendChild(node("li", "", item)));
      append(block, list);
      append(parent, block);
    }

    const evidence = Array.isArray(recovery?.evidence) ? recovery.evidence : [];
    if (evidence.length) {
      const block = node("div", "evidence-block");
      append(block, node("div", "evidence-label", "Evidence"));
      const list = node("ul", "recovery-list");
      evidence.forEach((entry) => {
        if (!entry || typeof entry !== "object") return;
        const id = firstValue(entry.message_id, "message unavailable");
        const quoteText = safeString(entry.quote, "");
        list.appendChild(node("li", "", `Message / ${shortId(id)}${quoteText ? ` · ${quoteText}` : ""}`));
      });
      if (list.hasChildNodes()) append(block, list);
      if (list.hasChildNodes()) append(parent, block);
    }
    if (!recovery && ["recovery_pending", "recovery_evaluating"].includes(normalized(stateValue))) {
      append(parent, node("p", "decision-reason", `${humanize(stateValue)}; assessment is still in progress.`));
    }
  }

  function renderRecoveryStateOptions() {
    const select = $("#message-state");
    if (!select) return;
    RECOVERY_STATES.forEach(([value, label]) => {
      if ([...select.options].some((option) => option.value === value)) return;
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    });
  }

  function getChannelMap() {
    const configured = Array.isArray(state.status?.configured_channels) ? state.status.configured_channels : [];
    const map = new Map();
    configured.forEach((channel) => {
      if (!channel) return;
      const id = firstValue(channel.id, channel.channel_id);
      if (id !== undefined) map.set(String(id), firstValue(channel.name, channel.label, String(id)));
    });
    [...state.data.messages, ...state.data.events].forEach((item) => {
      const id = firstValue(item?.channel_id, item?.channelId);
      if (id !== undefined && !map.has(String(id))) map.set(String(id), String(id));
    });
    return map;
  }

  function getOrdersForMessage(messageId) {
    const seen = new Set();
    return [...state.data.relationOrders, ...state.data.orders].filter((order) => {
      const orderId = String(firstValue(order?.id, order?.order_id, ""));
      const relatedMessageId = firstValue(order?.message_id, order?.messageId);
      if (!messageId || relatedMessageId === undefined || String(relatedMessageId) !== String(messageId) || seen.has(orderId)) return false;
      seen.add(orderId);
      return true;
    });
  }

  async function request(path, params = {}) {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
    });
    const url = query.toString() ? `${path}?${query.toString()}` : path;
    const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", signal: relayReadTimeoutSignal() });
    if (!response.ok) throw new ApiError(response.status);
    return response.json();
  }

  function resourceNodes(name) {
    return {
      loading: $(`[data-loading="${name}"]`),
      error: $(`[data-error="${name}"]`),
      empty: $(`[data-empty="${name}"]`),
      list: name === "messages" ? $("#message-list") : name === "orders" ? $("#order-list") : name === "positions" ? $("#position-list") : name === "account" ? $("#account-position-list") : $("#event-list"),
      errorMessage: $(`[data-error-message="${name}"]`),
    };
  }

  function beginResource(name) {
    if (state.loaded[name]) return;
    const nodes = resourceNodes(name);
    if (nodes.loading) nodes.loading.hidden = false;
    if (nodes.error) nodes.error.hidden = true;
    if (nodes.empty) nodes.empty.hidden = true;
  }

  function completeResource(name, hasRows) {
    const nodes = resourceNodes(name);
    state.loaded[name] = true;
    if (nodes.loading) nodes.loading.hidden = true;
    if (nodes.error) nodes.error.hidden = true;
    if (nodes.empty) nodes.empty.hidden = hasRows;
    if (nodes.list) nodes.list.hidden = !hasRows;
  }

  function failResource(name, error) {
    const nodes = resourceNodes(name);
    state.panelErrors.add(name);
    if (nodes.loading) nodes.loading.hidden = true;
    if (nodes.error) nodes.error.hidden = false;
    if (nodes.empty) nodes.empty.hidden = true;
    if (nodes.list) nodes.list.hidden = state.data[name]?.length === 0;
    if (nodes.errorMessage) nodes.errorMessage.textContent = apiErrorText(error);
  }

  function apiErrorText(error) {
    if (error instanceof ApiError) {
      if (error.status === 401 || error.status === 403) return "Authentication is required for this internal dashboard route.";
      if (error.status === 404) return "The dashboard endpoint is not available in this runtime.";
      if (error.status >= 500) return "The relay reported a server error while reading this panel.";
      return `The dashboard returned HTTP ${error.status}.`;
    }
    return "The dashboard could not be reached. Check the runtime and retry.";
  }

  function renderChannelOptions() {
    const select = $("#message-channel");
    if (!select) return;
    const current = select.value || state.filters.channelId;
    while (select.options.length > 1) select.remove(1);
    getChannelMap().forEach((label, id) => {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = label;
      select.appendChild(option);
    });
    if ([...select.options].some((option) => option.value === current)) select.value = current;
  }

  function renderMetrics() {
    const values = {
      messages: countFor("messages"),
      events: countFor("events"),
      held: countFor("held"),
      orders: countFor("orders"),
      positions: countFor("positions"),
    };
    Object.entries(values).forEach(([key, value]) => {
      const target = $(`[data-metric="${key}"]`);
      if (target) target.textContent = value === null ? "—" : formatNumber(value);
    });
  }

  function metricObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  }

  function metricNumber(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  }

  function evaluationMetricsRoot() {
    const status = state.status || {};
    const candidates = [
      status.evaluation_metrics,
      status.metrics?.evaluation,
      status.metrics?.evaluation_metrics,
      status.evaluation?.metrics,
      status.evaluation?.summary,
      status.evaluation_summary,
      state.evaluationMetrics,
      state.evaluationMetrics?.evaluation_metrics,
    ];
    return candidates.find((value) => metricObject(value)) || null;
  }

  function evaluationMetricsBucket(root) {
    if (!root) return null;
    const buckets = metricObject(root.buckets);
    const candidates = [
      root.recent,
      root.live,
      root.historical,
      root.recovery,
      buckets?.recent,
      buckets?.live,
      buckets?.historical,
      buckets?.recovery,
    ];
    return candidates.find((value) => metricObject(value)) || null;
  }

  function evaluationMetricsBucketName(root) {
    if (!root) return null;
    const buckets = metricObject(root.buckets);
    const candidates = [
      ["recent", root.recent],
      ["live", root.live],
      ["historical", root.historical],
      ["recovery", root.recovery],
      ["recent", buckets?.recent],
      ["live", buckets?.live],
      ["historical", buckets?.historical],
      ["recovery", buckets?.recovery],
    ];
    return candidates.find(([, value]) => metricObject(value))?.[0] || null;
  }

  function metricPercent(value) {
    const number = metricNumber(value);
    if (number === null) return "—";
    const ratio = number > 1 ? number / 100 : number;
    return `${Number((ratio * 100).toFixed(ratio * 100 < 10 ? 1 : 0))}%`;
  }

  function metricTiming(bucket, names) {
    const timings = metricObject(bucket?.timings) || {};
    for (const name of names) {
      const summary = metricObject(timings[name]) || metricObject(bucket?.[name]);
      if (summary) return summary;
    }
    return null;
  }

  function metricTimingValue(summary, key) {
    const value = metricNumber(summary?.[key]);
    return value === null ? "—" : formatEvaluationDuration(value);
  }

  function metricTimingTriplet(summary) {
    if (!summary) return "— / — / —";
    return ["p50", "p95", "p99"].map((key) => metricTimingValue(summary, key)).join(" / ");
  }

  function metricRatio(value, numerator, denominator) {
    const direct = metricNumber(value);
    if (direct !== null) return direct;
    const top = metricNumber(numerator);
    const bottom = metricNumber(denominator);
    return top !== null && bottom > 0 ? top / bottom : null;
  }

  function renderEvaluationMetrics() {
    const root = evaluationMetricsRoot();
    const bucket = evaluationMetricsBucket(root);
    const available = Boolean(bucket && Object.keys(bucket).length);
    const total = metricNumber(bucket?.total ?? bucket?.count);
    const evaluated = metricNumber(bucket?.evaluated);
    const eligible = metricNumber(bucket?.eligible);
    const fastPath = metricNumber(bucket?.fast_path);
    const direct = metricNumber(bucket?.direct);
    const jev = metricNumber(bucket?.jev);
    const fastCoverage = metricRatio(bucket?.coverage?.fast_path, fastPath, total);
    const eligibleCoverage = metricRatio(bucket?.coverage?.eligible, fastPath, eligible);
    const directCoverage = metricRatio(bucket?.coverage?.direct, direct, total);
    const jevCoverage = metricRatio(bucket?.coverage?.jev, jev, total);
    const fallback = metricNumber(bucket?.fallback);
    const failures = metricNumber(bucket?.failures);
    const timeouts = metricNumber(bucket?.timeouts);
    const interpretation = metricTiming(bucket, ["interpretation_seconds"]);
    const brokerAck = metricTiming(bucket, ["broker_ack_seconds"]);
    const fills = metricTiming(bucket, ["fill_seconds"]);

    setText("#evaluation-summary-count", total === null ? "—" : formatNumber(total));
    setText("#evaluation-summary-count-detail", available
      ? `${evaluated === null ? "—" : formatNumber(evaluated)} evaluated · ${eligible === null ? "—" : formatNumber(eligible)} eligible`
      : "Aggregate unavailable");
    setText("#evaluation-summary-fast-path", metricPercent(fastCoverage));
    setText("#evaluation-summary-fast-path-detail", available
      ? `Eligible coverage ${eligibleCoverage === null ? "—" : metricPercent(eligibleCoverage)} · Direct ${direct === null ? "—" : formatNumber(direct)} (${metricPercent(directCoverage)}) · JEV ${jev === null ? "—" : formatNumber(jev)} (${metricPercent(jevCoverage)})`
      : "Direct and JEV coverage unavailable");
    setText("#evaluation-summary-fallback", fallback === null ? "—" : formatNumber(fallback));
    setText("#evaluation-summary-fallback-detail", available
      ? `Errors ${failures === null ? "—" : formatNumber(failures)} · timeouts ${timeouts === null ? "—" : formatNumber(timeouts)}`
      : "No aggregate detail");
    setText("#evaluation-summary-interpretation", metricTimingTriplet(interpretation));
    setText("#evaluation-summary-interpretation-detail", interpretation ? "Seconds · p50 / p95 / p99" : "Interpretation timing unavailable");
    setText("#evaluation-summary-broker", `${metricTimingValue(brokerAck, "p50")} / ${metricTimingValue(fills, "p50")}`);
    setText("#evaluation-summary-broker-detail", brokerAck || fills
      ? `Ack p95/p99 ${metricTimingValue(brokerAck, "p95")} / ${metricTimingValue(brokerAck, "p99")} · fill p95/p99 ${metricTimingValue(fills, "p95")} / ${metricTimingValue(fills, "p99")}`
      : "Final broker timing unavailable");
    const window = firstValue(root?.window, root?.scope, bucket?.window, bucket?.scope, evaluationMetricsBucketName(root));
    const windowLabel = typeof window === "object" ? firstValue(window.label, window.name, window.limit && `last ${window.limit}`) : window;
    setText("#evaluation-summary-window", safeString(windowLabel, "RECENT").toUpperCase());
    setText("#evaluation-summary-detail", available
      ? `Bounded ${safeString(windowLabel, "recent")} aggregate · per-message interpretation traces remain authoritative for individual records.`
      : "Aggregate evaluation metrics unavailable; per-message interpretation traces remain available.");
  }

  function runtimePart(name) {
    return state.status?.runtime?.[name] || state.status?.[name] || {};
  }

  function stateLabel(part) {
    if (typeof part === "string") return humanize(part);
    return humanize(firstValue(part?.state, part?.status), "Unknown");
  }

  function renderStatus() {
    if (!state.status) return;
    const runtime = state.status.runtime || {};
    const updatedAt = firstValue(runtime.updated_at, state.status.updated_at);
    const timestamp = updatedAt ? new Date(updatedAt).getTime() : NaN;
    const stale = runtime.stale === true || !Number.isFinite(timestamp) || Date.now() - timestamp > STALE_AFTER_MS;
    const mode = safeString(firstValue(state.status.mode, state.status.execution_mode), "Unknown");
    const liveEnabled = state.status.live_orders_enabled === true;
    const killSwitch = state.status.kill_switch === true;
    const ledgerAvailable = state.status.ledger_available !== false;
    const discord = runtimePart("discord");
    const codex = runtimePart("codex");
    const broker = runtimePart("broker");
    const configured = Array.isArray(state.status.configured_channels) ? state.status.configured_channels : [];
    const runtimeChannels = Array.isArray(discord?.channels) ? discord.channels : [];
    const channelCount = configured.length || runtimeChannels.length;

    renderMetrics();
    renderEvaluationMetrics();
    renderChannelOptions();
    renderRecoveryStateOptions();
    setText("#runtime-updated", updatedAt ? `Runtime ${formatRelative(updatedAt)} · ${formatDate(updatedAt, true)}` : "Runtime timestamp unavailable");
    setText("#runtime-mode", mode.toUpperCase());
    setText("#runtime-mode-detail", `${liveEnabled ? "Live order gate flagged" : "Live order submission disabled"}${killSwitch ? " · kill switch active" : ""}`);
    setText("#runtime-discord", stateLabel(discord));
    setText("#runtime-discord-detail", firstValue(discord?.detail, `${channelCount} configured channel${channelCount === 1 ? "" : "s"}${stale ? " · runtime stale" : ""}`));
    setText("#runtime-codex", stateLabel(codex));
    setText("#runtime-codex-detail", firstValue(codex?.detail, ledgerAvailable ? "Subscription interpreter boundary" : "Ledger unavailable"));
    setText("#runtime-broker", stateLabel(broker));
    setText("#runtime-broker-detail", firstValue(broker?.detail, liveEnabled ? "Live order gate reported enabled by runtime" : "Shadow proposals only · no live orders"));

    const sidebarDot = $("#sidebar-status-dot");
    const sidebarLabel = $("#sidebar-status-label");
    const sidebarDetail = $("#sidebar-status-detail");
    if (sidebarDot) sidebarDot.className = `status-dot ${stale || runtime.state !== "running" ? "is-stale" : "is-healthy"}`;
    if (sidebarLabel) sidebarLabel.textContent = stale ? "Runtime stale" : stateLabel(runtime);
    if (sidebarDetail) sidebarDetail.textContent = stale ? `Last update ${formatRelative(updatedAt)} · check the relay.` : `${humanize(mode)} mode · ${killSwitch ? "kill switch active" : "kill switch clear"}`;
  }

  function setBanner(kind, message, meta) {
    const banner = $("#connection-banner");
    if (!banner) return;
    banner.className = `connection-banner banner banner-${kind}`;
    const mark = $("#connection-banner .banner-mark");
    if (mark) mark.textContent = kind === "healthy" ? "✓" : kind === "error" ? "!" : kind === "stale" ? "~" : "···";
    setText("#connection-message", message);
    setText("#connection-meta", meta);
  }

  function renderConnection() {
    if (state.statusError) {
      setBanner("error", "Dashboard API disconnected — panels may be stale.", "Retry available");
      const dot = $("#sidebar-status-dot");
      if (dot) dot.className = "status-dot is-error";
      setText("#sidebar-status-label", "Disconnected");
      setText("#sidebar-status-detail", "The dashboard API did not respond.");
      return;
    }
    if (!state.status) {
      setBanner("loading", "Connecting to the dashboard API…", "Initial load");
      return;
    }
    const updatedAt = firstValue(state.status.runtime?.updated_at, state.status.updated_at);
    const timestamp = updatedAt ? new Date(updatedAt).getTime() : NaN;
    const stale = state.status.runtime?.stale === true || !Number.isFinite(timestamp) || Date.now() - timestamp > STALE_AFTER_MS;
    if (state.panelErrors.size) {
      setBanner("error", `${state.panelErrors.size} dashboard panel${state.panelErrors.size === 1 ? " is" : "s are"} unavailable.`, "Retry available");
    } else if (stale) {
      setBanner("stale", "Runtime status is stale — recorded rows remain visible for inspection.", updatedAt ? `Last update ${formatRelative(updatedAt)}` : "No runtime timestamp");
    } else if (!state.autoRefresh) {
      setBanner("paused", "Auto-refresh paused by operator. Manual refresh remains available.", updatedAt ? `Last update ${formatRelative(updatedAt)}` : "Paused");
    } else if (state.status.runtime?.state !== "running") {
      setBanner("stale", state.status.runtime?.detail || `Relay ${stateLabel(state.status.runtime)} — recorded rows remain available.`, updatedAt ? `Last update ${formatRelative(updatedAt)}` : "Setup required");
    } else {
      setBanner("healthy", "Dashboard connected — monitoring relay state.", updatedAt ? `Last update ${formatRelative(updatedAt)}` : "Connected");
    }
  }

  function renderMessageImages(body, message) {
    const images = (Array.isArray(message.images) ? message.images : []).slice(0, 4)
      .filter((item) => typeof item?.url === "string" && item.url.startsWith("/api/message-image?"));
    if (!images.length) return;
    const key = `${message.id}:${message.revision}`;
    const details = node("details", "message-images");
    append(details, node("summary", "", `${images.length} image${images.length === 1 ? "" : "s"}`));
    const previews = node("div", "message-image-list");
    append(details, previews);
    const load = () => {
      if (previews.hasChildNodes()) return;
      images.forEach((item, index) => {
        const link = node("a", "message-image-link");
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        const caption = node("span", "record-meta", `Open image ${index + 1}`);
        append(link, caption);
        if (failedImagePreviews.has(item.url)) {
          caption.textContent = `Image ${index + 1} unavailable — open for details or retry`;
        } else {
          const image = node("img", "message-image");
          image.alt = `Attachment ${index + 1}`;
          image.loading = "lazy";
          image.decoding = "async";
          image.addEventListener("error", () => {
            failedImagePreviews.add(item.url);
            image.remove();
            caption.textContent = `Image ${index + 1} unavailable — open for details or retry`;
          });
          image.src = item.url;
          append(link, image);
        }
        append(previews, link);
      });
    };
    details.addEventListener("toggle", () => {
      if (details.open) {
        expandedImagePreviews.add(key);
        load();
      } else {
        expandedImagePreviews.delete(key);
      }
    });
    details.open = expandedImagePreviews.has(key);
    if (details.open) load();
    append(body, details);
  }

  function protectionText(evaluation) {
    if (!evaluation || typeof evaluation !== "object" || Array.isArray(evaluation)) return "";
    const status = normalized(evaluation.status);
    const price = firstValue(evaluation.stop_price, evaluation.requested_stop_price);
    if (!status && price === undefined) return "";
    const label = {
      shadow: "Protection shadow",
      pending: "Protection pending",
      active: "Protection active",
      complete: "Protection complete",
      blocked: "Protection blocked",
    }[status] || "Protection status unknown";
    let text = label;
    if (price !== undefined) text += ` at ${formatMoney(price)}`;
    if (status === "shadow") text += "; no order submitted";
    return text;
  }

  function renderProtection(parent, evaluation) {
    const text = protectionText(evaluation);
    if (text) append(parent, node("div", "record-meta", text));
  }

  function renderStopOrder(parent, order) {
    const orderType = normalized(order?.order_type);
    const price = firstValue(order?.stop_price, order?.requested_stop_price);
    if (!orderType && price === undefined) return;
    const label = orderType === "stop_market"
      ? "Native stop market order"
      : orderType === "market" ? "Protection market exit" : "Protection order";
    append(parent, node("div", "record-meta", `${label}${price !== undefined ? ` at ${formatMoney(price)}` : ""}`));
  }

  function renderMessages() {
    const list = $("#message-list");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    const channelMap = getChannelMap();
    state.data.messages.forEach((message) => {
      const card = node("article", "message-card");
      card.setAttribute("role", "listitem");
      const messageId = firstValue(message.id, message.message_id);
      const event = message.latest_event || message.latestEvent || null;
      const meta = node("div", "message-meta");
      const source = node("span", "source-chip", `SOURCE / ${firstValue(message.source_group, "Unassigned")}`);
      const channelId = firstValue(message.channel_id, message.channelId);
      const channel = node("span", "message-channel", `Channel / ${channelMap.get(String(channelId)) || safeString(channelId, "Unknown")}`);
      const author = message.author || {};
      const authorName = firstValue(author.name, message.author_name, author.username, author.id, "Unknown author");
      const authorNode = node("span", "message-author", `Author / ${authorName}`);
      const time = node("time", "message-time", formatDate(firstValue(message.timestamp, message.created_at)));
      const id = node("span", "message-id", `ID / ${shortId(messageId)}`);
      if (message.timestamp) time.dateTime = message.timestamp;
      append(meta, source, channel, authorNode, time, id);

      const body = node("div", "message-body");
      const content = safeString(message.content, "");
      if (content.trim()) append(body, node("p", "message-content", content));
      for (const embed of Array.isArray(message.embeds) ? message.embeds : []) {
        if (!embed || typeof embed !== "object") continue;
        const fields = Array.isArray(embed.fields) ? embed.fields : [];
        const parts = [embed.author?.name, embed.title, embed.description, embed.text,
          ...fields.flatMap((field) => [field?.name, field?.value]),
          embed.footer?.text || embed.footer?.name];
        const text = parts.filter((part) => typeof part === "string" && part.trim()).join("\n");
        if (text) append(body, node("div", "message-embed", text));
      }
      if (!body.hasChildNodes()) append(body, node("p", "message-content", "(No text content recorded)"));
      renderMessageImages(body, message);
      const revision = firstValue(message.revision, message.revision_id);
      if (revision !== undefined) append(body, node("p", "message-revision", `Revision ${revision}`));

      const decisionPanel = node("div", "message-decision");
      if (event) {
        const stateValue = firstValue(event.state, readDecision(event).state, "context");
        const header = node("div", "decision-header");
        const recovery = recoveryAssessment(event);
        append(header, decisionPill(stateValue), node("span", "decision-action", isRecoveryEvent(event) ? "Recovery assessment" : decisionActionLabel(event)));
        append(decisionPanel, header);
        if (isRecoveryEvent(event)) {
          renderRecoveryAssessment(decisionPanel, event, recovery, stateValue);
        } else {
          const reason = decisionReason(event);
          if (reason) append(decisionPanel, node("p", "decision-reason", valueText(reason)));
          const evidence = decisionEvidence(event);
          if (evidence) {
            const evidenceBlock = node("div", "evidence-block");
            append(evidenceBlock, node("div", "evidence-label", "Evidence"), node("p", "evidence-text", valueText(evidence)));
            append(decisionPanel, evidenceBlock);
          }
          const contract = firstValue(readDecision(event).contract, event.contract);
          if (contract) append(decisionPanel, node("p", "record-meta", `Contract / ${formatContract(contract)}`));
          renderEvaluationTiming(decisionPanel, event);
          renderProtection(decisionPanel, readDecision(event).stop_evaluation);
        }
      } else {
        append(decisionPanel, node("span", "no-decision", "No interpretation recorded"));
      }

      const ingestion = message.ingestion || {};
    if (ingestion.source && ingestion.source !== "unknown") {
      const cause = ingestion.reason === "edit" || ingestion.edited_timestamp ? "edit" : ingestion.kind;
      append(decisionPanel, node("p", "record-meta", `Received via ${humanize(ingestion.source)} / ${humanize(cause || "unknown")}`));
    }
    const previous = message.previous_evaluation;
    if (previous) {
      append(decisionPanel, node("p", "record-meta", `Previous revision: ${decisionActionLabel(previous)} / ${humanize(previous.state)}`));
      append(decisionPanel, node("p", "decision-reason", decisionReason(previous)));
      renderEvaluationTiming(decisionPanel, previous);
    }
    const related = getOrdersForMessage(messageId);
      if (related.length) {
        const relatedOrders = node("div", "related-orders");
        related.forEach((order) => {
          const filled = firstValue(order.filled_quantity, order.filledQuantity, 0);
          const quantity = firstValue(order.quantity, 0);
          const chip = node("span", "related-order");
          append(chip, node("strong", "", humanize(firstValue(order.status, "recorded"))), node("span", "", `${formatNumber(filled)} / ${formatNumber(quantity)} filled`));
          relatedOrders.appendChild(chip);
        });
        append(decisionPanel, relatedOrders);
      }
      append(card, meta, body, decisionPanel);
      list.appendChild(card);
    });
    setText("#messages-range", rangeText("messages"));
    renderPagination("messages");
  }

  function renderOrders() {
    const list = $("#order-list");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    state.data.orders.forEach((order) => {
      const row = node("article", "record-row");
      row.setAttribute("role", "listitem");
      const contract = node("div");
      append(contract, node("div", "record-title", formatContract(order.contract)), node("div", "record-meta", `${humanize(firstValue(order.action, order.side, "recorded"))} · ${humanize(firstValue(order.mode, "mode unavailable"))}`));
      const quantities = node("div", "record-detail");
      const evaluation = order.entry_evaluation;
      if (evaluation?.ask != null && evaluation?.ask_deviation_percent != null) {
        const deviation = Number(evaluation.ask_deviation_percent);
        if (Number.isFinite(deviation)) {
          append(contract, node("div", "record-meta", `Evaluated ask ${formatMoney(evaluation.ask)} · ${deviation >= 0 ? "+" : ""}${formatNumber(deviation)}% vs alert`));
        }
      }
      renderProtection(contract, order.stop_evaluation);
      if (!order.stop_evaluation) renderStopOrder(contract, order);
      const quantity = firstValue(order.quantity, 0);
      const filled = firstValue(order.filled_quantity, order.filledQuantity, 0);
      append(quantities, node("strong", "", `${formatNumber(filled)} / ${formatNumber(quantity)}`), node("div", "record-meta", "filled / requested"));
      const price = node("div", "record-detail");
      append(price, node("strong", "", formatMoney(firstValue(order.filled_notional, order.limit_price))), node("div", "record-meta", order.filled_notional !== undefined ? "filled notional" : "limit price"));
      const action = node("div", "record-action");
      append(action, statusPill(firstValue(order.status, "unknown")), node("div", "record-meta", `ID / ${shortId(firstValue(order.id, order.order_id))}`));
      append(row, contract, quantities, price, action);
      list.appendChild(row);
    });
    setText("#orders-range", rangeText("orders"));
    renderPagination("orders");
  }

  function renderPositions() {
    const list = $("#position-list");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    state.data.positions.forEach((position) => {
      const row = node("article", "record-row position-row");
      row.setAttribute("role", "listitem");
      const contract = node("div");
      append(contract, node("div", "record-title", formatContract(position.contract)), node("div", "record-meta", firstValue(position.source_group, "Source group unavailable")));
      renderProtection(contract, position.stop_evaluation);
      const price = node("div", "record-detail");
      append(price, node("strong", "", formatMoney(position.average_price)), node("div", "record-meta", "average recorded price"));
      const quantity = node("div", "position-quantity", formatNumber(position.quantity));
      append(row, contract, price, quantity);
      list.appendChild(row);
    });
  }

  function accountStatusClass(status, stale) {
    if (status === "error") return "is-error";
    if (status === "ready" && !stale) return "is-order";
    if (status === "unavailable" || stale) return "is-held";
    return "is-context";
  }

  function accountStatusLabel(status, stale) {
    if (status === "ready") return stale ? "Stale" : "Ready";
    if (status === "error") return "Error";
    if (status === "unavailable") return "Unavailable";
    return "Loading";
  }

  function accountAssetLabel(key) {
    return {
      equity_value: "Equities",
      options_value: "Options",
      futures_value: "Futures",
      event_contracts_value: "Event contracts",
      crypto_value: "Crypto",
      mutual_funds_value: "Mutual funds",
      fixed_income_value: "Fixed income",
    }[key] || humanize(key, "Other");
  }

  function renderAccountPositions(positions) {
    const list = $("#account-position-list");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    positions.forEach((position) => {
      const row = node("article", "record-row account-position-row");
      row.setAttribute("role", "listitem");
      const contract = node("div");
      const positionType = firstValue(position?.position_type, position?.side, "");
      const multiplierValue = Number(firstValue(position?.multiplier, null));
      const quoteTimestamp = firstValue(position?.quote_timestamp, position?.quote_time, position?.market_data?.timestamp, null);
      const metadata = [
        positionType ? humanize(positionType) : "Option position",
        Number.isFinite(multiplierValue) ? `Multiplier ×${formatNumber(multiplierValue)}` : "",
        quoteTimestamp ? `Quote ${formatDate(quoteTimestamp, true)}` : "",
      ].filter(Boolean).join(" · ");
      append(
        contract,
        node("div", "record-title", formatContract(position?.contract)),
        node("div", "record-meta", metadata),
      );
      const average = node("div", "record-detail");
      append(average, node("strong", "", formatMoney(position?.average_price)), node("div", "record-meta", "average price / unit"));
      const market = node("div", "record-detail");
      append(market, node("strong", "", formatMoney(position?.market_value)), node("div", "record-meta", "market value"));
      const quantity = node("div", "position-quantity");
      append(quantity, node("strong", "", formatNumber(position?.quantity)), node("div", "record-meta", "quantity"));
      append(row, contract, average, market, quantity);
      list.appendChild(row);
    });
  }

  function renderAccount() {
    const account = state.data.account && typeof state.data.account === "object" ? state.data.account : {};
    const status = safeString(account.status, state.loaded.account ? "unavailable" : "loading").toLowerCase();
    const available = account.available === true;
    const updatedAt = firstValue(account.updated_at, null);
    const attemptAt = firstValue(account.last_attempt_at, null);
    const updatedTimestamp = updatedAt ? new Date(updatedAt).getTime() : NaN;
    const stale = account.stale === true || (available && !Number.isFinite(updatedTimestamp));
    const statusNode = $("#account-status");
    if (statusNode) {
      statusNode.className = `status-pill ${accountStatusClass(status, stale)}`;
      statusNode.textContent = accountStatusLabel(status, stale);
    }
    setText("#account-updated", updatedAt
      ? `Updated ${formatRelative(updatedAt)}`
      : attemptAt
        ? `Last attempt ${formatRelative(attemptAt)}`
        : "Waiting for account snapshot");

    const equity = firstValue(account.equity, account.total_value, null);
    setText("#account-equity", formatMoney(equity));
    setText("#account-cash", formatMoney(account.cash));
    setText("#account-buying-power", formatMoney(account.buying_power));
    setText("#account-unleveraged-buying-power", formatMoney(account.unleveraged_buying_power));

    const detail = status === "error"
      ? safeString(account.error, "The cached Robinhood account snapshot could not be refreshed.")
      : status === "unavailable"
        ? safeString(account.error, "Robinhood account data is unavailable. The next scheduled refresh will retry.")
        : stale
          ? `Snapshot is stale${updatedAt ? `; last updated ${formatRelative(updatedAt)}` : "."}${attemptAt ? ` Last attempt ${formatRelative(attemptAt)}.` : ""}`
          : available
            ? "Cached broker values are ready."
            : "The cached Robinhood account snapshot is loading.";
    setText("#account-status-detail", detail);

    const assetContainer = $("#account-assets");
    const assetValues = $("#account-asset-values");
    const assets = account.asset_values && typeof account.asset_values === "object" ? account.asset_values : {};
    const assetEntries = Object.entries(assets).filter(([, value]) => value !== null && value !== undefined && value !== "");
    if (assetContainer && assetValues) {
      assetValues.replaceChildren(...assetEntries.map(([key, value]) => {
        const item = node("div", "account-asset-item");
        append(item, node("span", "field-label", accountAssetLabel(key)), node("strong", "", formatMoney(value)));
        return item;
      }));
      assetContainer.hidden = !assetEntries.length;
    }

    const positions = Array.isArray(account.positions) ? account.positions : [];
    setText("#account-position-count", state.loaded.account && available ? `${positions.length} option position${positions.length === 1 ? "" : "s"}` : "—");
    const nodes = resourceNodes("account");
    const errorFromPanel = state.panelErrors.has("account");
    const errorMessage = safeString(account.error, "The dashboard could not read the cached Robinhood account snapshot.");
    if (!state.loaded.account) return;
    if (nodes.loading) nodes.loading.hidden = true;
    if (errorFromPanel || (status === "error" && !available)) {
      if (nodes.error) nodes.error.hidden = false;
      if (nodes.errorMessage) nodes.errorMessage.textContent = errorMessage;
      if (nodes.empty) nodes.empty.hidden = true;
      if (nodes.list) nodes.list.hidden = true;
      return;
    }
    if (nodes.error) nodes.error.hidden = status !== "error";
    if (status === "error" && nodes.errorMessage) nodes.errorMessage.textContent = errorMessage;
    if (!available) {
      if (nodes.empty) nodes.empty.hidden = false;
      if (nodes.list) nodes.list.hidden = true;
      setText("#account-empty-title", "Account data unavailable");
      setText("#account-empty-detail", errorMessage || "No cached account snapshot is available yet.");
      return;
    }
    setText("#account-empty-title", "No option positions");
    setText("#account-empty-detail", "The cached account snapshot has no option positions to show.");
    if (nodes.empty) nodes.empty.hidden = positions.length > 0;
    if (nodes.list) nodes.list.hidden = positions.length === 0;
    if (positions.length) renderAccountPositions(positions);
  }

  function renderEvents() {
    const list = $("#event-list");
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);
    state.data.events.forEach((event) => {
      const row = node("article", "event-row");
      row.setAttribute("role", "listitem");
      const stateNode = node("div");
      const stateValue = firstValue(event.state, readDecision(event).state, "context");
      const recovery = recoveryAssessment(event);
      const eventId = firstValue(event.message_id, event.messageId);
      const sourceLabel = String(eventId).startsWith("expiry:") ? "Expiry policy" : "Message";
      append(stateNode, decisionPill(stateValue), node("div", "event-message-id", `${sourceLabel} / ${shortId(eventId)}`));
      const reason = node("p", "event-reason", valueText(firstValue(recovery?.reason, event.reason, decisionReason(event), "No reason recorded.")));
      const action = node("div", "event-action", isRecoveryEvent(event)
        ? `Recovery assessment${recovery?.status ? ` · ${recoveryStatusLabel(recovery.status)}` : ""}`
        : decisionActionLabel(event));
      const created = node("time", "event-time", formatDate(firstValue(event.created_at, event.timestamp)));
      if (event.created_at) created.dateTime = event.created_at;
      append(row, stateNode, reason, action, created);
      renderEvaluationTiming(row, event);
      renderProtection(row, readDecision(event).stop_evaluation);
      list.appendChild(row);
    });
    setText("#events-range", rangeText("events"));
    renderPagination("events");
  }

  function rangeText(name) {
    const page = state.pages[name];
    const items = state.data[name] || [];
    if (!items.length) return "0 rows";
    const start = page.offset + 1;
    const end = page.offset + items.length;
    return `${start}–${end}${page.nextOffset !== null ? "+" : ""}`;
  }

  function renderPagination(name) {
    const pagination = $(`#${name}-pagination`);
    if (!pagination) return;
    const page = state.pages[name];
    const hasRows = (state.data[name] || []).length > 0;
    pagination.hidden = !hasRows && page.page === 1;
    setText(`#${name}-page-label`, `Page ${page.page}`);
    const previous = $(`[data-page="${name}-prev"]`);
    const next = $(`[data-page="${name}-next"]`);
    if (previous) previous.disabled = page.page <= 1;
    if (next) next.disabled = page.nextOffset === null || page.nextOffset === undefined;
  }

  async function loadStatus() {
    try {
      state.status = await request(API.status);
      state.statusError = null;
      renderStatus();
      return true;
    } catch (error) {
      state.statusError = error;
      renderConnection();
      return false;
    }
  }

  async function loadEvaluationMetrics() {
    try {
      state.evaluationMetrics = await request(API.evaluationMetrics);
      state.evaluationMetricsError = null;
      renderEvaluationMetrics();
      return true;
    } catch (error) {
      state.evaluationMetricsError = error;
      renderEvaluationMetrics();
      return false;
    }
  }

  async function loadMessages() {
    const page = state.pages.messages;
    beginResource("messages");
    try {
      const payload = await request(API.messages, {
        limit: PAGE_SIZE,
        offset: page.offset,
        q: state.filters.q,
        channel_id: state.filters.channelId,
        state: state.filters.state,
      });
      state.data.messages = Array.isArray(payload?.items) ? payload.items : [];
      page.nextOffset = payload?.next_offset ?? null;
      state.panelErrors.delete("messages");
      completeResource("messages", state.data.messages.length > 0);
      renderMessages();
      return true;
    } catch (error) {
      failResource("messages", error);
      renderConnection();
      return false;
    }
  }

  async function loadOrders() {
    const page = state.pages.orders;
    beginResource("orders");
    try {
      const payload = await request(API.orders, { limit: PAGE_SIZE, offset: page.offset, status: state.orderStatus });
      state.data.orders = Array.isArray(payload?.items) ? payload.items : [];
      page.nextOffset = payload?.next_offset ?? null;
      state.panelErrors.delete("orders");
      completeResource("orders", state.data.orders.length > 0);
      renderOrders();
      renderMessages();
      return true;
    } catch (error) {
      failResource("orders", error);
      renderConnection();
      return false;
    }
  }

  async function loadRelationshipOrders() {
    try {
      const payload = await request(API.orders, { limit: 200, offset: 0 });
      state.data.relationOrders = Array.isArray(payload?.items) ? payload.items : [];
      renderMessages();
    } catch {
      state.data.relationOrders = [];
    }
  }

  async function loadPositions() {
    beginResource("positions");
    try {
      const payload = await request(API.positions);
      state.data.positions = Array.isArray(payload?.items) ? payload.items : [];
      state.panelErrors.delete("positions");
      completeResource("positions", state.data.positions.length > 0);
      renderPositions();
      return true;
    } catch (error) {
      failResource("positions", error);
      renderConnection();
      return false;
    }
  }

  async function loadAccount() {
    beginResource("account");
    try {
      const payload = await request(API.account);
      state.data.account = payload && typeof payload === "object" ? payload : {};
      state.loaded.account = true;
      state.panelErrors.delete("account");
      renderAccount();
      return true;
    } catch (error) {
      state.data.account = { available: false, status: "error", error: apiErrorText(error) };
      state.loaded.account = true;
      state.panelErrors.add("account");
      renderAccount();
      renderConnection();
      return false;
    }
  }

  async function loadEvents() {
    const page = state.pages.events;
    beginResource("events");
    try {
      const payload = await request(API.events, { limit: PAGE_SIZE, offset: page.offset });
      state.data.events = Array.isArray(payload?.items) ? payload.items : [];
      page.nextOffset = payload?.next_offset ?? null;
      state.panelErrors.delete("events");
      completeResource("events", state.data.events.length > 0);
      renderEvents();
      return true;
    } catch (error) {
      failResource("events", error);
      renderConnection();
      return false;
    }
  }

  async function loadAll() {
    if (state.refreshing) return;
    state.refreshing = true;
    state.cycle += 1;
    const cycle = state.cycle;
    state.panelErrors.clear();
    setText("#last-sync", "Syncing…");
    renderConnection();
    try {
      const results = await Promise.allSettled([
        loadStatus(),
        loadEvaluationMetrics(),
        loadMessages(),
        loadOrders(),
        loadRelationshipOrders(),
        loadPositions(),
        loadAccount(),
        loadEvents(),
      ]);
      if (cycle === state.cycle) {
        state.lastSync = new Date();
        setText("#last-sync", `Synced ${formatRelative(state.lastSync)}`);
        renderConnection();
      }
      return results;
    } finally {
      state.refreshing = false;
    }
  }

  async function reloadResource(name) {
    state.panelErrors.delete(name);
    if (name === "messages") await loadMessages();
    if (name === "orders") await loadOrders();
    if (name === "positions") await loadPositions();
    if (name === "account") await loadAccount();
    if (name === "events") await loadEvents();
    renderConnection();
  }

  async function changePage(name, direction) {
    const page = state.pages[name];
    if (direction === "next" && page.nextOffset !== null && page.nextOffset !== undefined) {
      page.offset = page.nextOffset;
      page.page += 1;
    } else if (direction === "prev" && page.page > 1) {
      page.offset = Math.max(0, page.offset - PAGE_SIZE);
      page.page -= 1;
    } else {
      return;
    }
    await reloadResource(name);
  }

  function resetPage(name) {
    state.pages[name].offset = 0;
    state.pages[name].nextOffset = null;
    state.pages[name].page = 1;
  }

  function scheduleRefresh() {
    if (state.refreshTimer) window.clearInterval(state.refreshTimer);
    state.refreshTimer = state.autoRefresh ? window.setInterval(loadAll, 20_000) : null;
    renderConnection();
  }

  function bindEvents() {
    renderRecoveryStateOptions();
    $("#refresh-button")?.addEventListener("click", loadAll);
    $("#auto-refresh")?.addEventListener("change", (event) => {
      state.autoRefresh = event.target.checked;
      scheduleRefresh();
    });

    $("#message-filters")?.addEventListener("submit", (event) => {
      event.preventDefault();
      state.filters.q = $("#message-search")?.value.trim() || "";
      state.filters.channelId = $("#message-channel")?.value || "";
      state.filters.state = $("#message-state")?.value || "";
      resetPage("messages");
      reloadResource("messages");
    });

    $("#clear-filters")?.addEventListener("click", () => {
      state.filters = { q: "", channelId: "", state: "" };
      $("#message-search").value = "";
      $("#message-channel").value = "";
      $("#message-state").value = "";
      resetPage("messages");
      reloadResource("messages");
    });

    $("#apply-order-filter")?.addEventListener("click", () => {
      state.orderStatus = $("#order-status")?.value || "";
      resetPage("orders");
      reloadResource("orders");
    });

    $$('[data-retry]').forEach((button) => button.addEventListener("click", () => reloadResource(button.dataset.retry)));
    $$('[data-page]').forEach((button) => button.addEventListener("click", () => {
      const [name, direction] = button.dataset.page.split("-");
      changePage(name, direction);
    }));

    const sections = $$("main section[id]");
    const links = $$(".side-nav-link");
    if ("IntersectionObserver" in window) {
      const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          links.forEach((link) => link.classList.toggle("is-active", link.getAttribute("href") === `#${entry.target.id}`));
        });
      }, { rootMargin: "-30% 0px -60% 0px", threshold: 0 });
      sections.forEach((section) => observer.observe(section));
    }
  }

  bindEvents();
  scheduleRefresh();
  loadAll();
})();

/* Setup controls intentionally live outside the monitoring module. They use
 * the fixed setup API and keep the ledger rendering independent. */
(function setupUi() {
  "use strict";

  const BROWSER_LOGIN_URL = "/browser/vnc.html?autoconnect=true&resize=scale&path=browser/websockify";
  const setupState = {
    csrfToken: "",
    status: null,
    statusError: "",
    providerError: { discord: "", codex: "", robinhood: "" },
    dirtyChannels: false,
    loadedChannels: false,
    authActive: { codex: false, robinhood: false },
    robinhoodCallbackInFlight: false,
    robinhoodCallbackAuthorizationUrl: "",
    dialogProvider: "discord",
    dialogReturnFocus: null,
    pollTimer: null,
    requestInFlight: false,
    channelSaveInFlight: false,
    notificationsDirty: false,
    notificationsSaveInFlight: false,
    expiryPolicyInFlight: false,
    expiryPolicyDirty: false,
    evaluationDirty: false,
    evaluationJevDirty: false,
    evaluationInFlight: false,
    evaluationTestInFlight: false,
    modeChangeInFlight: false,
    modeReturnFocus: null,
    discoveryRequestInFlight: false,
    discoveryVersion: 0,
    statusReadSequence: 0,
    discoveryPreviousRequestId: "",
    discoveryPendingRequestId: "",
    discoveryAutoAttempted: false,
    discovery: {
      state: "idle",
      requestId: "",
      guildId: "",
      channelId: "",
      detail: "",
      authorsLimited: false,
      guilds: new Map(),
      channels: new Map(),
      authors: new Map(),
      authorsByChannel: new Map(),
    },
    discordDirty: false,
    discordSaveInFlight: false,
  };

  const setupById = (id) => document.getElementById(id);
  const setupText = (value, fallback = "") => {
    if (value === null || value === undefined) return fallback;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
    return fallback;
  };
  const setupFirst = (...values) => values.find((value) => value !== null && value !== undefined && value !== "");
  const setupStateName = (value) => setupText(value, "unknown").toLowerCase().replace(/[-\s]+/g, "_");
  const setupPart = (status, name) => status && typeof status[name] === "object" ? status[name] : {};
  const setupUnwrap = (payload) => {
    if (!payload || typeof payload !== "object") return {};
    if (payload.csrf_token) setupState.csrfToken = setupText(payload.csrf_token);
    return payload;
  };
  const setupStatusValue = (part, fallback = "not_connected") => setupStateName(setupFirst(part?.state, fallback));
  const setupStatusLabel = (value, provider = "") => {
    const labels = {
      setup_required: "Setup required",
      not_connected: "Not connected",
      starting: "Starting",
      waiting: "Waiting",
      ready: "Ready",
      connected: "Connected",
      existing_connected: "Connected",
      failed: "Failed",
      cancelled: "Cancelled",
      canceled: "Cancelled",
      paused: "Paused",
      stopped: "Stopped",
      running: "Running",
      pending_reload: "Pending reload",
      unpaused: "Unpaused",
      unknown: "Unknown",
      login_required: "Login required",
      configured: "Configured",
      not_configured: "Not configured",
      enabled: "Enabled",
      disabled: "Disabled",
      pending: "Pending",
      sent: "Sent",
      unavailable: "Unavailable",
      needs_attention: "Needs attention",
    };
    if (provider === "discord" && ["connected", "existing_connected"].includes(setupStateName(value))) return "Signed in";
    return labels[setupStateName(value)] || setupText(value, "Unknown");
  };
  const setupStatusClass = (value) => {
    const stateName = setupStateName(value);
    if (["connected", "existing_connected", "configured", "enabled", "sent"].includes(stateName)) return "is-order";
    if (["failed", "error", "unavailable", "needs_attention", "auth_error"].includes(stateName)) return "is-error";
    if (["starting", "waiting", "paused", "stopped", "pending_reload", "pending"].includes(stateName)) return "is-held";
    return "is-context";
  };
  const setupDetail = (part, fallback) => {
    const failure = part?.failure;
    const diagnostic = failure && typeof failure === "object" ? [
      failure.phase && `Phase: ${setupText(failure.phase)}`,
      failure.code && `Code: ${setupText(failure.code)}`,
      failure.type && `Error: ${setupText(failure.type)}`,
      Number.isInteger(failure.http_status) && `HTTP: ${failure.http_status}`,
      failure.source && `Source: ${setupText(failure.source)}${Number.isInteger(failure.line) ? `:${failure.line}` : ""}`,
    ].filter(Boolean).join(" · ") : "";
    return [setupText(part?.detail, fallback), diagnostic].filter(Boolean).join(" ");
  };
  const setupTradingStatus = (status) => {
    const trading = status && typeof status.trading === "object" ? status.trading : {};
    const mode = setupStateName(setupFirst(trading.mode, status?.mode, "unknown"));
    const workerModeValue = setupFirst(trading.worker_mode, "");
    return {
      mode,
      workerMode: workerModeValue ? setupStateName(workerModeValue) : "",
      liveEnabled: trading.live_enabled === true || status?.live_orders_enabled === true,
      pending: trading.pending === true,
    };
  };
  const setupTradingModeLabel = (mode) => ({ shadow: "Shadow", live: "Live", paper: "Paper" }[mode] || setupText(mode, "Unknown"));
  const setupLiveReady = (status) => status?.configured === true && setupStatusValue(setupPart(status, "discord")) === "connected";
  const setupTradingExplanation = (mode) => {
    if (mode === "live") return "Live can send real Robinhood orders.";
    if (mode === "shadow") return "Shadow records proposed orders and never sends them.";
    if (mode === "paper") return "Paper records simulated fills without sending broker orders.";
    return "Choose Shadow or Live after setup is complete.";
  };
  const setupTradingDetail = (status, trading) => {
    const configured = setupTradingModeLabel(trading.mode);
    const worker = trading.workerMode ? setupTradingModeLabel(trading.workerMode) : "not reported";
    const lifecycle = status?.paused ? (trading.pending ? "Paused · pending reload." : "Paused.") : "Unpaused.";
    const alignment = `Configured ${configured}; worker ${worker}.`;
    const pending = trading.pending ? " Wait for the worker to acknowledge the selected mode before using Resume relay." : "";
    return `${lifecycle} ${alignment}${pending} ${setupTradingExplanation(trading.mode)}`;
  };
  const setupAccountLastFour = (part) => {
    const account = setupFirst(part?.last_four, part?.account);
    if (account && typeof account === "object") return setupAccountLastFour(account);
    const text = setupText(account, "").replace(/\D/g, "");
    return text ? text.slice(-4) : "";
  };
  const setupSetText = (id, value, fallback = "") => {
    const target = setupById(id);
    if (target) target.textContent = setupText(value, fallback);
  };
  const setupSetStatus = (id, value, provider = "") => {
    const target = setupById(id);
    if (!target) return;
    target.classList.remove("is-context", "is-held", "is-order", "is-error", "is-filled", "is-open", "is-pending", "is-unknown");
    target.classList.add(setupStatusClass(value));
    target.textContent = setupStatusLabel(value, provider);
  };
  const setupSetNotice = (kind, message) => {
    const notice = setupById("setup-notice");
    const mark = notice?.querySelector(".setup-notice-mark");
    if (notice) notice.className = `setup-notice setup-notice-${kind}`;
    if (mark) mark.textContent = kind === "healthy" ? "✓" : kind === "error" ? "!" : kind === "stale" ? "~" : "···";
    setupSetText("setup-notice-text", message);
  };
  const setupJson = async (path, options = {}) => {
    const method = options.method || "GET";
    const headers = { Accept: "application/json" };
    const request = { method, headers, cache: "no-store" };
    if (method === "GET") request.signal = relayReadTimeoutSignal();
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      request.body = JSON.stringify(options.body);
    }
    if (method !== "GET" && setupState.csrfToken) headers["X-Relay-CSRF"] = setupState.csrfToken;
    const response = await fetch(path, request);
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      const errorValue = typeof payload?.error === "object" ? payload.error?.detail : payload?.error;
      const error = new Error(setupText(errorValue, setupText(payload?.detail, `HTTP ${response.status}`)));
      error.status = response.status;
      throw error;
    }
    return payload;
  };
  const setupChannels = (status) => Array.isArray(status?.channels) ? status.channels : [];
  const setupPollSeconds = (status) => {
    const value = status?.poll_seconds;
    const number = Number(value);
    return Number.isFinite(number) && number >= 2 ? Math.min(number, 60) : 3;
  };
  const setupChannelRows = () => [...document.querySelectorAll(".channel-editor")];
  const setupField = (row, name) => row?.querySelector(`[data-channel-field="${name}"]`);
  const setupChannelId = (value) => setupText(value, "").trim();
  const setupSnowflake = (value) => /^\d{15,22}$/.test(setupChannelId(value));
  const setupChannelKey = (guildId, channelId) => `${setupChannelId(guildId)}:${setupChannelId(channelId)}`;
  const setupParseChannelUrl = (value) => {
    try {
      const url = new URL(setupText(value), window.location.origin);
      if (url.protocol !== "https:" || url.hostname !== "discord.com") return {};
      const match = url.pathname.match(/^\/channels\/(\d{15,22})\/(\d{15,22})\/?$/);
      if (!match) return {};
      return { guildId: match[1], channelId: match[2], url: `https://discord.com/channels/${match[1]}/${match[2]}` };
    } catch {
      return {};
    }
  };
  const setupFixedChannelUrl = (guildId, channelId, value = "") => {
    const parsed = setupParseChannelUrl(value);
    if (parsed.guildId === setupChannelId(guildId) && parsed.channelId === setupChannelId(channelId)) return parsed.url;
    if (setupSnowflake(guildId) && setupSnowflake(channelId)) return `https://discord.com/channels/${guildId}/${channelId}`;
    return "";
  };
  const setupDiscoveryPart = (status = setupState.status) => {
    const discord = setupPart(status, "discord");
    return discord.discovery && typeof discord.discovery === "object" ? discord.discovery : {};
  };
  const setupMergeDiscovery = (discovery) => {
    const value = discovery && typeof discovery === "object" ? discovery : {};
    const incomingRequestId = setupText(value.request_id, "");
    const pendingRequestId = setupState.discoveryPendingRequestId;
    if (setupState.discoveryRequestInFlight && !incomingRequestId) return false;
    if (setupState.discoveryRequestInFlight && incomingRequestId && setupState.discoveryPreviousRequestId && incomingRequestId === setupState.discoveryPreviousRequestId) return false;
    if (pendingRequestId && incomingRequestId && incomingRequestId !== pendingRequestId) return false;
    if (pendingRequestId && !incomingRequestId && setupState.discovery.state === "waiting") return false;
    const stateName = setupStateName(setupFirst(value.state, "idle"));
    setupState.discovery.state = stateName;
    if (Object.prototype.hasOwnProperty.call(value, "request_id")) setupState.discovery.requestId = incomingRequestId;
    if (Object.prototype.hasOwnProperty.call(value, "guild_id")) setupState.discovery.guildId = setupChannelId(value.guild_id);
    if (Object.prototype.hasOwnProperty.call(value, "channel_id")) setupState.discovery.channelId = setupChannelId(value.channel_id);
    setupState.discovery.detail = setupText(value.detail, "");
    if (value.authors_limited !== undefined) setupState.discovery.authorsLimited = value.authors_limited === true;
    if (pendingRequestId && incomingRequestId === pendingRequestId && stateName !== "waiting") setupState.discoveryPendingRequestId = "";

    if (Array.isArray(value.guilds)) {
      value.guilds.forEach((guild) => {
        if (!guild || !setupSnowflake(guild.id)) return;
        const id = setupChannelId(guild.id);
        setupState.discovery.guilds.set(id, {
          id,
          name: setupText(setupFirst(guild.name, guild.label), `Server ${id}`),
        });
      });
    }
    if (Array.isArray(value.channels)) {
      value.channels.forEach((channel) => {
        if (!channel || !setupSnowflake(channel.id) || !setupSnowflake(channel.guild_id)) return;
        const id = setupChannelId(channel.id);
        const guildId = setupChannelId(channel.guild_id);
        setupState.discovery.channels.set(id, {
          id,
          guild_id: guildId,
          name: setupText(setupFirst(channel.name, channel.label), `Channel ${id}`),
          url: setupFixedChannelUrl(guildId, id, setupFirst(channel.url, channel.href, "")),
        });
        if (!setupState.discovery.guilds.has(guildId)) {
          setupState.discovery.guilds.set(guildId, { id: guildId, name: `Server ${guildId}` });
        }
      });
    }
    if (Array.isArray(value.authors)) {
      const key = setupChannelKey(setupState.discovery.guildId, setupState.discovery.channelId);
      const byChannel = setupState.discovery.authorsByChannel.get(key) || new Map();
      value.authors.forEach((author) => {
        if (!author || !setupSnowflake(author.id)) return;
        const id = setupChannelId(author.id);
        const item = { id, name: setupText(setupFirst(author.name, author.username, author.label), `Author ${id}`) };
        setupState.discovery.authors.set(id, item);
        byChannel.set(id, item);
      });
      if (key !== ":") setupState.discovery.authorsByChannel.set(key, byChannel);
    }
    return true;
  };
  const setupCurrentRowBinding = (row) => {
    const url = setupText(setupField(row, "url")?.value, "").trim();
    const parsed = setupParseChannelUrl(url);
    const guildId = setupChannelId(setupFirst(setupField(row, "guild_id")?.value, parsed.guildId, ""));
    const channelId = setupChannelId(setupFirst(setupField(row, "channel_id")?.value, parsed.channelId, ""));
    return { guildId, channelId, url, parsed };
  };
  const setupSelectedAuthorIds = (row) => {
    const select = setupField(row, "author_choices");
    const selected = select ? [...select.selectedOptions].map((option) => setupChannelId(option.value)) : [];
    const manual = setupText(setupField(row, "authors")?.value, "").split(/[\s,]+/).map((id) => id.trim()).filter(Boolean);
    return [...new Set([...selected, ...manual])];
  };
  const setupSetFeedback = (id, message, kind = "") => {
    const target = setupById(id);
    if (!target) return;
    target.textContent = setupText(message, "");
    target.classList.toggle("is-error", kind === "error");
    target.classList.toggle("is-success", kind === "success");
  };
  const setupOption = (value, label) => new Option(setupText(label, value), setupChannelId(value));
  const setupSorted = (items) => [...items].sort((left, right) => setupText(left.name).localeCompare(setupText(right.name), undefined, { sensitivity: "base" }));
  const setupRenderAuthorChoices = (row, binding) => {
    const select = setupField(row, "author_choices");
    if (!select) return;
    const selected = new Set([...select.selectedOptions].map((option) => setupChannelId(option.value)));
    const manual = setupText(setupField(row, "authors")?.value, "").split(/[\s,]+/).map((id) => id.trim()).filter(Boolean);
    const known = setupState.discovery.authorsByChannel.get(setupChannelKey(binding.guildId, binding.channelId)) || new Map();
    const items = new Map(known);
    [...selected, ...manual].forEach((id) => {
      if (setupSnowflake(id) && !items.has(id)) items.set(id, setupState.discovery.authors.get(id) || { id, name: `Saved author ${id}` });
    });
    select.textContent = "";
    setupSorted(items.values()).forEach((author) => {
      const option = setupOption(author.id, `${author.name} · ${author.id}`);
      option.selected = selected.has(author.id);
      select.appendChild(option);
    });
    if (!items.size) {
      const option = setupOption("", "No observed authors yet");
      option.disabled = true;
      select.appendChild(option);
    }
  };
  const setupRenderRowOptions = (row) => {
    if (!row) return;
    const guildField = setupField(row, "guild_id");
    const channelField = setupField(row, "channel_id");
    const restrictField = setupField(row, "restrict_authors");
    const panel = row.querySelector("[data-author-panel]");
    const binding = setupCurrentRowBinding(row);
    const busy = setupState.discoveryRequestInFlight || setupState.discovery.state === "waiting";
    const selectedGuild = setupChannelId(guildField?.value || binding.guildId);
    const selectedChannel = setupChannelId(channelField?.value || binding.channelId);
    const guilds = setupSorted(setupState.discovery.guilds.values());
    const previousGuild = selectedGuild;
    if (guildField) {
      guildField.textContent = "";
      guildField.appendChild(new Option("Select a server…", ""));
      guilds.forEach((guild) => guildField.appendChild(setupOption(guild.id, guild.name)));
      if (previousGuild && !guilds.some((guild) => guild.id === previousGuild)) {
        guildField.appendChild(setupOption(previousGuild, `Saved server · ${previousGuild}`));
      }
      guildField.value = previousGuild;
      guildField.disabled = busy;
    }
    const channels = setupSorted([...setupState.discovery.channels.values()].filter((channel) => channel.guild_id === previousGuild));
    if (channelField) {
      channelField.textContent = "";
      channelField.appendChild(new Option(previousGuild ? "Select a channel…" : "Select a server first", ""));
      channels.forEach((channel) => channelField.appendChild(setupOption(channel.id, channel.name)));
      if (selectedChannel && !channels.some((channel) => channel.id === selectedChannel)) {
        channelField.appendChild(setupOption(selectedChannel, `Saved channel · ${selectedChannel}`));
      }
      channelField.value = selectedChannel;
      channelField.disabled = busy || !previousGuild;
    }
    const refresh = row.querySelector(".discovery-refresh-channel");
    if (refresh) refresh.disabled = busy || !previousGuild;
    setupRenderAuthorChoices(row, { guildId: previousGuild, channelId: selectedChannel });
    const restricted = Boolean(restrictField?.checked);
    if (panel) panel.hidden = !restricted;
    if (restrictField) restrictField.disabled = busy || !selectedChannel;
    const authorSelect = setupField(row, "author_choices");
    const authorInput = setupField(row, "authors");
    if (authorSelect) authorSelect.disabled = busy || !restricted || !selectedChannel;
    if (authorInput) authorInput.disabled = busy || !restricted;
    const authorNote = row.querySelector("[data-author-note]");
    if (authorNote) authorNote.textContent = setupState.discovery.authorsLimited
      ? "Observed authors are a partial list. Add a manual Discord ID for anyone missing from the browser result."
      : "Choose observed authors, or add manual Discord IDs when the browser result is incomplete.";
    const url = setupField(row, "url");
    if (url) url.disabled = busy;
    row.classList.toggle("is-discovery-busy", busy);
    row.setAttribute("aria-busy", busy ? "true" : "false");
  };
  const setupRenderDiscovery = (discovery) => {
    if (discovery !== setupState.discovery) setupMergeDiscovery(discovery);
    const stateName = setupState.discovery.state;
    const busy = setupState.discoveryRequestInFlight || stateName === "waiting";
    const toolbar = document.querySelector(".discovery-toolbar");
    if (toolbar) toolbar.setAttribute("aria-busy", busy ? "true" : "false");
    setupSetStatus("discord-discovery-state", stateName, "discord");
    const detail = setupState.discovery.detail || (
      stateName === "idle" ? "Sign in to Discord, then refresh servers to load the directory." :
      stateName === "waiting" ? "Discord discovery is running…" :
      stateName === "ready" ? "Directory loaded. Choose a server to continue." :
      stateName === "login_required" ? "Sign in to Discord in the browser before discovering servers." :
      stateName === "failed" ? "Discovery failed. Retry when the browser session is ready." :
      "Discord discovery is ready.");
    setupSetText("discord-discovery-detail", detail);
    const count = setupState.discovery.guilds.size ? `${setupState.discovery.guilds.size} server${setupState.discovery.guilds.size === 1 ? "" : "s"} cached` : "";
    setupSetText("discord-discovery-count", count);
    const discover = setupById("discover-discord");
    if (discover) discover.disabled = busy;
    setupChannelRows().forEach(setupRenderRowOptions);
    if (!busy && stateName === "failed") setupSetFeedback("discord-discovery-feedback", detail, "error");
  };
  const setupDiscover = async (body = {}) => {
    const stateName = setupState.discovery.state;
    if (setupState.discoveryRequestInFlight || stateName === "waiting") {
      setupSetFeedback("discord-discovery-feedback", "Discord discovery is already running. Wait for it to finish.", "error");
      return null;
    }
    if (setupState.pollTimer) window.clearTimeout(setupState.pollTimer);
    setupState.discoveryPreviousRequestId = setupState.discovery.requestId;
    setupState.discoveryRequestInFlight = true;
    setupState.discoveryVersion += 1;
    setupSetFeedback("discord-discovery-feedback", "Requesting Discord directory…");
    setupRenderDiscovery(setupState.discovery);
    try {
      const payload = await setupJson("/api/setup/discord/discover", { method: "POST", body });
      const status = setupUnwrap(payload);
      setupRenderStatus(status);
      const discovery = setupDiscoveryPart(status);
      if (setupStateName(discovery.state) === "waiting" && setupState.discovery.requestId) {
        setupState.discoveryPendingRequestId = setupState.discovery.requestId;
      }
      if (setupStateName(discovery.state) === "waiting") {
        setupSetFeedback("discord-discovery-feedback", "Discovery request accepted. Waiting for the signed-in browser…", "success");
      } else {
        setupSetFeedback("discord-discovery-feedback", setupText(discovery.detail, "Discord directory updated."), "success");
      }
      return status;
    } catch (error) {
      const message = setupText(error?.message, "request failed");
      setupSetFeedback("discord-discovery-feedback", `Could not discover Discord data: ${message}`, "error");
      return null;
    } finally {
      setupState.discoveryRequestInFlight = false;
      setupState.discoveryVersion += 1;
      setupRenderDiscovery(setupDiscoveryPart(setupState.status));
      setupSchedulePoll();
    }
  };
  const setupMaybeAutoDiscover = (status) => {
    const discordState = setupStatusValue(setupPart(status, "discord"));
    if (!["connected", "existing_connected"].includes(discordState)) {
      setupState.discoveryAutoAttempted = false;
      return;
    }
    const discovery = setupDiscoveryPart(status);
    const discoveryState = setupStateName(setupFirst(discovery.state, "idle"));
    if (!["idle", "failed", "login_required"].includes(discoveryState) || setupState.discoveryAutoAttempted) return;
    setupState.discoveryAutoAttempted = true;
    if (setupState.discovery.guilds.size) return;
    window.setTimeout(() => setupDiscover({}), 0);
  };
  const setupHydrateChannels = (status) => {
    const channels = setupChannels(status);
    setupChannelRows().forEach((row, index) => {
      const channel = channels[index] || {};
      const url = setupFirst(channel.url, channel.channel_url, channel.href, "");
      const parsed = setupParseChannelUrl(url);
      const guildId = setupChannelId(setupFirst(channel.guild_id, parsed.guildId, ""));
      const channelId = setupChannelId(setupFirst(channel.channel_id, channel.id, parsed.channelId, ""));
      const rawName = setupText(setupFirst(channel.name, channel.label, ""));
      const name = !url && !channelId && new RegExp(`^Channel ${index + 1}$`, "i").test(rawName) ? "" : rawName;
      const role = setupText(setupFirst(channel.role, "signals"), "signals");
      const authors = Array.isArray(setupFirst(channel.authors, channel.trusted_authors, [])) ? setupFirst(channel.authors, channel.trusted_authors, []) : [];
      const guildField = setupField(row, "guild_id");
      const channelField = setupField(row, "channel_id");
      const urlField = setupField(row, "url");
      const nameField = setupField(row, "name");
      const roleField = setupField(row, "role");
      const restrictField = setupField(row, "restrict_authors");
      const authorsField = setupField(row, "authors");
      const authorIds = authors.map((author) => setupText(author)).filter(Boolean);
      if (guildField) guildField.value = guildId;
      if (channelField) channelField.value = channelId;
      if (urlField) urlField.value = setupText(url);
      if (nameField) nameField.value = name;
      if (roleField) roleField.value = ["signals", "context"].includes(role) ? role : "signals";
      if (restrictField) restrictField.checked = authorIds.length > 0;
      if (authorsField) authorsField.value = "";
      const authorSelect = setupField(row, "author_choices");
      if (authorSelect) authorSelect.replaceChildren(...authorIds.map((id) => {
        const option = setupOption(id, `Saved author ${id}`);
        option.selected = true;
        return option;
      }));
      setupRenderRowOptions(row);
      const state = row.querySelector("[data-channel-state]");
      if (state) state.textContent = url || channelId ? "Configured" : "Ready to pick a channel";
      const advanced = row.querySelector(".advanced-channel");
      if (advanced) advanced.open = Boolean(url && (!setupState.discovery.channels.has(channelId) || !setupState.discovery.guilds.has(guildId)));
    });
    const poll = setupById("poll-seconds");
    if (poll) poll.value = String(setupPollSeconds(status));
    setupState.loadedChannels = true;
    setupState.dirtyChannels = false;
    setupSetText("channel-save-state", "Saved");
  };
  const setupSetChannelDirty = () => {
    setupState.dirtyChannels = true;
    setupSetText("channel-save-state", "Unsaved changes");
    setupSetFeedback("channel-form-feedback", "");
  };
  const setupReadChannels = () => setupChannelRows().map((row) => {
    const binding = setupCurrentRowBinding(row);
    const known = setupState.discovery.channels.get(binding.channelId);
    const url = setupFixedChannelUrl(binding.guildId, binding.channelId, known?.url || binding.url);
    const restrict = Boolean(setupField(row, "restrict_authors")?.checked);
    return {
      url,
      name: setupText(setupField(row, "name")?.value, "").trim(),
      role: setupText(setupField(row, "role")?.value, "signals").trim() || "signals",
      authors: restrict ? setupSelectedAuthorIds(row) : [],
    };
  });
  const setupRenderEvaluationLegacy = (value) => {
    const settings = value || {};
    const saving = setupState.evaluationInFlight || setupState.expiryPolicyInFlight;
    const values = { "evaluation-model": settings.model || "", "evaluation-effort": settings.reasoning_effort || "medium",
      "evaluation-tier": settings.service_tier || "standard", "evaluation-chase": Number(settings.max_chase_fraction ?? 0.10) * 100 };
    Object.entries(values).forEach(([id, current]) => {
      const field = setupById(id);
      if (!field) return;
      if (!setupState.evaluationDirty && !saving) field.value = String(current);
      field.disabled = saving;
    });
    const save = setupById("save-evaluation");
    if (save) {
      save.disabled = saving;
      save.textContent = setupState.evaluationInFlight ? "Saving…" : setupState.status?.paused === false ? "Pause and save evaluation settings" : "Save evaluation settings";
    }
  };
  const setupRenderEvaluation = (value) => {
    const settings = value && typeof value === "object" ? value : {};
    const jev = settings.jev && typeof settings.jev === "object" ? settings.jev : settings;
    const saving = setupState.evaluationInFlight || setupState.expiryPolicyInFlight;
    const testing = setupState.evaluationTestInFlight;
    const percentValue = (current, fallback) => {
      const number = Number(current ?? fallback);
      return Number.isFinite(number) ? (number <= 1 ? number * 100 : number) : fallback * 100;
    };
    const values = {
      "evaluation-model": settings.model || "",
      "evaluation-effort": settings.reasoning_effort || "medium",
      "evaluation-tier": settings.service_tier || "standard",
      "evaluation-chase": Number(settings.max_chase_fraction ?? 0.10) * 100,
      "evaluation-mode": jev.mode || "codex",
      "evaluation-direct-entries": settings.direct_entries ?? jev.direct_entries ?? true,
      "evaluation-timeout": Number(jev.timeout_ms ?? 1200),
      "evaluation-min-confidence": percentValue(jev.min_confidence, 0.95),
      "evaluation-min-probability": percentValue(jev.min_probability, 0.95),
      "evaluation-min-eligibility": percentValue(jev.min_eligibility, 0.98),
    };
    Object.entries(values).forEach(([id, current]) => {
      const field = setupById(id);
      if (!field) return;
      if (!setupState.evaluationDirty && !saving) {
        if (field.type === "checkbox") field.checked = Boolean(current);
        else field.value = String(current);
      }
      field.disabled = saving || testing;
    });
    const statusValue = setupFirst(jev.state, jev.status, jev.credential_configured === true ? "configured" : "not_configured");
    setupSetStatus("jev-provider-status", statusValue, "jev");
    const credentialConfigured = jev.credential_configured === true;
    setupSetText("jev-credential-state", credentialConfigured ? "Credential configured" : "Credential not configured");
    const directEntries = settings.direct_entries ?? jev.direct_entries ?? true;
    const modeLabel = {
      codex: directEntries ? "Codex fallback" : "Codex only",
      jev_shadow: "JEV shadow comparison",
      jev: "JEV with Codex fallback",
    }[setupText(jev.mode, "codex")] || (directEntries ? "Codex fallback" : "Codex only");
    const detail = setupDetail(jev, credentialConfigured ? `${modeLabel} · ${setupText(jev.model, "jev-latest")}` : `${modeLabel} · TypeSafe credential required`);
    setupSetText("jev-provider-detail", detail);
    const save = setupById("save-evaluation");
    if (save) {
      save.disabled = saving || testing;
      save.textContent = setupState.evaluationInFlight ? "Saving…" : setupState.status?.paused === false ? "Pause and save evaluation settings" : "Save evaluation settings";
    }
    const test = setupById("test-evaluation");
    if (test) {
      test.disabled = saving || testing;
      test.textContent = testing ? "Testing…" : "Test JEV connection";
    }
  };

  const setupRenderRisk = (risk) => {
    const container = setupById("risk-context");
    if (!container || !risk || typeof risk !== "object") return;
    const pieces = [];
    const percent = (value) => (Number(value) * 100).toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (risk.entry_risk_min_fraction !== undefined && risk.entry_risk_max_fraction !== undefined) {
      pieces.push(`Entry sizing reference: ${percent(risk.entry_risk_min_fraction)}–${percent(risk.entry_risk_max_fraction)}% of equity by confidence`);
      pieces.push("Whole-contract fallback: one affordable contract when the sizing budget is below one contract and available buying power after reserve covers it");
    }
    if (risk.min_confidence !== undefined) pieces.push(`Confidence floor: ${percent(risk.min_confidence)}%`);
    if (risk.max_position_fraction !== undefined) pieces.push(`Same-underlying sizing reference: ${percent(risk.max_position_fraction)}% of equity`);
    if (risk.max_total_exposure_fraction !== undefined) pieces.push(`Total option exposure sizing reference: ${percent(risk.max_total_exposure_fraction)}% of equity`);
    if (!pieces.length) return;
    container.hidden = false;
    setupSetText("risk-context-text", `${pieces.join(" · ")}. Risk defaults shown above are read-only.`);
    const expiry = setupById("allow-same-day-expiry");
    if (expiry) {
      if (!setupState.expiryPolicyDirty && !setupState.expiryPolicyInFlight && typeof risk.allow_same_day_expiry === "boolean") {
        expiry.checked = risk.allow_same_day_expiry;
      }
      const trading = setupTradingStatus(setupState.status);
      expiry.disabled = setupState.expiryPolicyInFlight || setupState.evaluationInFlight;
      const save = setupById("save-expiry-policy");
      if (save) {
        save.textContent = setupState.expiryPolicyInFlight ? "Saving…" : setupState.status?.paused === false ? "Pause and save" : "Save permission";
        save.disabled = setupState.expiryPolicyInFlight || setupState.evaluationInFlight;
        save.title = trading.pending ? "Waiting for the worker to load the previous change." : "";
      }
    }
  };
  const setupRenderNotifications = (part, status) => {
    const value = part && typeof part === "object" ? part : {};
    const stateName = setupStatusValue(value, value.configured === true ? "configured" : "not_configured");
    setupSetStatus("notifications-status", stateName);
    setupSetText("notifications-detail", setupDetail(value, value.configured === true ? "Webhook configured; the saved URL stays hidden." : "No output webhook is configured."));
    const mode = setupTradingModeLabel(setupTradingStatus(status).mode);
    setupSetText("notifications-mode", `Relay mode: ${mode}. Output is limited to authentication assistance and relay actions.`);
    const enabled = setupById("notifications-enabled");
    if (enabled && !setupState.notificationsDirty && !setupState.notificationsSaveInFlight) enabled.checked = value.enabled === true;
    if (enabled) enabled.disabled = setupState.notificationsSaveInFlight;
    const input = setupById("notifications-webhook");
    if (input) input.disabled = setupState.notificationsSaveInFlight;
    const save = setupById("save-notifications");
    const clear = setupById("clear-notifications");
    if (save) save.disabled = setupState.notificationsSaveInFlight;
    if (clear) clear.disabled = setupState.notificationsSaveInFlight || value.configured !== true;
    const lastSent = value.last_sent_at;
    if (lastSent && !setupState.notificationsSaveInFlight) setupSetText("notifications-detail", `${setupDetail(value, "Webhook configured; the saved URL stays hidden.")} Last sent ${setupText(lastSent)}.`);
  };
  const setupApprovedDeviceUrl = (value) => {
    try {
      const url = new URL(setupText(value), window.location.origin);
      return url.origin === "https://auth.openai.com" && url.pathname === "/codex/device" && !url.username && !url.password && !url.search && !url.hash ? url.href : "";
    } catch {
      return "";
    }
  };
  const setupApprovedRobinhoodUrl = (value) => {
    const raw = setupText(value, "").trim();
    if (!raw || /[\u0000-\u001f\u007f]/.test(raw)) return "";
    const authority = raw.match(/^https:\/\/([^\/?#]+)(?:[\/?#]|$)/)?.[1];
    if (authority !== "robinhood.com") return "";
    try {
      const url = new URL(raw);
      const states = url.searchParams.getAll("state");
      return url.protocol === "https:" && url.hostname === "robinhood.com" && !url.port && !url.username && !url.password && !url.hash && states.length === 1 && states[0].trim() && !/[\u0000-\u001f\u007f]/.test(states[0]) ? url.href : "";
    } catch {
      return "";
    }
  };
  const setupIsLoopbackHost = (value) => {
    const host = setupText(value, "").trim().toLowerCase().replace(/^\[|\]$/g, "");
    return host === "localhost" || host === "::1" || host === "127.0.0.1" || /^127(?:\.\d{1,3}){3}$/.test(host);
  };
  const setupRobinhoodCallbackUrl = (part, authorizationUrl) => {
    const explicit = setupText(part?.redirect_uri, "").trim();
    try {
      const authUrl = new URL(authorizationUrl);
      const raw = explicit || authUrl.searchParams.get("redirect_uri") || "";
      if (!raw) return null;
      const callbackUrl = new URL(raw);
      return ["http:", "https:"].includes(callbackUrl.protocol) ? callbackUrl : null;
    } catch {
      return null;
    }
  };
  const setupRobinhoodRemoteCallbackRequired = (part, authorizationUrl) => {
    const callbackUrl = setupRobinhoodCallbackUrl(part, authorizationUrl);
    return Boolean(callbackUrl && setupIsLoopbackHost(callbackUrl.hostname) && !setupIsLoopbackHost(window.location.hostname));
  };
  const setupClearRobinhoodCallback = () => {
    const input = setupById("robinhood-callback-url");
    if (input) input.value = "";
  };
  const setupRenderCodex = (part) => {
    const stateName = setupStatusValue(part);
    const waiting = ["starting", "waiting"].includes(stateName);
    if (!waiting) setupState.authActive.codex = false;
    const code = waiting ? setupFirst(part?.user_code, "") : "";
    const url = waiting ? setupApprovedDeviceUrl(part?.verification_url) : "";
    setupSetStatus("codex-setup-status", stateName);
    setupSetText("codex-setup-detail", setupDetail(part, "Waiting for setup status."));
    setupSetText("codex-progress-label", setupDetail(part, setupStatusLabel(stateName)));
    setupSetText("codex-device-code", code || "—");
    const progress = setupById("codex-progress");
    if (progress) progress.hidden = !(setupState.authActive.codex || waiting);
    const link = setupById("codex-device-url");
    const empty = setupById("codex-device-url-empty");
    if (link) {
      link.hidden = !url;
      if (url) link.href = url;
      else link.removeAttribute("href");
    }
    if (empty) empty.hidden = Boolean(url);
    const start = setupById("codex-start");
    const cancel = setupById("codex-cancel");
    if (start) start.disabled = setupState.authActive.codex || ["starting", "waiting"].includes(stateName);
    if (cancel) cancel.disabled = !setupState.authActive.codex && !["starting", "waiting"].includes(stateName);
    if (!["starting", "waiting"].includes(stateName) && ["connected", "existing_connected", "failed", "cancelled", "canceled"].includes(stateName)) setupState.authActive.codex = false;
  };
  const setupRenderRobinhood = (part) => {
    const stateName = setupStatusValue(part);
    const waiting = stateName === "waiting";
    if (!waiting) setupState.authActive.robinhood = false;
    setupSetStatus("robinhood-setup-status", stateName);
    setupSetText("robinhood-setup-detail", setupDetail(part, "Waiting for setup status."));
    const authorizationUrl = waiting ? setupApprovedRobinhoodUrl(part?.authorization_url) : "";
    const authorizationChanged = authorizationUrl !== setupState.robinhoodCallbackAuthorizationUrl;
    if (authorizationChanged) setupClearRobinhoodCallback();
    setupState.robinhoodCallbackAuthorizationUrl = authorizationUrl;
    if (!authorizationUrl) setupState.robinhoodCallbackInFlight = false;
    const authorizationLink = setupById("robinhood-authorization-url");
    const authorizationEmpty = setupById("robinhood-authorization-url-empty");
    if (authorizationLink) {
      authorizationLink.hidden = !authorizationUrl;
      if (authorizationUrl) authorizationLink.href = authorizationUrl;
      else authorizationLink.removeAttribute("href");
    }
    if (authorizationEmpty) authorizationEmpty.hidden = Boolean(authorizationUrl);
    const progress = setupById("robinhood-progress");
    if (progress) progress.hidden = !authorizationUrl;
    const remoteCallback = setupById("robinhood-remote-callback");
    if (remoteCallback) {
      remoteCallback.hidden = !authorizationUrl;
      if (!authorizationUrl) remoteCallback.open = false;
      else if (authorizationChanged && setupRobinhoodRemoteCallbackRequired(part, authorizationUrl)) remoteCallback.open = true;
    }
    const callbackInput = setupById("robinhood-callback-url");
    const callbackSubmit = setupById("robinhood-callback-submit");
    if (callbackInput) callbackInput.disabled = !authorizationUrl || setupState.robinhoodCallbackInFlight;
    if (callbackSubmit) callbackSubmit.disabled = !authorizationUrl || setupState.robinhoodCallbackInFlight;
    const lastFour = setupAccountLastFour(part);
    const summary = setupById("robinhood-account-summary");
    if (summary) summary.hidden = !lastFour;
    setupSetText("robinhood-account-label", lastFour ? `Account ending ${lastFour}` : "");
    const account = part?.account || {};
    const observed = [account.type, account.state, account.option_level].filter((value) => typeof value === "string");
    if (account.agentic_allowed === true) observed.push("Agentic enabled");
    setupSetText("robinhood-account-detail", observed.length ? `Last inspection: ${observed.join(" · ")}` : "");
    const start = setupById("robinhood-start");
    const cancel = setupById("robinhood-cancel");
    if (start) start.disabled = setupState.authActive.robinhood || ["starting", "waiting"].includes(stateName);
    if (cancel) cancel.disabled = !setupState.authActive.robinhood && !["starting", "waiting"].includes(stateName);
  };
  const setupRenderTradingMode = (status) => {
    const trading = setupTradingStatus(status);
    const paused = Boolean(status?.paused);
    const configured = trading.mode !== "unknown";
    const rollbackShadow = trading.pending && trading.mode === "live";
    const settingsInFlight = setupState.expiryPolicyInFlight || setupState.evaluationInFlight;
    const busy = setupState.modeChangeInFlight || settingsInFlight || (trading.pending && !rollbackShadow);
    const statusLabel = trading.pending ? "pending_reload" : paused ? "paused" : "unpaused";
    setupSetStatus("trading-mode-status", statusLabel);
    setupSetText("trading-mode-detail", setupTradingDetail(status, trading));
    const availability = !paused
      ? "Pause the relay before changing mode."
      : trading.pending
        ? rollbackShadow
          ? "Live worker reload is still pending; Use Shadow to roll back while the relay is paused."
          : "Worker reload is still pending; Resume relay stays disabled until it reports the configured mode."
          : !setupLiveReady(status)
            ? "Live requires configured channels and a connected Discord session; Codex and Robinhood sign-ins are also required by the worker."
          : "Select a mode, then wait for the worker to reload before resuming.";
    setupSetText("trading-mode-availability", availability);
    const shadow = setupById("set-shadow-mode");
    const live = setupById("set-live-mode");
    if (shadow) shadow.disabled = setupState.modeChangeInFlight || settingsInFlight || !paused || trading.mode === "shadow" || (trading.pending && trading.mode !== "live");
    if (live) live.disabled = busy || !paused || trading.mode === "live" || !setupLiveReady(status);
    const pause = setupById("pause-relay");
    if (pause) {
      pause.disabled = (paused && settingsInFlight) || (paused && (trading.pending || setupState.modeChangeInFlight));
      pause.title = paused && settingsInFlight
        ? "Wait for settings to save."
        : pause.disabled
          ? "Wait for the worker to load the selected mode before resuming."
          : "";
    }
    if (!configured && shadow) shadow.disabled = true;
  };
  const setupRenderBrowserDialog = () => {
    const dialog = setupById("browser-login-dialog");
    if (!dialog || !dialog.open) return;
    const provider = setupState.dialogProvider || "discord";
    if (provider !== "discord") return;
    const providerName = "Discord login";
    const part = setupPart(setupState.status, provider);
    const stateName = setupState.statusError || setupState.providerError[provider] ? "unknown" : setupStatusValue(part);
    const detail = setupState.statusError || setupState.providerError[provider] || setupDetail(part, "Waiting for setup status.");
    setupSetText("browser-login-title", providerName);
    setupSetStatus("browser-login-status-label", stateName, provider);
    setupSetText("browser-login-detail", detail);
  };

  const setupDiscordTransport = (part = setupPart(setupState.status, "discord")) => {
    const configured = setupText(part?.transport, "browser");
    if (!setupState.discordDirty) return configured === "gateway" ? "gateway" : "browser";
    const selected = setupText(setupById("discord-transport")?.value, configured);
    return selected === "gateway" ? "gateway" : "browser";
  };
  const setupRenderDiscordTransport = (part = setupPart(setupState.status, "discord")) => {
    const select = setupById("discord-transport");
    const fields = setupById("discord-gateway-fields");
    const indicator = setupById("discord-credential-status");
    const transport = setupDiscordTransport(part);
    if (select && !setupState.discordDirty) select.value = transport;
    if (fields) fields.hidden = transport !== "gateway";
    if (indicator) {
      indicator.textContent = transport === "gateway"
        ? part?.credential_configured === true
          ? "A gateway credential is saved. Leave the token empty to keep it."
          : "No saved gateway credential. Enter a token before saving."
        : "Browser session selected.";
    }
    const browserAvailable = transport === "browser" || part?.browser_fallback === true;
    document.querySelectorAll('.browser-link[data-provider="discord"], .setup-browser-link[data-provider="discord"]').forEach((link) => {
      link.hidden = !browserAvailable;
      if (browserAvailable) link.removeAttribute("tabindex");
      else link.setAttribute("tabindex", "-1");
    });
    const frame = setupById("browser-login-frame");
    if (frame) {
      if (!frame.dataset.gatewayBrowserSrc) frame.dataset.gatewayBrowserSrc = frame.getAttribute("src") || "";
      if (browserAvailable) {
        if (!frame.getAttribute("src") && frame.dataset.gatewayBrowserSrc) frame.setAttribute("src", frame.dataset.gatewayBrowserSrc);
      } else frame.removeAttribute("src");
    }
    const save = setupById("save-discord");
    if (save) save.disabled = setupState.discordSaveInFlight;
  };
  const setupSaveDiscord = async () => {
    if (setupState.discordSaveInFlight) return null;
    const transport = setupDiscordTransport();
    const token = setupText(setupById("discord-token")?.value, "").trim();
    const body = { transport };
    if (token) body.token = token;
    setupState.discordSaveInFlight = true;
    setupSetFeedback("discord-setup-feedback", "Saving Discord setup…");
    setupRenderDiscordTransport();
    try {
      const status = await setupPost("/api/setup/discord", body, "discord-setup-feedback");
      if (status) {
        setupState.discordDirty = false;
        const input = setupById("discord-token");
        if (input) input.value = "";
        setupSetFeedback("discord-setup-feedback", "Discord setup saved. Reconnect requested.", "success");
      }
      return status;
    } finally {
      setupState.discordSaveInFlight = false;
      setupRenderDiscordTransport(setupPart(setupState.status, "discord"));
      setupSchedulePoll();
    }
  };
  const setupRenderStatus = (status) => {
    setupState.status = status || {};
    setupState.statusError = "";
    const overall = status?.paused ? "paused" : status?.configured === true ? "connected" : "setup_required";
    setupSetText("setup-state-stamp", setupStatusLabel(overall).toUpperCase());
    const paused = Boolean(status?.paused);
    const pause = setupById("pause-relay");
    if (pause) pause.textContent = paused ? "Resume relay" : "Pause relay";
    setupSetStatus("discord-setup-status", setupStatusValue(setupPart(status, "discord")), "discord");
    setupSetText("discord-setup-detail", setupDetail(setupPart(status, "discord"), "Waiting for setup status."));
    setupRenderDiscordTransport(setupPart(status, "discord"));
    setupRenderDiscovery(setupDiscoveryPart(status));
    setupRenderCodex(setupPart(status, "codex"));
    setupRenderRobinhood(setupPart(status, "robinhood"));
    setupRenderTradingMode(status);
    setupRenderNotifications(setupPart(status, "notifications"), status);
    setupRenderRisk(status?.risk);
    setupRenderEvaluation(status?.evaluation);
    setupRenderBrowserDialog();
    if (overall === "connected") setupSetNotice("healthy", "Channels and account connections are configured. Check the runtime indicators for current monitoring status.");
    else if (["failed", "error"].includes(overall)) setupSetNotice("error", setupDetail(status, "Setup needs attention."));
    else if (overall === "setup_required") setupSetNotice("stale", "Finish the channel settings and account sign-ins below to complete setup.");
    else setupSetNotice("loading", `Setup status: ${setupStatusLabel(overall)}.`);
  };
  const setupSchedulePoll = () => {
    if (setupState.pollTimer) window.clearTimeout(setupState.pollTimer);
    const activeTrading = setupTradingStatus(setupState.status);
    const active = setupState.authActive.codex || setupState.authActive.robinhood || setupState.modeChangeInFlight || setupState.notificationsSaveInFlight || setupState.discordSaveInFlight || setupState.evaluationTestInFlight || activeTrading.pending || setupById("browser-login-dialog")?.open || setupState.discoveryRequestInFlight || setupState.discovery.state === "waiting";
    setupState.pollTimer = window.setTimeout(async () => {
      await setupLoadStatus();
      setupSchedulePoll();
    }, active ? 1600 : 9000);
  };
  async function setupLoadStatus() {
    const discoveryVersion = setupState.discoveryVersion;
    const readSequence = ++setupState.statusReadSequence;
    try {
      const payload = await setupJson("/api/setup");
      if (discoveryVersion !== setupState.discoveryVersion || readSequence !== setupState.statusReadSequence) return null;
      const status = setupUnwrap(payload);
      setupRenderStatus(status);
      if (!setupState.dirtyChannels) setupHydrateChannels(status);
      setupMaybeAutoDiscover(status);
      setupSchedulePoll();
      return status;
    } catch (error) {
      setupState.statusError = error?.status === 404 ? "Setup API is not available in this runtime." : `Setup status unavailable: ${setupText(error?.message, "request failed")}`;
      setupSetNotice("error", setupState.statusError);
      setupRenderBrowserDialog();
      setupSchedulePoll();
      return null;
    }
  }
  const setupPost = async (path, body, feedbackId) => {
    if (!setupState.csrfToken) {
      setupSetText(feedbackId, "Setup security token is not ready; refresh status and try again.");
      return null;
    }
    setupState.statusReadSequence += 1;
    try {
      const payload = await setupJson(path, { method: "POST", body });
      const status = setupUnwrap(payload);
      setupRenderStatus(status);
      setupSchedulePoll();
      return status;
    } catch (error) {
      setupSetFeedback(feedbackId, `Could not update setup: ${setupText(error?.message, "request failed")}`, "error");
      return null;
    } finally {
      setupState.statusReadSequence += 1;
    }
  };
  const setupSaveExpiryPolicy = async () => {
    const status = setupState.status || {};
    const trading = setupTradingStatus(status);
    if (setupState.expiryPolicyInFlight || setupState.evaluationInFlight) return;
    if (!setupState.csrfToken || typeof status.risk?.allow_same_day_expiry !== "boolean") {
      setupSetFeedback("expiry-policy-feedback", "Setup status is not ready. Refresh the page and try saving again.", "error");
      return;
    }
    if (trading.pending) {
      setupSetFeedback("expiry-policy-feedback", "Waiting for the worker to load the previous change. Try saving again shortly.", "error");
      return;
    }
    const enabled = Boolean(setupById("allow-same-day-expiry")?.checked);
    setupState.expiryPolicyInFlight = true;
    setupSetFeedback("expiry-policy-feedback", "Saving…");
    setupRenderRisk(status.risk);
    setupRenderTradingMode(status);
    setupRenderEvaluation(status.evaluation);
    try {
      if (status.paused !== true) {
        const paused = await setupPost("/api/setup/pause", { paused: true }, "expiry-policy-feedback");
        if (!paused || paused.paused !== true) return;
      }
      const next = await setupPost("/api/setup/expiry-policy", { allow_same_day_expiry: enabled }, "expiry-policy-feedback");
      if (next) {
        setupState.expiryPolicyDirty = false;
        setupSetFeedback("expiry-policy-feedback", "Same-day entry permission saved.", "success");
      }
    } finally {
      setupState.expiryPolicyInFlight = false;
      setupRenderRisk(setupState.status?.risk || status.risk);
      setupRenderTradingMode(setupState.status || status);
      setupRenderEvaluation(setupState.status?.evaluation || status.evaluation);
    }
  };
  const setupSaveEvaluation = async () => {
    if (setupState.evaluationInFlight || setupState.expiryPolicyInFlight) return;
    const status = setupState.status || {};
    if (!setupState.csrfToken) {
      setupSetFeedback("evaluation-feedback", "Wait for setup status, then save again.", "error");
      return;
    }
    const percent = (id, fallback) => {
      const value = Number(setupById(id)?.value);
      return Number.isFinite(value) ? value / 100 : fallback;
    };
    const timeout = Number(setupById("evaluation-timeout")?.value);
    const minConfidence = percent("evaluation-min-confidence", 0.95);
    const minProbability = percent("evaluation-min-probability", 0.95);
    const minEligibility = percent("evaluation-min-eligibility", 0.98);
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > 1200 || [minConfidence, minProbability, minEligibility].some((value) => value <= 0 || value > 1)) {
      setupSetFeedback("evaluation-feedback", "JEV deadline must be 1–1200 ms and thresholds must be greater than 0 and at most 100%.", "error");
      return;
    }
    const jevSettings = {
      mode: setupById("evaluation-mode").value,
      direct_entries: Boolean(setupById("evaluation-direct-entries")?.checked),
      model: "jev-latest",
      timeout_ms: timeout,
      min_confidence: minConfidence,
      min_probability: minProbability,
      min_eligibility: minEligibility,
    };
    const body = {
      model: setupById("evaluation-model").value.trim() || null,
      reasoning_effort: setupById("evaluation-effort").value,
      service_tier: setupById("evaluation-tier").value,
      max_chase_fraction: String(Number(setupById("evaluation-chase").value) / 100),
    };
    const key = setupText(setupById("evaluation-typesafe-key")?.value, "").trim();
    const currentJev = status.evaluation?.jev;
    if ((currentJev && typeof currentJev === "object") || setupState.evaluationJevDirty || key) body.evaluation = jevSettings;
    if (key) body.evaluation.typesafe_api_key = key;
    setupState.evaluationInFlight = true;
    setupSetFeedback("evaluation-feedback", "Saving…");
    setupRenderStatus(status);
    try {
      if (status.paused !== true) {
        const paused = await setupPost("/api/setup/pause", { paused: true }, "evaluation-feedback");
        if (!paused || paused.paused !== true) return;
      }
      if (await setupPost("/api/setup/evaluation", body, "evaluation-feedback")) {
        setupState.evaluationDirty = false;
        setupState.evaluationJevDirty = false;
        const keyInput = setupById("evaluation-typesafe-key");
        if (keyInput) keyInput.value = "";
        setupSetFeedback("evaluation-feedback", "Evaluation settings saved. Resume after the worker reloads.", "success");
      }
    } finally {
      setupState.evaluationInFlight = false;
      setupRenderStatus(setupState.status || status);
    }
  };

  const setupTestEvaluation = async () => {
    if (setupState.evaluationTestInFlight || setupState.evaluationInFlight) return;
    if (!setupState.csrfToken) {
      setupSetFeedback("evaluation-jev-feedback", "Wait for setup status, then test again.", "error");
      return;
    }
    setupState.evaluationTestInFlight = true;
    setupSetFeedback("evaluation-jev-feedback", "Running synthetic JEV classification…");
    setupRenderEvaluation(setupState.status?.evaluation || {});
    try {
      const payload = await setupJson("/api/setup/evaluation/test", { method: "POST", body: {} });
      const returnedStatus = payload?.setup && typeof payload.setup === "object"
        ? payload.setup
        : payload?.evaluation && typeof payload.evaluation === "object" && payload?.configured !== undefined
          ? payload
          : null;
      if (returnedStatus) setupRenderStatus(returnedStatus);
      const result = payload?.result && typeof payload.result === "object" ? payload.result : payload || {};
      const stateName = setupText(result.state || result.status, "completed");
      const latency = Number(result.latency_ms);
      const detail = setupText(result.detail, setupText(result.message, "Synthetic classification completed."));
      const suffix = Number.isFinite(latency) && latency >= 0 ? ` · ${Math.round(latency)} ms` : "";
      const kind = ["ok", "ready", "configured", "connected", "completed", "success", "passed"].includes(setupStateName(stateName)) ? "success" : "";
      const displayState = setupText(stateName, "completed").replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
      setupSetFeedback("evaluation-jev-feedback", `JEV test ${displayState}${suffix}: ${detail}`, kind);
    } catch (error) {
      setupSetFeedback("evaluation-jev-feedback", `JEV test failed: ${setupText(error?.message, "request failed")}`, "error");
    } finally {
      setupState.evaluationTestInFlight = false;
      setupRenderEvaluation(setupState.status?.evaluation || {});
    }
  };

  const setupReadNotifications = () => {
    const body = { enabled: Boolean(setupById("notifications-enabled")?.checked) };
    const webhookUrl = setupText(setupById("notifications-webhook")?.value, "").trim();
    if (webhookUrl) body.webhook_url = webhookUrl;
    return body;
  };
  const setupSaveNotifications = async (clear = false) => {
    if (setupState.notificationsSaveInFlight) return null;
    setupState.notificationsDirty = true;
    setupState.notificationsSaveInFlight = true;
    setupSetFeedback("notifications-feedback", clear ? "Removing saved webhook…" : "Saving output settings…");
    const body = clear ? { enabled: false, webhook_url: "" } : setupReadNotifications();
    try {
      const status = await setupPost("/api/setup/notifications", body, "notifications-feedback");
      if (status) {
        setupState.notificationsDirty = false;
        const input = setupById("notifications-webhook");
        if (input) input.value = "";
        setupSetFeedback("notifications-feedback", clear ? "Saved webhook removed." : "Output settings saved. Webhook URL remains hidden.", "success");
      }
      return status;
    } finally {
      setupState.notificationsSaveInFlight = false;
      setupRenderNotifications(setupPart(setupState.status, "notifications"), setupState.status || {});
      setupSchedulePoll();
    }
  };
  const setupSetMode = async (mode) => {
    const status = setupState.status || {};
    const trading = setupTradingStatus(status);
    if (setupState.modeChangeInFlight) return null;
    if (!status.paused) {
      setupSetFeedback("trading-mode-feedback", "Pause the relay before changing trading mode.", "error");
      return null;
    }
    if (trading.pending && !(mode === "shadow" && trading.mode === "live")) {
      setupSetFeedback("trading-mode-feedback", "Wait for the worker to load the selected mode before changing it again.", "error");
      return null;
    }
    if (trading.mode === mode) {
      setupSetFeedback("trading-mode-feedback", `${setupTradingModeLabel(mode)} is already configured.`, "success");
      return null;
    }
    if (mode === "live" && !setupLiveReady(status)) {
      setupSetFeedback("trading-mode-feedback", "Live requires configured channels and a connected Discord session; Codex and Robinhood sign-ins are also required by the worker.", "error");
      return null;
    }
    setupState.modeChangeInFlight = true;
    setupRenderTradingMode(status);
    const body = mode === "live" ? { mode: "live", confirm_live: true } : { mode: "shadow" };
    try {
      const next = await setupPost("/api/setup/mode", body, "trading-mode-feedback");
      if (next) setupSetFeedback("trading-mode-feedback", `${setupTradingModeLabel(mode)} selected. Relay remains paused; wait for the worker reload before using Resume relay.`, "success");
      return next;
    } finally {
      setupState.modeChangeInFlight = false;
      setupRenderTradingMode(setupState.status || status);
    }
  };
  const setupOpenLiveModeDialog = (trigger) => {
    const status = setupState.status || {};
    const trading = setupTradingStatus(status);
    if (!status.paused || trading.pending || trading.mode === "live" || !setupLiveReady(status)) {
      setupSetMode("live");
      return;
    }
    const dialog = setupById("live-mode-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      if (typeof window.confirm === "function" && window.confirm("Enable Live mode? Live can send real Robinhood orders.")) setupSetMode("live");
      return;
    }
    setupState.modeReturnFocus = trigger && !trigger.disabled ? trigger : null;
    dialog.returnValue = "";
    dialog.showModal();
    dialog.querySelector('button[type="submit"]')?.focus();
  };
  const setupOpenBrowser = (provider = "discord", trigger = null) => {
    if (provider !== "discord") return false;
    const discord = setupPart(setupState.status, "discord");
    if (setupDiscordTransport(discord) === "gateway" && discord?.browser_fallback !== true) return true;
    const dialog = setupById("browser-login-dialog");
    if (dialog && typeof dialog.showModal === "function") {
      if (!dialog.open) {
        const candidate = trigger || document.activeElement;
        setupState.dialogReturnFocus = candidate && !candidate.disabled ? candidate : null;
      }
      setupState.dialogProvider = provider;
      if (!dialog.open) dialog.showModal();
      setupRenderBrowserDialog();
      setupById("browser-login-close")?.focus();
      setupSchedulePoll();
      return true;
    }
    const opened = window.open(BROWSER_LOGIN_URL, "_blank", "noopener");
    if (!opened) window.location.assign(BROWSER_LOGIN_URL);
    return true;
  };
  const setupCloseBrowser = () => {
    const dialog = setupById("browser-login-dialog");
    if (dialog?.open) dialog.close();
  };
  const setupAuth = async (provider, start) => {
    const route = `/api/setup/auth/${provider}/${start ? "start" : "cancel"}`;
    const feedback = provider === "codex" ? "codex-setup-detail" : "robinhood-setup-detail";
    if (start) setupState.authActive[provider] = true;
    const accountNumber = setupText(setupById("robinhood-account-number")?.value, "").replace(/\D/g, "");
    const body = provider === "robinhood" && start ? { account_number: accountNumber } : {};
    const status = await setupPost(route, body, feedback);
    if (status) setupState.providerError[provider] = "";
    else {
      if (start) setupState.authActive[provider] = false;
      setupState.providerError[provider] = setupText(setupById(feedback)?.textContent, "Authentication request failed.");
      setupRenderBrowserDialog();
    }
    setupSchedulePoll();
  };
  const setupCompleteRobinhoodCallback = async () => {
    if (setupState.robinhoodCallbackInFlight || !setupState.robinhoodCallbackAuthorizationUrl) return;
    const input = setupById("robinhood-callback-url");
    const callbackUrl = setupText(input?.value, "").trim();
    if (!callbackUrl) return;
    setupState.robinhoodCallbackInFlight = true;
    setupClearRobinhoodCallback();
    setupRenderRobinhood(setupPart(setupState.status, "robinhood"));
    const status = await setupPost(
      "/api/setup/auth/robinhood/callback",
      { callback_url: callbackUrl },
      "robinhood-setup-detail",
    );
    setupState.robinhoodCallbackInFlight = false;
    if (status) setupRenderRobinhood(setupPart(setupState.status, "robinhood"));
    else {
      const activeUrl = setupState.robinhoodCallbackAuthorizationUrl;
      if (input) input.disabled = !activeUrl;
      const submit = setupById("robinhood-callback-submit");
      if (submit) submit.disabled = !activeUrl;
    }
  };
  const setupClearRowAuthors = (row) => {
    const select = setupField(row, "author_choices");
    const input = setupField(row, "authors");
    const restrict = setupField(row, "restrict_authors");
    if (select) [...select.options].forEach((option) => { option.selected = false; });
    if (input) input.value = "";
    if (restrict) restrict.checked = false;
  };
  const setupHandleChannelFieldChange = (event) => {
    const target = event.target;
    const row = target?.closest?.(".channel-editor");
    if (!row || !target.dataset.channelField) return;
    setupSetChannelDirty();
    const field = target.dataset.channelField;
    if (field === "guild_id") {
      const channel = setupField(row, "channel_id");
      const url = setupField(row, "url");
      if (channel) channel.value = "";
      if (url) url.value = "";
      setupClearRowAuthors(row);
      setupRenderRowOptions(row);
      if (target.value) setupDiscover({ guild_id: target.value });
      return;
    }
    if (field === "channel_id") {
      const binding = setupCurrentRowBinding(row);
      const known = setupState.discovery.channels.get(setupChannelId(target.value));
      const url = setupField(row, "url");
      if (url) url.value = known?.url || setupFixedChannelUrl(binding.guildId, target.value, "");
      setupClearRowAuthors(row);
      setupRenderRowOptions(row);
      if (binding.guildId && target.value) setupDiscover({ guild_id: binding.guildId, channel_id: target.value });
      return;
    }
    if (field === "url") {
      const parsed = setupParseChannelUrl(target.value);
      if (setupField(row, "guild_id")?.value !== parsed.guildId || setupField(row, "channel_id")?.value !== parsed.channelId) setupClearRowAuthors(row);
      if (parsed.guildId) {
        const guild = setupField(row, "guild_id");
        const channel = setupField(row, "channel_id");
        if (guild) guild.value = parsed.guildId;
        if (channel) channel.value = parsed.channelId;
      } else {
        const guild = setupField(row, "guild_id");
        const channel = setupField(row, "channel_id");
        if (guild) guild.value = "";
        if (channel) channel.value = "";
        setupClearRowAuthors(row);
      }
      setupRenderRowOptions(row);
      return;
    }
    if (field === "restrict_authors") {
      setupRenderRowOptions(row);
      const binding = setupCurrentRowBinding(row);
      const known = setupState.discovery.authorsByChannel.get(setupChannelKey(binding.guildId, binding.channelId));
      if (target.checked && binding.guildId && binding.channelId && !known) {
        setupDiscover({ guild_id: binding.guildId, channel_id: binding.channelId });
      }
    }
  };
  const setupBind = () => {
    const browserDialog = setupById("browser-login-dialog");
    browserDialog?.addEventListener("close", () => {
      const returnFocus = setupState.dialogReturnFocus;
      setupState.dialogReturnFocus = null;
      const fallback = document.querySelector(".setup-browser-link");
      const focusTarget = returnFocus && !returnFocus.disabled ? returnFocus : fallback;
      if (focusTarget?.isConnected && !focusTarget.disabled) focusTarget.focus();
    });
    setupById("browser-login-close")?.addEventListener("click", setupCloseBrowser);
    document.querySelectorAll('.browser-link[data-provider="discord"], .setup-browser-link[data-provider="discord"]').forEach((link) => {
      link.addEventListener("click", (event) => {
        if (setupOpenBrowser(setupText(link.dataset.provider, "discord"), link)) event.preventDefault();
      });
    });
    const liveModeDialog = setupById("live-mode-dialog");
    liveModeDialog?.addEventListener("cancel", (event) => {
      event.preventDefault();
      liveModeDialog.close("cancel");
    });
    liveModeDialog?.addEventListener("close", () => {
      const confirmed = liveModeDialog.returnValue === "confirm";
      const returnFocus = setupState.modeReturnFocus;
      setupState.modeReturnFocus = null;
      if (confirmed) setupSetMode("live");
      else if (returnFocus?.isConnected && !returnFocus.disabled) returnFocus.focus();
    });
    setupById("live-mode-cancel")?.addEventListener("click", () => liveModeDialog?.close("cancel"));
    setupById("live-mode-cancel-secondary")?.addEventListener("click", () => liveModeDialog?.close("cancel"));
    setupById("set-shadow-mode")?.addEventListener("click", () => setupSetMode("shadow"));
    setupById("set-live-mode")?.addEventListener("click", (event) => setupOpenLiveModeDialog(event.currentTarget));
    const discordForm = setupById("discord-setup-form");
    discordForm?.addEventListener("input", () => {
      setupState.discordDirty = true;
      setupRenderDiscordTransport();
      setupSetFeedback("discord-setup-feedback", "Unsaved changes.");
    });
    discordForm?.addEventListener("change", () => {
      setupState.discordDirty = true;
      setupRenderDiscordTransport();
      setupSetFeedback("discord-setup-feedback", "Unsaved changes.");
    });
    discordForm?.addEventListener("submit", (event) => {
      event.preventDefault();
      setupSaveDiscord();
    });
    const channelForm = setupById("channel-setup-form");
    channelForm?.addEventListener("input", (event) => {
      if (event.target?.dataset?.channelField === "url") setupHandleChannelFieldChange(event);
      else setupSetChannelDirty();
    });
    channelForm?.addEventListener("change", setupHandleChannelFieldChange);
    channelForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const pollValue = Number(setupById("poll-seconds")?.value || 30);
      if (!Number.isFinite(pollValue) || pollValue < 2 || pollValue > 60) {
        setupSetFeedback("channel-form-feedback", "Poll interval must be between 2 and 60 seconds.", "error");
        return;
      }
      const restrictedEmpty = setupChannelRows().some((row) => setupField(row, "restrict_authors")?.checked && setupSelectedAuthorIds(row).length === 0);
      if (restrictedEmpty) {
        setupSetFeedback("channel-form-feedback", "Choose at least one observed or manual author, or turn off author restriction.", "error");
        return;
      }
      const save = setupById("save-channels");
      setupState.channelSaveInFlight = true;
      if (save) save.disabled = true;
      try {
        const status = await setupPost("/api/setup/channels", { channels: setupReadChannels(), poll_seconds: Math.round(pollValue) }, "channel-form-feedback");
        if (status) {
          setupState.dirtyChannels = false;
          setupHydrateChannels(status);
          setupSetFeedback("channel-form-feedback", "Channels saved.", "success");
        }
      } finally {
        setupState.channelSaveInFlight = false;
        if (save) save.disabled = false;
      }
    });
    setupById("discover-discord")?.addEventListener("click", () => setupDiscover({}));
    setupChannelRows().forEach((row) => {
      row.querySelector(".discovery-refresh-channel")?.addEventListener("click", () => {
        const binding = setupCurrentRowBinding(row);
        if (binding.guildId) setupDiscover({ guild_id: binding.guildId });
      });
    });
    setupById("codex-start")?.addEventListener("click", () => setupAuth("codex", true));
    setupById("codex-cancel")?.addEventListener("click", () => setupAuth("codex", false));
    setupById("robinhood-start")?.addEventListener("click", () => setupAuth("robinhood", true));
    setupById("robinhood-cancel")?.addEventListener("click", () => setupAuth("robinhood", false));
    setupById("robinhood-callback-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      setupCompleteRobinhoodCallback();
    });
    const notificationsForm = setupById("notifications-form");
    notificationsForm?.addEventListener("input", () => {
      setupState.notificationsDirty = true;
      setupSetFeedback("notifications-feedback", "");
    });
    notificationsForm?.addEventListener("submit", (event) => {
      event.preventDefault();
      setupSaveNotifications(false);
    });
    setupById("clear-notifications")?.addEventListener("click", () => setupSaveNotifications(true));
    setupById("allow-same-day-expiry")?.addEventListener("change", (event) => {
      setupState.expiryPolicyDirty = event.currentTarget.checked !== setupState.status?.risk?.allow_same_day_expiry;
      setupSetFeedback("expiry-policy-feedback", setupState.expiryPolicyDirty ? "Unsaved changes." : "");
      setupRenderRisk(setupState.status?.risk);
    });
    setupById("save-expiry-policy")?.addEventListener("click", setupSaveExpiryPolicy);
    const jevFieldIds = new Set(["evaluation-mode", "evaluation-direct-entries", "evaluation-typesafe-key", "evaluation-timeout", "evaluation-min-confidence", "evaluation-min-probability", "evaluation-min-eligibility"]);
    setupById("evaluation-form")?.addEventListener("input", (event) => {
      setupState.evaluationDirty = true;
      if (jevFieldIds.has(event.target?.id)) setupState.evaluationJevDirty = true;
      setupSetFeedback("evaluation-feedback", "Unsaved changes.");
    });
    setupById("evaluation-form")?.addEventListener("change", (event) => {
      setupState.evaluationDirty = true;
      if (jevFieldIds.has(event.target?.id)) setupState.evaluationJevDirty = true;
      setupSetFeedback("evaluation-feedback", "Unsaved changes.");
    });
    setupById("evaluation-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      setupSaveEvaluation();
    });
    setupById("test-evaluation")?.addEventListener("click", setupTestEvaluation);
    setupById("pause-relay")?.addEventListener("click", async () => {
      const paused = Boolean(setupState.status?.paused);
      const status = await setupPost("/api/setup/pause", { paused: !paused }, "control-feedback");
      if (status) setupSetText("control-feedback", !paused ? "Relay paused." : "Relay resumed.");
    });
    setupById("reconnect-relay")?.addEventListener("click", async () => {
      const status = await setupPost("/api/setup/reconnect", {}, "control-feedback");
      if (status) setupSetText("control-feedback", "Reconnect requested.");
    });
  };
  const setupStart = () => {
    if (!setupById("setup")) return;
    window.addEventListener("beforeunload", (event) => {
      if (!setupState.expiryPolicyDirty && !setupState.expiryPolicyInFlight && !setupState.evaluationDirty && !setupState.evaluationInFlight) return;
      event.preventDefault();
      event.returnValue = "";
    });
    setupBind();
    setupLoadStatus();
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setupStart, { once: true });
  else setupStart();
})();
