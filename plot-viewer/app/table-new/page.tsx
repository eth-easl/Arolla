"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { absentCellStyle, cellStyle, outcomeColor } from "@/lib/cellOutcome";

const TABLE_NEW_ROOT = "prototype-new";
const HIDDEN_ROWS_STORAGE_KEY = "plotViewerTableNewHiddenRowKeys";

type SweepParam = { key: string; label: string; value: string };

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
  const seen = new Set<string>();
  const out: SweepParam[] = [];
  for (const seg of runId.split("/")) {
    for (const part of seg.split("__")) {
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

function formatScenarioColumnLabel(scen: string): string {
  if (scen === "") return "Single run";
  const params = parseSweepParams(scen);
  return params.length ? params.map((p) => p.value).join(" · ") : scen;
}

function suffixToHmms(suffixDigits: string): string {
  if (!suffixDigits.length) return "000000";
  const d =
    suffixDigits.length > 6
      ? suffixDigits.slice(-6)
      : suffixDigits.padStart(6, "0");
  return d.slice(-6);
}

function parsedTimestampInstant(ts: string): Date | null {
  const raw = ts.startsWith("ignored-") ? ts.slice("ignored-".length) : ts;
  const firstSegment = raw.split("/")[0] ?? raw;
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

function formatRunRowLabel(ts: string): string {
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

type CellData = {
  present: boolean;
  symbol: string | null;
  reason?: string | null;
};

type TableRow = {
  key: string;
  experiment: string;
  timestamp: string;
  policy: string;
  cells: Record<string, CellData>;
};

function IconEye() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
      width={16}
      height={16}
      aria-hidden
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z"
      />
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
      />
    </svg>
  );
}

function IconFunnel() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
      width={15}
      height={15}
      aria-hidden
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 3c2.755 0 5.455.232 8.083.678.533.09.917.556.917 1.096v1.044a2.25 2.25 0 01-.659 1.591l-5.432 5.432a2.25 2.25 0 00-.659 1.591v2.927a2.25 2.25 0 01-1.244 2.013L9.75 21v-6.568a2.25 2.25 0 00-.659-1.591L3.659 7.409A2.25 2.25 0 013 5.818V4.774c0-.54.384-1.006.917-1.096A48.32 48.32 0 0112 3z"
      />
    </svg>
  );
}

function readHiddenKeysFromStorage(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(HIDDEN_ROWS_STORAGE_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw) as unknown;
    if (!Array.isArray(arr)) return new Set();
    return new Set(arr.filter((x): x is string => typeof x === "string"));
  } catch {
    return new Set();
  }
}

function writeHiddenKeysToStorage(keys: Set<string>): void {
  try {
    window.localStorage.setItem(
      HIDDEN_ROWS_STORAGE_KEY,
      JSON.stringify([...keys]),
    );
  } catch {
    /* private mode / quota */
  }
}

const COL_TOGGLE_W = 42;
const COL_FILTER_W = 42;
const STICKY_RUN_LEFT = COL_TOGGLE_W + COL_FILTER_W;

const PAGE_CSS = `
  .tn-table-wrap {
    overflow: auto;
    max-height: calc(100vh - 13rem);
    border-radius: 12px;
    border: 1px solid #1e293b;
    box-shadow:
      0 1px 3px rgba(0,0,0,.2),
      0 8px 32px rgba(15,23,42,.16),
      0 0 0 1px rgba(255,255,255,.04) inset;
  }
  .tn-table-wrap::-webkit-scrollbar { width: 7px; height: 7px; }
  .tn-table-wrap::-webkit-scrollbar-track { background: #0f172a; }
  .tn-table-wrap::-webkit-scrollbar-thumb {
    background: #334155; border-radius: 4px;
    border: 1px solid #0f172a;
  }
  .tn-table-wrap::-webkit-scrollbar-thumb:hover { background: #475569; }
  .tn-table-wrap::-webkit-scrollbar-corner { background: #0f172a; }

  .tn-table {
    border-collapse: separate;
    border-spacing: 0;
    min-width: 100%;
    font-size: .78rem;
    background: #ffffff;
  }

  /* Column headers */
  .tn-th-ctrl {
    position: sticky; top: 0; z-index: 4;
    background: #0f172a;
    border-bottom: 1px solid #1e293b;
    border-right: 1px solid #1e293b;
    padding: .55rem .25rem;
    font-size: .6rem; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
    color: #475569;
    text-align: center; vertical-align: bottom; line-height: 1.2;
  }
  .tn-th-run {
    position: sticky; top: 0; z-index: 3;
    background: #0f172a;
    border-bottom: 1px solid #1e293b;
    border-right: 2px solid #334155;
    padding: .65rem .85rem;
    text-align: left; font-weight: 700;
    font-size: .7rem; letter-spacing: .05em; text-transform: uppercase;
    color: #64748b;
    white-space: nowrap; min-width: 260px;
  }
  .tn-th-col {
    position: sticky; top: 0; z-index: 2;
    background: #0f172a;
    border-bottom: 1px solid #1e293b;
    border-right: 1px solid #1e293b;
    padding: .6rem .55rem;
    text-align: center; font-weight: 600;
    font-size: .7rem; letter-spacing: .02em;
    color: #94a3b8;
    max-width: 140px; line-height: 1.3;
    vertical-align: bottom;
    transition: color .15s, background .15s;
  }
  .tn-th-col:hover { color: #e2e8f0; background: #1e293b; }

  /* Body rows */
  .tn-row { transition: background .1s; }
  .tn-row:hover .tn-td-ctrl,
  .tn-row:hover .tn-td-run { filter: brightness(.96); }

  .tn-td-ctrl {
    position: sticky; z-index: 2;
    border-bottom: 1px solid #e2e8f0;
    border-right: 1px solid #e2e8f0;
    padding: .35rem .25rem;
    text-align: center; vertical-align: middle;
    transition: background .1s;
  }
  .tn-td-run {
    position: sticky; z-index: 2;
    border-bottom: 1px solid #e2e8f0;
    border-right: 2px solid #e2e8f0;
    padding: .55rem .85rem;
    text-align: left; vertical-align: middle;
    transition: background .1s;
  }

  /* Action buttons */
  .tn-btn {
    display: inline-flex; align-items: center; justify-content: center;
    width: 32px; height: 28px; padding: 0;
    border-radius: 7px;
    cursor: pointer;
    transition: background .15s, border-color .15s, color .15s,
                box-shadow .15s, transform .1s;
  }
  .tn-btn:active { transform: scale(.92); }
  .tn-btn:focus-visible { outline: 2px solid #6366f1; outline-offset: 2px; }

  .tn-btn-hide {
    background: transparent;
    border: 1px solid #334155;
    color: #475569;
  }
  .tn-btn-hide:hover {
    background: #fef2f2;
    border-color: #fca5a5;
    color: #dc2626;
    box-shadow: 0 0 0 3px rgba(220,38,38,.1);
  }

  .tn-btn-filter {
    background: transparent;
    border: 1px solid #334155;
    color: #475569;
  }
  .tn-btn-filter:hover {
    background: #eef2ff;
    border-color: #a5b4fc;
    color: #4338ca;
    box-shadow: 0 0 0 3px rgba(99,102,241,.1);
  }

  .tn-btn-filter-on {
    background: #e0e7ff;
    border: 2px solid #6366f1;
    color: #3730a3;
    box-shadow: 0 0 0 3px rgba(99,102,241,.18);
  }
  .tn-btn-filter-on:hover {
    background: #c7d2fe;
    border-color: #4338ca;
    box-shadow: 0 0 0 4px rgba(99,102,241,.22);
  }

  /* Data cells */
  .tn-cell {
    border-bottom: 1px solid #e2e8f0;
    border-right: 1px solid #e2e8f0;
    padding: .4rem .35rem;
    text-align: center;
    font-size: 1rem;
    min-width: 50px;
    user-select: none;
    vertical-align: middle;
  }
  .tn-cell-link {
    cursor: pointer;
    transition: filter .12s, transform .12s, box-shadow .12s;
  }
  .tn-cell-link:hover {
    filter: brightness(1.09) saturate(1.1);
    transform: scale(1.06);
    position: relative; z-index: 1;
    box-shadow: 0 4px 12px rgba(0,0,0,.18);
  }
  .tn-cell-link:focus-visible { outline: 2px solid #6366f1; outline-offset: -2px; }

  /* Spinner */
  .tn-spinner {
    display: inline-block; width: 14px; height: 14px;
    border: 2px solid #334155; border-top-color: #6366f1;
    border-radius: 50%;
    animation: tn-spin .65s linear infinite;
  }
  @keyframes tn-spin { to { transform: rotate(360deg); } }

  /* Toolbar pills */
  .tn-pill {
    display: inline-flex; align-items: center; gap: .4rem;
    padding: .28rem .72rem; border-radius: 999px;
    font-size: .75rem; font-weight: 600; line-height: 1.4;
    cursor: pointer;
    transition: box-shadow .15s, transform .1s, filter .15s;
    text-decoration: none;
  }
  .tn-pill:hover { box-shadow: 0 2px 8px rgba(0,0,0,.14); filter: brightness(.97); }
  .tn-pill:active { transform: scale(.97); }
  .tn-pill:focus-visible { outline: 2px solid #6366f1; outline-offset: 2px; }
`;

function TableNewContent() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [rows, setRows] = useState<TableRow[]>([]);
  const [hiddenKeys, setHiddenKeys] = useState<Set<string>>(() => new Set());
  const [columnFilterSourceKey, setColumnFilterSourceKey] = useState<
    string | null
  >(null);

  useEffect(() => {
    setHiddenKeys(readHiddenKeysFromStorage());
  }, []);

  useEffect(() => {
    let cancel = false;
    (async () => {
      setBusy(true);
      setError(null);
      try {
        const res = await fetch(
          `/api/compare/table-matrix?root=${encodeURIComponent(TABLE_NEW_ROOT)}`,
        );
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "matrix fetch failed");
        if (cancel) return;
        setColumns(data.columns || []);
        setRows(data.rows || []);
      } catch (e) {
        if (!cancel) setError(e instanceof Error ? e.message : "fetch error");
      } finally {
        if (!cancel) setBusy(false);
      }
    })();
    return () => {
      cancel = true;
    };
  }, []);

  function toggleRowHidden(key: string) {
    setColumnFilterSourceKey((f) => (f === key ? null : f));
    setHiddenKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      writeHiddenKeysToStorage(next);
      return next;
    });
  }

  function showAllRows() {
    setHiddenKeys(() => {
      writeHiddenKeysToStorage(new Set());
      return new Set();
    });
  }

  const visibleRows = rows.filter((r) => !hiddenKeys.has(r.key));

  const filterSourceRow = useMemo(
    () =>
      columnFilterSourceKey
        ? rows.find((r) => r.key === columnFilterSourceKey)
        : null,
    [rows, columnFilterSourceKey],
  );

  const visibleColumns = useMemo(() => {
    if (!filterSourceRow) return columns;
    const picked = columns.filter((c) => filterSourceRow.cells[c]?.present);
    return picked.length ? picked : columns;
  }, [columns, filterSourceRow]);

  function toggleColumnFilterForRow(key: string) {
    setColumnFilterSourceKey((prev) => (prev === key ? null : key));
  }

  function clearColumnFilter() {
    setColumnFilterSourceKey(null);
  }

  return (
    <main style={{ padding: "1rem 1.25rem", margin: 0, maxWidth: "100%" }}>
      {/* eslint-disable-next-line react/no-danger */}
      <style dangerouslySetInnerHTML={{ __html: PAGE_CSS }} />

      {/* ── Toolbar ── */}
      <section
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "0.6rem",
          alignItems: "center",
          marginBottom: "0.85rem",
        }}
      >
        {/* Root path chip */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.5rem",
            background: "#0f172a",
            border: "1px solid #1e293b",
            borderRadius: 8,
            padding: "0.3rem 0.65rem 0.3rem 0.55rem",
          }}
        >
          <span
            style={{
              fontSize: "0.6rem",
              fontWeight: 700,
              letterSpacing: "0.07em",
              textTransform: "uppercase",
              color: "#475569",
            }}
          >
            Root
          </span>
          <span
            style={{
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
              fontSize: "0.78rem",
              fontWeight: 600,
              color: "#a5b4fc",
              letterSpacing: "0.01em",
            }}
          >
            {TABLE_NEW_ROOT}
          </span>
        </div>

        {busy && <div className="tn-spinner" aria-label="Loading" />}

        {hiddenKeys.size > 0 && (
          <button
            type="button"
            onClick={showAllRows}
            className="tn-pill"
            style={{
              background: "#fef3c7",
              color: "#92400e",
              border: "1px solid #fbbf24",
            }}
          >
            <svg
              width={12}
              height={12}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2.5}
              aria-hidden
            >
              <path d="M17 12H7M12 17l-5-5 5-5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {hiddenKeys.size} hidden — show all
          </button>
        )}

        {columnFilterSourceKey && (
          <button
            type="button"
            onClick={clearColumnFilter}
            className="tn-pill"
            style={{
              background: "#eef2ff",
              color: "#3730a3",
              border: "1px solid #a5b4fc",
            }}
          >
            <svg
              width={12}
              height={12}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2.5}
              aria-hidden
            >
              <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Clear column filter
          </button>
        )}
      </section>

      {/* ── Legend ── */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "0.6rem 1.1rem",
          marginBottom: "0.9rem",
          padding: "0.45rem 0.8rem",
          background: "#f8fafc",
          border: "1px solid #e2e8f0",
          borderRadius: 8,
          fontSize: "0.73rem",
          color: "#475569",
          maxWidth: "64rem",
        }}
      >
        {(
          [
            { symbol: "+", label: "recovered", bg: "#dcfce7", border: "#4ade80", color: "#166534" },
            { symbol: "−", label: "metastable", bg: "#fee2e2", border: "#f87171", color: "#991b1b" },
            { symbol: "~", label: "ambiguous", bg: "#ffedd5", border: "#fb923c", color: "#9a3412" },
            { symbol: "—", label: "not run", bg: "#f1f5f9", border: "#cbd5e1", color: "#64748b" },
          ] as const
        ).map(({ symbol, label, bg, border, color }) => (
          <span
            key={label}
            style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}
          >
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                width: 22,
                height: 20,
                borderRadius: 5,
                background: bg,
                border: `1.5px solid ${border}`,
                color,
                fontWeight: 800,
                fontSize: "0.78rem",
                lineHeight: 1,
              }}
            >
              {symbol}
            </span>
            <span style={{ fontWeight: 500 }}>{label}</span>
          </span>
        ))}
        <span style={{ color: "#94a3b8", fontSize: "0.7rem" }}>
          · funnel button filters columns to scenarios present in that row
        </span>
      </div>

      {error && (
        <div
          role="alert"
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.5rem",
            padding: "0.6rem 0.9rem",
            marginBottom: "0.75rem",
            background: "#fef2f2",
            border: "1px solid #fca5a5",
            borderRadius: 8,
            color: "#b91c1c",
            fontSize: "0.85rem",
            fontWeight: 500,
          }}
        >
          <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
            <circle cx={12} cy={12} r={10} />
            <path d="M12 8v4M12 16h.01" strokeLinecap="round" />
          </svg>
          {error}
        </div>
      )}

      {!rows.length && !busy && !error && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: "0.6rem",
            padding: "3rem 1rem",
            color: "#64748b",
            fontSize: "0.88rem",
          }}
        >
          <svg width={32} height={32} viewBox="0 0 24 24" fill="none" stroke="#cbd5e1" strokeWidth={1.5} aria-hidden>
            <rect x={3} y={3} width={18} height={18} rx={3} />
            <path d="M3 9h18M9 9v12" strokeLinecap="round" />
          </svg>
          No <code>timeseries.json</code> files found under{" "}
          <strong>{TABLE_NEW_ROOT}/</strong>
        </div>
      )}

      {rows.length > 0 && (
        <div className="tn-table-wrap">
          <table className="tn-table">
            <thead>
              <tr>
                {/* Hide column header */}
                <th
                  scope="col"
                  className="tn-th-ctrl"
                  style={{
                    left: 0,
                    width: COL_TOGGLE_W,
                    minWidth: COL_TOGGLE_W,
                    maxWidth: COL_TOGGLE_W,
                  }}
                  title="Hide row"
                >
                  Hide
                </th>

                {/* Filter column header */}
                <th
                  scope="col"
                  className="tn-th-ctrl"
                  style={{
                    left: COL_TOGGLE_W,
                    width: COL_FILTER_W,
                    minWidth: COL_FILTER_W,
                    maxWidth: COL_FILTER_W,
                  }}
                  title="Per-row: filter scenario columns"
                >
                  Cols
                </th>

                {/* Run label header */}
                <th scope="col" className="tn-th-run" style={{ left: STICKY_RUN_LEFT }}>
                  Policy &nbsp;·&nbsp; Sweep
                </th>

                {/* Scenario column headers */}
                {visibleColumns.map((col) => (
                  <th
                    key={col || "__single__"}
                    scope="col"
                    className="tn-th-col"
                  >
                    <span style={{ display: "block", wordBreak: "break-word" }}>
                      {formatScenarioColumnLabel(col)}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((row, ri) => {
                const isFilterSource = columnFilterSourceKey === row.key;

                const zebraBase = ri % 2 === 0 ? "#ffffff" : "#f8fafc";
                const zebraCtrl = ri % 2 === 0 ? "#fafafa" : "#f3f6fa";
                const runBg = isFilterSource
                  ? ri % 2 === 0
                    ? "#eef2ff"
                    : "#e0e7ff"
                  : zebraBase;

                return (
                  <tr key={row.key} className="tn-row">
                    {/* Hide button */}
                    <td
                      className="tn-td-ctrl"
                      style={{
                        left: 0,
                        width: COL_TOGGLE_W,
                        minWidth: COL_TOGGLE_W,
                        maxWidth: COL_TOGGLE_W,
                        background: zebraCtrl,
                      }}
                    >
                      <button
                        type="button"
                        onClick={() => toggleRowHidden(row.key)}
                        title="Hide this row"
                        aria-label={`Hide ${row.policy} @ ${row.timestamp}`}
                        className="tn-btn tn-btn-hide"
                      >
                        <IconEye />
                      </button>
                    </td>

                    {/* Filter button */}
                    <td
                      className="tn-td-ctrl"
                      style={{
                        left: COL_TOGGLE_W,
                        width: COL_FILTER_W,
                        minWidth: COL_FILTER_W,
                        maxWidth: COL_FILTER_W,
                        background: zebraCtrl,
                      }}
                    >
                      <button
                        type="button"
                        onClick={() => toggleColumnFilterForRow(row.key)}
                        title={
                          isFilterSource
                            ? "Clear column filter (click again)"
                            : "Show only scenarios present for this row"
                        }
                        aria-label={
                          isFilterSource
                            ? "Clear scenario column filter"
                            : `Filter columns to scenarios for ${row.policy} ${row.timestamp}`
                        }
                        className={`tn-btn ${isFilterSource ? "tn-btn-filter-on" : "tn-btn-filter"}`}
                      >
                        <IconFunnel />
                      </button>
                    </td>

                    {/* Row label */}
                    <th
                      scope="row"
                      className="tn-td-run"
                      style={{
                        left: STICKY_RUN_LEFT,
                        background: runBg,
                      }}
                    >
                      <span
                        style={{
                          display: "inline-block",
                          fontSize: "0.78rem",
                          fontWeight: 700,
                          color: "#0f172a",
                          background: isFilterSource ? "#c7d2fe" : "#f1f5f9",
                          border: `1px solid ${isFilterSource ? "#818cf8" : "#e2e8f0"}`,
                          borderRadius: 5,
                          padding: "0.1rem 0.45rem",
                          marginBottom: "0.2rem",
                          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                          letterSpacing: "0.01em",
                        }}
                      >
                        {row.policy}
                      </span>
                      <span
                        style={{
                          display: "block",
                          fontSize: "0.7rem",
                          fontWeight: 400,
                          color: "#64748b",
                        }}
                      >
                        {formatRunRowLabel(row.timestamp)}
                        {row.experiment !== "full-sweep" ? (
                          <span
                            style={{
                              marginLeft: 6,
                              fontFamily:
                                "ui-monospace, SFMono-Regular, Menlo, monospace",
                              color: "#6366f1",
                              fontSize: "0.67rem",
                            }}
                          >
                            {row.experiment}
                          </span>
                        ) : null}
                      </span>
                    </th>

                    {/* Data cells */}
                    {visibleColumns.map((col) => {
                      const cell = row.cells[col];
                      const present = cell?.present ?? false;
                      const symbol = cell?.symbol ?? null;
                      const label =
                        !present ? "—" : symbol && symbol.length ? symbol : "…";
                      const styleCore = present
                        ? cellStyle(outcomeColor(symbol))
                        : absentCellStyle();
                      const compareHref = present
                        ? `/compare?${new URLSearchParams({
                            timestamp: row.timestamp,
                            scenario: col,
                          }).toString()}`
                        : null;
                      const baseTitle = !present
                        ? "Absent for this row"
                        : cell?.reason
                          ? `${symbol ?? "?"} — ${cell.reason}`
                          : symbol
                            ? `Outcome: ${symbol}`
                            : "Present; no classification";
                      const titleText = compareHref
                        ? `${baseTitle}\nClick to open this scenario in /compare`
                        : baseTitle;
                      return (
                        <td
                          key={col || "__single__"}
                          title={titleText}
                          onClick={
                            compareHref
                              ? () => router.push(compareHref)
                              : undefined
                          }
                          onKeyDown={
                            compareHref
                              ? (e) => {
                                  if (e.key === "Enter" || e.key === " ") {
                                    e.preventDefault();
                                    router.push(compareHref);
                                  }
                                }
                              : undefined
                          }
                          role={compareHref ? "link" : undefined}
                          tabIndex={compareHref ? 0 : undefined}
                          aria-label={
                            compareHref
                              ? `Open ${row.policy} @ ${row.timestamp} / ${col} in /compare`
                              : undefined
                          }
                          className={`tn-cell${compareHref ? " tn-cell-link" : ""}`}
                          style={{
                            ...styleCore,
                            fontWeight: present ? 800 : 600,
                          }}
                        >
                          {label}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}

export default function TableNewPage() {
  return (
    <Suspense fallback={<main style={{ padding: "1rem" }}>Loading…</main>}>
      <TableNewContent />
    </Suspense>
  );
}
