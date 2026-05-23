import type { Timeseries } from "./compareSchema";

/**
 * Live re-implementation of `prototype/experiments/classify_runs.py:classify_policy`
 * driven by the per-scenario `timeseries.json` payload that the aggregator
 * already produces for `outputs/prototype-new/<sweep>/<scenario>/`.
 *
 * The original Python classifier reads `summary.csv` + every policy's
 * `client_attempts*.csv`. Since `aggregate_for_viewer.py` already bakes the
 * relevant per-second buckets (goodput, success rate, attempt-level p95
 * latency) into `timeseries.json`, we recompute the same classification
 * without touching the raw CSVs.
 *
 * Thresholds match `classify_runs.py` so the symbols line up with what the
 * pre-baked `comparison-health.json` / `classification.json` would have
 * said for the prototype root.
 */

export type HealthLabel = "recovered" | "metastable" | "ambiguous";

export type PolicyHealth = {
  label: HealthLabel;
  symbol: "+" | "-" | "~";
  recoverySec: number | null;
  finalGoodputRatio: number;
  finalSuccessPct: number;
  prefaultP95Ms: number | null;
  cooldownP95Ms: number | null;
  reason: string;
};

/** Last 30 s of the recovery window — matches `FINAL_WINDOW_SEC` in classify_runs.py. */
const FINAL_WINDOW_SEC = 30;
const LATENCY_ABS_THRESHOLD_MS = 500;
const LATENCY_MULTIPLIER = 2;
const GOODPUT_RECOVERY_RATIO = 0.9;
const SUCCESS_RECOVERY_PCT = 95;
/** Threshold (fraction) used by `analyze.recovery_time_sec` — success_rate is stored as 0..1 here. */
const RECOVERY_SUCCESS_FRACTION = 0.95;

/** Canonical order for rendering policy rows. Unknown policies fall to the end alphabetically. */
const CANONICAL_POLICY_ORDER: readonly string[] = [
  "no-control",
  "circuit-breaker",
  "envoy-retry-budget",
  "arolla",
  "rb-rl-v1-a",
  "rb-rl-v1-b",
  "rb-rl-v2",
  "rb-rl-v3",
];

export function policySortIndex(policy: string): number {
  const idx = CANONICAL_POLICY_ORDER.indexOf(policy);
  return idx === -1 ? CANONICAL_POLICY_ORDER.length : idx;
}

export function comparePolicy(a: string, b: string): number {
  const ai = policySortIndex(a);
  const bi = policySortIndex(b);
  if (ai !== bi) return ai - bi;
  return a.localeCompare(b);
}

/**
 * Mean of the values whose t falls in [tStart, tEnd) and is non-null/finite.
 * Returns null when the window is empty (caller decides the fallback).
 */
function windowMean(
  t: readonly number[],
  values: readonly (number | null)[],
  tStart: number,
  tEnd: number,
): number | null {
  if (tEnd <= tStart) return null;
  let sum = 0;
  let n = 0;
  for (let i = 0; i < t.length; i += 1) {
    const ti = t[i];
    if (ti < tStart || ti >= tEnd) continue;
    const v = values[i];
    if (v == null || !Number.isFinite(v)) continue;
    sum += v;
    n += 1;
  }
  return n === 0 ? null : sum / n;
}

/**
 * `analyze.recovery_time_sec` semantics: first relative second AFTER fault end
 * where the smoothed success rate clears the threshold. Returns null if the
 * series never recovers within the captured window.
 */
function recoveryTimeSec(
  t: readonly number[],
  successRate: readonly (number | null)[],
  faultEndRelS: number,
): number | null {
  for (let i = 0; i < t.length; i += 1) {
    const ti = t[i];
    if (ti < faultEndRelS) continue;
    const v = successRate[i];
    if (v == null || !Number.isFinite(v)) continue;
    if (v >= RECOVERY_SUCCESS_FRACTION) return ti - faultEndRelS;
  }
  return null;
}

export function classifyPolicyFromSeries(
  ts: Timeseries,
  policy: string,
): PolicyHealth | null {
  const series = ts.series[policy];
  if (!series) return null;

  const phases = ts.phases;
  const t = series.t_rel_s;
  const goodput = series.goodput_rps;
  const successRate = series.success_rate;
  const p95 = series.latency_p95_ms;

  const prefaultStart = phases.prefault_start_rel_s;
  const faultStart = phases.fault_start_rel_s;
  const faultEnd = phases.fault_end_rel_s;
  const recoveryEnd = phases.recovery_end_rel_s;
  const cooldownEnd = phases.cooldown_end_rel_s;

  const finalStart = Math.max(faultEnd, recoveryEnd - FINAL_WINDOW_SEC);

  const prefaultGoodput = windowMean(t, goodput, prefaultStart, faultStart) ?? 0;
  const finalGoodput = windowMean(t, goodput, finalStart, recoveryEnd) ?? 0;
  const finalGoodputRatio =
    prefaultGoodput > 0 ? finalGoodput / prefaultGoodput : 0;

  const finalSuccessMean = windowMean(t, successRate, finalStart, recoveryEnd);
  const finalSuccessPct =
    finalSuccessMean == null ? 0 : finalSuccessMean * 100;

  const prefaultP95Ms = windowMean(t, p95, prefaultStart, faultStart);
  const cooldownP95Ms = windowMean(t, p95, recoveryEnd, cooldownEnd);

  const latencyLimit = Math.max(
    LATENCY_ABS_THRESHOLD_MS,
    prefaultP95Ms != null ? prefaultP95Ms * LATENCY_MULTIPLIER : LATENCY_ABS_THRESHOLD_MS,
  );
  const latencyRecovered =
    cooldownP95Ms == null || cooldownP95Ms <= latencyLimit;

  const recoverySec = recoveryTimeSec(t, successRate, faultEnd);

  const reasons: string[] = [];
  if (recoverySec == null) reasons.push("no success-rate recovery");
  if (finalGoodputRatio < GOODPUT_RECOVERY_RATIO) {
    reasons.push(`final goodput ${finalGoodputRatio.toFixed(2)}x prefault`);
  }
  if (finalSuccessPct < SUCCESS_RECOVERY_PCT) {
    reasons.push(`final success ${finalSuccessPct.toFixed(1)}%`);
  }
  if (!latencyRecovered && cooldownP95Ms != null) {
    reasons.push(
      `cooldown p95 ${cooldownP95Ms.toFixed(0)}ms > ${latencyLimit.toFixed(0)}ms`,
    );
  }

  const hardFail =
    recoverySec == null || finalGoodputRatio < 0.5 || finalSuccessPct < 80;
  const cleanRecovery =
    recoverySec != null &&
    finalGoodputRatio >= GOODPUT_RECOVERY_RATIO &&
    finalSuccessPct >= SUCCESS_RECOVERY_PCT &&
    latencyRecovered;

  let label: HealthLabel;
  let reason: string;
  if (cleanRecovery) {
    label = "recovered";
    reason = "all recovery criteria passed";
  } else if (hardFail) {
    label = "metastable";
    reason = reasons.join("; ");
  } else {
    label = "ambiguous";
    reason = reasons.length ? reasons.join("; ") : "mixed recovery signals";
  }

  return {
    label,
    symbol: symbolForLabel(label),
    recoverySec,
    finalGoodputRatio,
    finalSuccessPct,
    prefaultP95Ms,
    cooldownP95Ms,
    reason,
  };
}

function symbolForLabel(label: HealthLabel): "+" | "-" | "~" {
  if (label === "recovered") return "+";
  if (label === "metastable") return "-";
  return "~";
}
