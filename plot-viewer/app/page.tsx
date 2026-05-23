"use client";

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { sortRbRlV1RunIds } from "@/lib/rbRlSort";

type Err = string | null;

const DEFAULT_EXPERIMENT = "rb-rl-v1-a";
const FALLBACK_EXPERIMENT = "post-cart-stress-open";
/** This page is locked to the legacy `prototype/` outputs root. Use /compare for prototype-new. */
const PLOTS_ROOT = "prototype";

const GRID_COLUMNS = 3;
/** Matches row `gap` between columns — used only for aspect → pixel height math. */
const GRID_GAP_PX = 8;
/** Vertical gap between plot rows inside a section. */
const ROW_STACK_GAP_PX = 12;
/** Space between sections (below the section’s plots + heading clearance). */
const SECTION_STACK_GAP_PX = 26;
const ROW_MIN_HEIGHT_PX = 220;
/** US Letter aspect when /MediaBox is unavailable (matplotlib default-ish). */
const FALLBACK_WH_RATIO = 11 / 8.5;

function chunkRuns<T>(arr: readonly T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < arr.length; i += size)
    out.push(arr.slice(i, i + size));
  return out;
}

function plotBaseName(filename: string): string {
  return filename.replace(/\.(pdf|png)$/i, "");
}

type SweepParam = {
  key: string;
  label: string;
  value: string;
};

const SWEEP_PARAM_LABELS: Record<string, string> = {
  rate_rps: "RPS",
  fault_duration: "Fault duration",
  fault_rate: "Fault rate",
};

function titleCaseToken(token: string): string {
  return token
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatSweepValue(key: string, rawValue: string): string {
  if (key === "rate_rps") return `${rawValue} RPS`;
  if (key === "fault_duration") return `${rawValue}s`;
  if (key === "fault_rate") {
    const m = /^(.+)-(\d+)pct$/.exec(rawValue);
    if (m) return `${m[1]} ${m[2]}%`;
    if (/^\d+$/.test(rawValue)) return `${rawValue}%`;
  }
  return rawValue;
}

function parseSweepParams(runId: string): SweepParam[] {
  // Collect params from ALL path segments to support both:
  //   flat:   rate_rps=1000__fault_duration=20__fault_rate=cartservice-50pct
  //   nested: rate_rps=1000/fault_duration=20/fault_rate=50
  const seen = new Set<string>();
  const out: SweepParam[] = [];
  for (const seg of runId.split("/")) {
    for (const part of seg.split("__")) {
      // key=value — standard sweep dirs
      // key-<digit>… — only match when value starts with a digit (avoids envoy-retry-budget etc.)
      const m =
        /^([^=]+)=(.+)$/.exec(part) ??
        /^([a-zA-Z][a-zA-Z0-9_]*)-([0-9].*)$/.exec(part);
      if (!m || seen.has(m[1])) continue;
      seen.add(m[1]);
      out.push({
        key: m[1],
        label: SWEEP_PARAM_LABELS[m[1]] ?? titleCaseToken(m[1]),
        value: formatSweepValue(m[1], m[2]),
      });
    }
  }
  return out;
}

function formatSweepSummary(params: readonly SweepParam[]): string {
  return params.map((p) => p.value).join(" · ");
}

function isImageFile(filename: string): boolean {
  return /\.png$/i.test(filename);
}

/**
 * Mirrors prototype/experiments/analyze.py (and paper_plotting) figure groups.
 * Each file appears in at most one section; first matching rule wins.
 * PNG names (comparison plots) are matched against the base without extension.
 */
const PLOT_SECTION_SPECS: readonly {
  title: string;
  match: (base: string) => boolean;
}[] = [
  {
    title: "Throughput & reliability",
    match: (b) => b === "rps" || b.startsWith("success-rate"),
  },
  {
    title: "Latency",
    match: (b) => b.startsWith("latency-") || b === "latency-ts",
  },
  {
    title: "Retry amplification & efficiency",
    match: (b) => b === "amplification" || b === "retry-efficiency",
  },
  {
    title: "RL budget control",
    match: (b) => b === "selected_retry_budget",
  },
  {
    title: "Recovery",
    match: (b) => b === "recovery-time" || b.startsWith("recovery-"),
  },
  {
    title: "Retry breakdown & chains",
    match: (b) =>
      b.startsWith("retries-") ||
      b.startsWith("chain-retry") ||
      b === "retry-status-ts",
  },
  {
    title: "Resource usage",
    match: (b) => b.startsWith("resource_usage"),
  },
];

function groupPlotsIntoSections(plotFiles: readonly string[]): {
  title: string;
  files: string[];
}[] {
  const unseen = new Set(plotFiles);
  const out: { title: string; files: string[] }[] = [];
  for (const spec of PLOT_SECTION_SPECS) {
    const matched = [...unseen].filter((f) => spec.match(plotBaseName(f)));
    matched.sort((a, b) => a.localeCompare(b));
    for (const f of matched) unseen.delete(f);
    if (matched.length) out.push({ title: spec.title, files: matched });
  }
  if (unseen.size) {
    const rest = [...unseen].sort((a, b) => a.localeCompare(b));
    out.push({ title: "Other", files: rest });
  }
  return out;
}

/** Fixed display height for PNG comparison plots (landscape, ~4:3 ratio). */
const PNG_ROW_HEIGHT_PX = 320;

function rowHeightPx(
  row: readonly string[],
  colWidthPx: number,
  pdfSizes: Record<string, { w: number; h: number }>,
): number {
  // PNG rows use a fixed height — they render at natural size with objectFit.
  if (row.some((f) => isImageFile(f))) return PNG_ROW_HEIGHT_PX;
  let hRow = ROW_MIN_HEIGHT_PX;
  const ratioFallback = FALLBACK_WH_RATIO;
  for (const f of row) {
    const s = pdfSizes[f];
    const ratio = s && s.w > 0 ? s.h / s.w : ratioFallback;
    const displayH = colWidthPx > 0 ? colWidthPx * ratio : ROW_MIN_HEIGHT_PX;
    hRow = Math.max(hRow, displayH);
  }
  return Math.round(Math.max(hRow, ROW_MIN_HEIGHT_PX));
}

/** Suffix padded / trimmed to HHMMSS for labeling (and sort key when valid). */
function suffixToHmms(suffixDigits: string): string {
  if (!suffixDigits.length) return "000000";
  const d =
    suffixDigits.length > 6
      ? suffixDigits.slice(-6)
      : suffixDigits.padStart(6, "0");
  return d.slice(-6);
}

function parsedRunInstant(runId: string): Date | null {
  const firstSegment = runId.split("/")[0] ?? runId;
  const m = /^(\d{4})(\d{2})(\d{2})_(\d+)$/.exec(firstSegment);
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const hmms = suffixToHmms(m[4]);
  const hh = Number(hmms.slice(0, 2));
  const mm = Number(hmms.slice(2, 4));
  const ss = Number(hmms.slice(4, 6));
  if (
    hh > 23 ||
    mm > 59 ||
    ss > 59 ||
    !Number.isFinite(y) ||
    !Number.isFinite(mo) ||
    !Number.isFinite(d)
  ) {
    return null;
  }
  return new Date(y, mo - 1, d, hh, mm, ss);
}

/** Parse a timestamp string (strips optional "ignored-" prefix before parsing). */
function parsedTimestampInstant(ts: string): Date | null {
  const raw = ts.startsWith("ignored-") ? ts.slice("ignored-".length) : ts;
  return parsedRunInstant(raw);
}

/** Non-ignored timestamps newest-first, ignored timestamps (newest-first) at the bottom. */
function sortTimestamps(ts: string[]): string[] {
  return [...ts].sort((a, b) => {
    const aIgn = a.startsWith("ignored-");
    const bIgn = b.startsWith("ignored-");
    if (aIgn !== bIgn) return aIgn ? 1 : -1;
    return b.localeCompare(a);
  });
}

function formatTimestampLabel(ts: string, _idx: number): string {
  const when = parsedTimestampInstant(ts);
  const prefix = ts.startsWith("ignored-") ? "[ignored] " : "";
  if (!when) return `${prefix}${ts}`;
  const dateStr = when.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
  const timeStr = when.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
  return `${prefix}${dateStr}, ${timeStr}`;
}

function formatScenarioLabel(scen: string, symbol?: string | null): string {
  if (!scen) return "—";
  const params = parseSweepParams(scen);
  const summary = params.length ? formatSweepSummary(params) : scen;
  const sym =
    symbol && /^[~\-+=]$/.test(symbol) ? ` ${symbol.trim()}` : "";
  return `${summary}${sym}`;
}

/**
 * Fragment hints for the built-in PDF viewer (PDFium / Acrobat params):
 * FitH = fit page width to the view, reducing side grey bars from “whole page” zoom.
 */
function pdfEmbedSrc(
  root: string,
  experiment: string,
  run: string,
  file: string,
): string {
  const q = new URLSearchParams({
    root,
    experiment,
    run,
    file,
  });
  return `/api/pdf?${q.toString()}#page=1&toolbar=0&navpanes=0&view=FitH`;
}

function PageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const root = PLOTS_ROOT;
  const [experiments, setExperiments] = useState<string[]>([]);
  const [experiment, setExperiment] = useState(
    () => searchParams.get("experiment") || DEFAULT_EXPERIMENT,
  );
  const [timestamps, setTimestamps] = useState<string[]>([]);
  const [timestamp, setTimestamp] = useState(
    () => searchParams.get("timestamp") || "",
  );
  const [scenarios, setScenarios] = useState<string[]>([]);
  const [scenario, setScenario] = useState(
    () => searchParams.get("scenario") || "",
  );
  const [runSymbols, setRunSymbols] = useState<Record<string, string>>({});
  // Full run path used for all API calls (timestamp/scenario or just timestamp)
  const run = useMemo(
    () => (timestamp && scenario ? `${timestamp}/${scenario}` : timestamp),
    [timestamp, scenario],
  );
  const [files, setFiles] = useState<string[]>([]);
  const [pdfSizes, setPdfSizes] = useState<
    Record<string, { w: number; h: number }>
  >({});
  const [gridInnerWidthPx, setGridInnerWidthPx] = useState(0);
  const gridMeasureRef = useRef<HTMLDivElement>(null);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Err>(null);
  const [heatmapFiles, setHeatmapFiles] = useState<string[]>([]);
  const [showHeatmap, setShowHeatmap] = useState(false);

  useEffect(() => {
    if (!root) return;
    let cancel = false;
    (async () => {
      setError(null);
      try {
        const res = await fetch(
          `/api/experiments?root=${encodeURIComponent(root)}`,
        );
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "experiments fetch failed");
        if (cancel) return;
        const exp: string[] = data.experiments || [];
        setExperiments(exp);
        setExperiment((prev) => {
          if (prev && exp.includes(prev)) return prev;
          if (exp.includes(DEFAULT_EXPERIMENT)) return DEFAULT_EXPERIMENT;
          if (exp.includes(FALLBACK_EXPERIMENT)) return FALLBACK_EXPERIMENT;
          return exp[0] || DEFAULT_EXPERIMENT;
        });
      } catch (e) {
        if (!cancel) setError(e instanceof Error ? e.message : "fetch error");
      }
    })();
    return () => {
      cancel = true;
    };
  }, [root]);

  // Load timestamps when experiment changes
  useEffect(() => {
    setTimestamps([]);
    setTimestamp("");
    setScenarios([]);
    setScenario("");
    if (!root || !experiment) return;
    let cancel = false;
    (async () => {
      setBusy(true);
      setError(null);
      try {
        const q = new URLSearchParams({ root, experiment });
        const res = await fetch(`/api/timestamps?${q.toString()}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "timestamps fetch failed");
        if (cancel) return;
        const ts = sortTimestamps(data.timestamps || []);
        setTimestamps(ts);
        setTimestamp((prev) => {
          if (prev && ts.includes(prev)) return prev;
          return ts[0] ?? "";
        });
      } catch (e) {
        if (!cancel) setError(e instanceof Error ? e.message : "fetch error");
      } finally {
        if (!cancel) setBusy(false);
      }
    })();
    return () => { cancel = true; };
  }, [root, experiment]);

  // Load scenarios when timestamp changes
  useEffect(() => {
    if (!timestamp) {
      setScenarios([]);
      setScenario("");
      return;
    }
    setScenarios([]);
    setScenario("");
    if (!root) return;
    let cancel = false;
    (async () => {
      setBusy(true);
      setError(null);
      try {
        const q = new URLSearchParams({ root, experiment, timestamp });
        const res = await fetch(`/api/runs?${q.toString()}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "scenarios fetch failed");
        if (cancel) return;
        const rRaw: string[] = data.runs || [];
        const symRaw = data.run_symbols ?? data.runSymbols;
        const symbols: Record<string, string> =
          symRaw && typeof symRaw === "object" && symRaw !== null && !Array.isArray(symRaw)
            ? (symRaw as Record<string, string>)
            : {};
        setRunSymbols(symbols);
        // Strip the leading timestamp/ prefix to get bare scenario strings
        const scenList = rRaw.map((r) => {
          const parts = r.split("/");
          return parts.length > 1 ? parts.slice(1).join("/") : "";
        });
        // Difficulty sort when scenarios carry sweep params (contain "="), else alpha
        const sorted =
          scenList.length && scenList[0].includes("=")
            ? sortRbRlV1RunIds(scenList)
            : [...scenList].sort();
        setScenarios(sorted);
        setScenario((prev) => {
          if (sorted.includes(prev)) return prev;
          return sorted[sorted.length - 1] ?? "";
        });
      } catch (e) {
        if (!cancel) setError(e instanceof Error ? e.message : "fetch error");
      } finally {
        if (!cancel) setBusy(false);
      }
    })();
    return () => { cancel = true; };
  }, [root, experiment, timestamp]);

  useEffect(() => {
    if (!run) {
      setHeatmapFiles([]);
      setShowHeatmap(false);
      return;
    }
    if (!root) return;
    let cancel = false;
    (async () => {
      try {
        const q = new URLSearchParams({ root, experiment, run });
        const res = await fetch(`/api/heatmap?${q.toString()}`);
        if (!res.ok) { setHeatmapFiles([]); return; }
        const data = await res.json();
        if (!cancel) setHeatmapFiles(data.files ?? []);
      } catch {
        if (!cancel) setHeatmapFiles([]);
      }
    })();
    setShowHeatmap(false);
    return () => { cancel = true; };
  }, [root, experiment, run]);

  useEffect(() => {
    if (!run) {
      setFiles([]);
      return;
    }
    if (!root) return;
    let cancel = false;
    (async () => {
      setBusy(true);
      setError(null);
      try {
        const q = new URLSearchParams({ root, experiment, run });
        const res = await fetch(`/api/plots?${q.toString()}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "plots fetch failed");
        if (cancel) return;
        setFiles(data.files || []);
      } catch (e) {
        if (!cancel) setError(e instanceof Error ? e.message : "fetch error");
      } finally {
        if (!cancel) setBusy(false);
      }
    })();
    return () => {
      cancel = true;
    };
  }, [root, experiment, run]);

  useEffect(() => {
    setPdfSizes({});
  }, [run]);

  useEffect(() => {
    const params = new URLSearchParams();
    if (experiment) params.set("experiment", experiment);
    if (timestamp) params.set("timestamp", timestamp);
    if (scenario) params.set("scenario", scenario);
    router.replace(`?${params.toString()}`, { scroll: false });
  }, [experiment, timestamp, scenario, router]);

  useEffect(() => {
    const el = gridMeasureRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setGridInnerWidthPx(el.clientWidth);
    });
    ro.observe(el);
    setGridInnerWidthPx(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!run || files.length === 0 || !root) {
      setPdfSizes({});
      return;
    }
    const ac = new AbortController();
    (async () => {
      try {
        const pairs = await Promise.all(
          files.map(async (f) => {
            const q = new URLSearchParams({ root, experiment, run, file: f });
            const res = await fetch(
              `/api/pdf-size?${q.toString()}`,
              { signal: ac.signal },
            );
            const data: unknown = await res.json().catch(() => ({}));
            if (
              res.ok &&
              data &&
              typeof data === "object" &&
              "width" in data &&
              "height" in data
            ) {
              const row = data as { width?: unknown; height?: unknown };
              const w = Number(row.width);
              const h = Number(row.height);
              if (Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0) {
                return [f, { w, h }] as const;
              }
            }
            return null;
          }),
        );
        const next: Record<string, { w: number; h: number }> = {};
        for (const p of pairs) if (p) next[p[0]] = p[1];
        setPdfSizes(next);
      } catch {
        /* aborted or network */
      }
    })();
    return () => ac.abort();
  }, [root, experiment, run, files]);

  const plotSectionsLayout = useMemo(() => {
    const colW =
      gridInnerWidthPx > 0
        ? (gridInnerWidthPx - (GRID_COLUMNS - 1) * GRID_GAP_PX) /
          GRID_COLUMNS
        : 0;
    return groupPlotsIntoSections(files).map((sec) => {
      const rows = chunkRuns(sec.files, GRID_COLUMNS);
      const heightsPx = rows.map((row) => rowHeightPx(row, colW, pdfSizes));
      return {
        title: sec.title,
        rows,
        heightsPx,
      };
    });
  }, [files, pdfSizes, gridInnerWidthPx]);

  const selectedSweepParams = useMemo(() => parseSweepParams(scenario), [scenario]);

  return (
    <main style={{ padding: 0, margin: 0, maxWidth: "100%" }}>
      <section
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "0.75rem",
          alignItems: "flex-end",
          padding: "0.5rem 0.75rem",
          background: "#fff",
          borderBottom: "1px solid #e2e8f0",
        }}
      >
        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ fontSize: "0.75rem", fontWeight: 600, color: "#64748b" }}>
            Experiment
          </span>
          <select
            value={experiment}
            onChange={(e) => setExperiment(e.target.value)}
            disabled={!experiments.length}
            style={{ minWidth: 260, padding: "0.4rem 0.5rem" }}
          >
            {experiments.map((e) => (
              <option key={e} value={e}>
                {e}
              </option>
            ))}
          </select>
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ fontSize: "0.75rem", fontWeight: 600, color: "#64748b" }}>
            Run
          </span>
          <select
            value={timestamp}
            onChange={(e) => setTimestamp(e.target.value)}
            disabled={!timestamps.length}
            style={{ minWidth: 260, padding: "0.4rem 0.5rem" }}
          >
            {timestamps.map((ts, idx) => (
              <option key={ts} value={ts}>
                {formatTimestampLabel(ts, idx + 1)}
              </option>
            ))}
          </select>
        </label>
        {scenarios.length > 0 && !(scenarios.length === 1 && scenarios[0] === "") && (
          <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={{ fontSize: "0.75rem", fontWeight: 600, color: "#64748b" }}>
              Scenario
            </span>
            <select
              value={scenario}
              onChange={(e) => setScenario(e.target.value)}
              disabled={!scenarios.length}
              style={{ minWidth: 280, padding: "0.4rem 0.5rem" }}
            >
              {scenarios.map((s) => (
                <option key={s} value={s}>
                  {formatScenarioLabel(
                    s,
                    runSymbols[s ? `${timestamp}/${s}` : timestamp],
                  )}
                </option>
              ))}
            </select>
          </label>
        )}
        {selectedSweepParams.length ? (
          <div
            aria-label="Selected sweep cell"
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "0.35rem",
              alignItems: "center",
              maxWidth: "100%",
            }}
          >
            {selectedSweepParams.map((param) => (
              <span
                key={param.key}
                style={{
                  display: "inline-flex",
                  gap: "0.3rem",
                  alignItems: "center",
                  border: "1px solid #cbd5e1",
                  borderRadius: 999,
                  padding: "0.22rem 0.5rem",
                  fontSize: "0.78rem",
                  background: "#f8fafc",
                }}
              >
                <strong style={{ color: "#475569" }}>{param.label}</strong>
                <span>{param.value}</span>
              </span>
            ))}
          </div>
        ) : null}
        {busy && (
          <span style={{ fontSize: "0.85rem", color: "#64748b" }}>Loading…</span>
        )}
        {heatmapFiles.length > 0 && (
          <button
            type="button"
            onClick={() => setShowHeatmap((v) => !v)}
            style={{
              padding: "0.4rem 0.8rem",
              fontSize: "0.78rem",
              fontWeight: 600,
              border: "1px solid #cbd5e1",
              borderRadius: 6,
              background: showHeatmap ? "#1e40af" : "#f1f5f9",
              color: showHeatmap ? "#fff" : "#1e40af",
              cursor: "pointer",
            }}
          >
            {showHeatmap ? "Hide Heatmap" : "Show Heatmap"}
          </button>
        )}
      </section>

      {error && (
        <p
          style={{
            color: "#b91c1c",
            fontSize: "0.9rem",
            padding: "0 0.75rem",
            margin: "0.5rem 0 0",
          }}
          role="alert"
        >
          {error}
        </p>
      )}

      {showHeatmap && heatmapFiles.length > 0 && (
        <div
          style={{
            padding: "0.75rem",
            background: "#f8fafc",
            borderBottom: "1px solid #e2e8f0",
          }}
        >
          <h2
            style={{
              margin: "0 0 0.6rem",
              fontSize: "0.8rem",
              fontWeight: 700,
              letterSpacing: "0.04em",
              textTransform: "uppercase",
              color: "#475569",
            }}
          >
            Heatmap
          </h2>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: `repeat(${Math.min(heatmapFiles.length, GRID_COLUMNS)}, minmax(0, 1fr))`,
              gap: GRID_GAP_PX,
            }}
          >
            {heatmapFiles.map((file) => {
              const q = new URLSearchParams({ root, experiment, run, file });
              const src = `/api/heatmap-pdf?${q.toString()}#page=1&toolbar=0&navpanes=0&view=FitH`;
              const isPng = /\.png$/i.test(file);
              return (
                <div
                  key={file}
                  style={{ position: "relative", width: "100%", minHeight: 480 }}
                >
                  {isPng ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      alt={file.replace(/\.(pdf|png)$/i, "")}
                      src={src}
                      style={{ width: "100%", height: "100%", objectFit: "contain" }}
                    />
                  ) : (
                    <iframe
                      title={file}
                      src={src}
                      tabIndex={-1}
                      style={{
                        position: "absolute",
                        inset: 0,
                        width: "100%",
                        height: "100%",
                        border: "none",
                        background: "#fff",
                      }}
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {!files.length && !busy && run && !error ? (
        <p style={{ color: "#64748b", padding: "0 0.75rem" }}>
          No plots in this folder.
        </p>
      ) : null}

      <div style={{ padding: "0.5rem", background: "#fff" }}>
        <div ref={gridMeasureRef} style={{ width: "100%" }}>
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: SECTION_STACK_GAP_PX,
            }}
          >
            {plotSectionsLayout.map((sec, si) => (
              <section
                key={`sec-${sec.title}-${si}`}
                aria-label={sec.title}
                style={{ margin: 0, padding: 0 }}
              >
                <h2
                  style={{
                    margin: "0 0 0.75rem",
                    fontSize: "0.8rem",
                    fontWeight: 700,
                    letterSpacing: "0.04em",
                    textTransform: "uppercase",
                    color: "#475569",
                  }}
                >
                  {sec.title}
                </h2>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: ROW_STACK_GAP_PX,
                  }}
                >
                  {sec.rows.map((rowFiles, rowIdx) => (
                    <div
                      key={`row-${si}-${rowIdx}-${rowFiles[0] ?? ""}`}
                      style={{
                        display: "grid",
                        gridTemplateColumns: `repeat(${GRID_COLUMNS}, minmax(0, 1fr))`,
                        gap: GRID_GAP_PX,
                        alignItems: "stretch",
                        minHeight:
                          sec.heightsPx[rowIdx] ?? ROW_MIN_HEIGHT_PX,
                        height: sec.heightsPx[rowIdx] ?? ROW_MIN_HEIGHT_PX,
                      }}
                    >
                      {rowFiles.map((file) => (
                        <div
                          key={file}
                          style={{
                            position: "relative",
                            width: "100%",
                            height: "100%",
                            minHeight: 0,
                            minWidth: 0,
                          }}
                        >
                          {isImageFile(file) ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                              alt={plotBaseName(file)}
                              src={pdfEmbedSrc(root, experiment, run, file)}
                              style={{
                                width: "100%",
                                height: "100%",
                                objectFit: "contain",
                                objectPosition: "top left",
                                display: "block",
                              }}
                            />
                          ) : (
                            <>
                              <iframe
                                title={file}
                                src={pdfEmbedSrc(root, experiment, run, file)}
                                tabIndex={-1}
                                style={{
                                  position: "absolute",
                                  inset: 0,
                                  width: "100%",
                                  height: "100%",
                                  border: "none",
                                  background: "#fff",
                                  pointerEvents: "none",
                                }}
                              />
                              <div
                                aria-hidden
                                style={{
                                  position: "absolute",
                                  inset: 0,
                                  zIndex: 1,
                                  cursor: "default",
                                }}
                              />
                            </>
                          )}
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              </section>
            ))}
          </div>
        </div>
      </div>
    </main>
  );
}

export default function Page() {
  return (
    <Suspense>
      <PageContent />
    </Suspense>
  );
}
