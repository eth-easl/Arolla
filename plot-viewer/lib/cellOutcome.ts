import type { CSSProperties } from "react";

/** Maps classify_runs / comparison-health symbols to traffic-light colors (present cells only). */

export type OutcomeColor = "green" | "orange" | "red";

const COLOR_BG: Record<OutcomeColor, string> = {
  green: "linear-gradient(145deg, #bbf7d0 0%, #4eda7a 45%, #22c55e 100%)",
  orange:
    "linear-gradient(145deg, #fde68a 0%, #fdba74 45%, #f97316 100%)",
  red: "linear-gradient(145deg, #fecaca 0%, #f87171 45%, #dc2626 100%)",
};

const COLOR_BORDER: Record<OutcomeColor, string> = {
  green: "#15803d",
  orange: "#c2410c",
  red: "#b91c1c",
};

const COLOR_SHADOW: Record<OutcomeColor, string> = {
  green: "rgba(22, 101, 52, 0.35)",
  orange: "rgba(154, 52, 18, 0.35)",
  red: "rgba(127, 29, 29, 0.35)",
};

/** Outcome for a scenario that was actually run (`present` in API). */
export function outcomeColor(symbol: string | null): OutcomeColor {
  if (!symbol) return "orange";
  if (symbol === "+" || symbol === "=") return "green";
  if (symbol === "-" || symbol === "~") return "red";
  return "orange";
}

export function cellStyle(color: OutcomeColor): CSSProperties {
  return {
    background: COLOR_BG[color],
    border: `2px solid ${COLOR_BORDER[color]}`,
    boxShadow: `inset 0 1px 0 rgba(255,255,255,0.55), 0 2px 6px ${COLOR_SHADOW[color]}`,
    color: "#0f172a",
    textShadow: "0 1px 0 rgba(255,255,255,0.35)",
  };
}

/** Scenario not present for this run / experiment (no column in matrix for that row). */
export function absentCellStyle(): CSSProperties {
  return {
    background: "linear-gradient(145deg, #f1f5f9 0%, #e2e8f0 50%, #cbd5e1 100%)",
    border: "2px solid #94a3b8",
    boxShadow: "inset 0 1px 0 rgba(255,255,255,0.4)",
    color: "#64748b",
    textShadow: "none",
  };
}
