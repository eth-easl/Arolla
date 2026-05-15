"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { absentCellStyle, cellStyle, outcomeColor } from "@/lib/cellOutcome";

const HIDDEN_ROWS_STORAGE_KEY = "plotViewerTableHiddenRowKeys";

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

function formatSweepSummary(params: readonly SweepParam[]): string {
  return params.map((p) => p.value).join(" · ");
}

function formatScenarioColumnLabel(scen: string): string {
  if (scen === "") return "Single run";
  const params = parseSweepParams(scen);
  return params.length ? formatSweepSummary(params) : scen;
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

type CellData = { present: boolean; symbol: string | null };

type TableRow = {
  key: string;
  experiment: string;
  timestamp: string;
  cells: Record<string, CellData>;
};

function IconEye(props: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
      width={20}
      height={20}
      aria-hidden
      {...props}
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

const COL_TOGGLE_W = 44;
const COL_FILTER_W = 44;
const STICKY_RUN_LEFT = COL_TOGGLE_W + COL_FILTER_W;

function IconFunnel() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
      width={18}
      height={18}
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

function TableContent() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [columns, setColumns] = useState<string[]>([]);
  const [rows, setRows] = useState<TableRow[]>([]);
  const [hiddenKeys, setHiddenKeys] = useState<Set<string>>(() => new Set());
  /** When set, only scenario columns present on this row are shown. */
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
        const res = await fetch("/api/table-matrix-all");
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "matrix fetch failed");
        if (cancel) return;
        setColumns(data.columns || []);
        setRows(data.rows || []);
      } catch (e) {
        if (!cancel)
          setError(e instanceof Error ? e.message : "fetch error");
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
    <main style={{ padding: "0.75rem", margin: 0, maxWidth: "100%" }}>
      <section
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "0.75rem",
          alignItems: "center",
          marginBottom: "1rem",
        }}
      >
        {busy && (
          <span style={{ fontSize: "0.85rem", color: "#64748b" }}>Loading…</span>
        )}
        {hiddenKeys.size > 0 && (
          <button
            type="button"
            onClick={showAllRows}
            style={{
              padding: "0.35rem 0.65rem",
              fontSize: "0.8rem",
              fontWeight: 600,
              border: "1px solid #cbd5e1",
              borderRadius: 6,
              background: "#fff",
              color: "#334155",
              cursor: "pointer",
            }}
          >
            Show all rows ({hiddenKeys.size} hidden)
          </button>
        )}
        {columnFilterSourceKey && (
          <button
            type="button"
            onClick={clearColumnFilter}
            style={{
              padding: "0.35rem 0.65rem",
              fontSize: "0.8rem",
              fontWeight: 600,
              border: "1px solid #a5b4fc",
              borderRadius: 6,
              background: "#eef2ff",
              color: "#3730a3",
              cursor: "pointer",
            }}
          >
            Clear column filter
          </button>
        )}
      </section>

      <p
        style={{
          fontSize: "0.82rem",
          color: "#475569",
          maxWidth: "56rem",
          lineHeight: 1.45,
          margin: "0 0 1rem",
        }}
      >
        All experiments and timestamp runs. Each column is a scenario (union
        across experiments). Colors follow{" "}
        <code>classification.json</code> /{" "}
        <code>comparison-health.json</code>: green{" "}
        <strong style={{ color: "#15803d" }}>+</strong> /{" "}
        <strong style={{ color: "#15803d" }}>=</strong>, red{" "}
        <strong style={{ color: "#b91c1c" }}>−</strong> /{" "}
        <strong style={{ color: "#b91c1c" }}>~</strong>, orange for other /
        no label. Absent scenarios for a run are{" "}
        <strong style={{ color: "#64748b" }}>grey</strong>. The
        funnel narrows columns to those scenarios that exist for that row;
        clear from the bar above. The eye hides rows (stored in this browser).
      </p>

      {error && (
        <p style={{ color: "#b91c1c", fontSize: "0.9rem" }} role="alert">
          {error}
        </p>
      )}

      {!rows.length && !busy && !error ? (
        <p style={{ color: "#64748b" }}>No runs found under prototype outputs.</p>
      ) : null}

      {rows.length > 0 && (
        <div
          style={{
            overflow: "auto",
            maxHeight: "calc(100vh - 11rem)",
            borderRadius: 10,
            border: "1px solid #e2e8f0",
            boxShadow: "0 4px 24px rgba(15, 23, 42, 0.06)",
          }}
        >
          <table
            style={{
              borderCollapse: "separate",
              borderSpacing: 0,
              minWidth: "100%",
              fontSize: "0.78rem",
              background: "#f8fafc",
            }}
          >
            <thead>
              <tr>
                <th
                  scope="col"
                  style={{
                    position: "sticky",
                    left: 0,
                    top: 0,
                    zIndex: 4,
                    width: COL_TOGGLE_W,
                    minWidth: COL_TOGGLE_W,
                    maxWidth: COL_TOGGLE_W,
                    background: "#fff",
                    borderBottom: "2px solid #cbd5e1",
                    borderRight: "1px solid #e2e8f0",
                    padding: "0.5rem 0.25rem",
                    fontSize: "0.65rem",
                    fontWeight: 700,
                    color: "#64748b",
                    textAlign: "center",
                    verticalAlign: "bottom",
                    lineHeight: 1.2,
                  }}
                  title="Hide row"
                >
                  Hide
                </th>
                <th
                  scope="col"
                  style={{
                    position: "sticky",
                    left: COL_TOGGLE_W,
                    top: 0,
                    zIndex: 4,
                    width: COL_FILTER_W,
                    minWidth: COL_FILTER_W,
                    maxWidth: COL_FILTER_W,
                    background: "#fff",
                    borderBottom: "2px solid #cbd5e1",
                    borderRight: "1px solid #e2e8f0",
                    padding: "0.5rem 0.25rem",
                    fontSize: "0.6rem",
                    fontWeight: 700,
                    color: "#64748b",
                    textAlign: "center",
                    verticalAlign: "bottom",
                    lineHeight: 1.15,
                  }}
                  title="Per-row: filter scenario columns"
                >
                  Cols
                </th>
                <th
                  scope="col"
                  style={{
                    position: "sticky",
                    left: STICKY_RUN_LEFT,
                    top: 0,
                    zIndex: 3,
                    background: "#fff",
                    borderBottom: "2px solid #cbd5e1",
                    borderRight: "1px solid #e2e8f0",
                    padding: "0.65rem 0.75rem",
                    textAlign: "left",
                    fontWeight: 700,
                    color: "#334155",
                    whiteSpace: "nowrap",
                    minWidth: 220,
                  }}
                >
                  Run
                </th>
                {visibleColumns.map((col) => (
                  <th
                    key={col || "__single__"}
                    scope="col"
                    style={{
                      position: "sticky",
                      top: 0,
                      zIndex: 2,
                      background:
                        "linear-gradient(180deg, #fff 0%, #f1f5f9 100%)",
                      borderBottom: "2px solid #cbd5e1",
                      borderRight: "1px solid #e2e8f0",
                      padding: "0.55rem 0.5rem",
                      textAlign: "center",
                      fontWeight: 700,
                      color: "#334155",
                      maxWidth: 160,
                      lineHeight: 1.25,
                      verticalAlign: "bottom",
                    }}
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
                const zebra =
                  ri % 2 === 0
                    ? "linear-gradient(90deg, #fff 85%, #f8fafc)"
                    : "linear-gradient(90deg, #f8fafc 85%, #f1f5f9)";
                const zebraRun =
                  ri % 2 === 0
                    ? isFilterSource
                      ? "linear-gradient(90deg, #eef2ff 70%, #e0e7ff)"
                      : "linear-gradient(90deg, #fff 70%, #f8fafc)"
                    : isFilterSource
                      ? "linear-gradient(90deg, #e0e7ff 70%, #c7d2fe)"
                      : "linear-gradient(90deg, #f8fafc 70%, #f1f5f9)";
                return (
                <tr key={row.key}>
                  <td
                    style={{
                      position: "sticky",
                      left: 0,
                      zIndex: 2,
                      width: COL_TOGGLE_W,
                      minWidth: COL_TOGGLE_W,
                      maxWidth: COL_TOGGLE_W,
                      background: zebra,
                      borderBottom: "1px solid #e2e8f0",
                      borderRight: "1px solid #e2e8f0",
                      padding: "0.35rem 0.25rem",
                      textAlign: "center",
                      verticalAlign: "middle",
                    }}
                  >
                    <button
                      type="button"
                      onClick={() => toggleRowHidden(row.key)}
                      title="Hide this row"
                      aria-label={`Hide run ${row.experiment} ${row.timestamp}`}
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        width: 36,
                        height: 32,
                        padding: 0,
                        border: "1px solid #cbd5e1",
                        borderRadius: 6,
                        background: "#fff",
                        color: "#475569",
                        cursor: "pointer",
                      }}
                    >
                      <IconEye />
                    </button>
                  </td>
                  <td
                    style={{
                      position: "sticky",
                      left: COL_TOGGLE_W,
                      zIndex: 2,
                      width: COL_FILTER_W,
                      minWidth: COL_FILTER_W,
                      maxWidth: COL_FILTER_W,
                      background: zebra,
                      borderBottom: "1px solid #e2e8f0",
                      borderRight: "1px solid #e2e8f0",
                      padding: "0.35rem 0.25rem",
                      textAlign: "center",
                      verticalAlign: "middle",
                    }}
                  >
                    <button
                      type="button"
                      onClick={() => toggleColumnFilterForRow(row.key)}
                      title={
                        isFilterSource
                          ? "Clear column filter (click again)"
                          : "Show only scenarios present for this run"
                      }
                      aria-label={
                        isFilterSource
                          ? "Clear scenario column filter"
                          : `Filter columns to scenarios in ${row.experiment} ${row.timestamp}`
                      }
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        width: 36,
                        height: 32,
                        padding: 0,
                        border: isFilterSource
                          ? "2px solid #6366f1"
                          : "1px solid #cbd5e1",
                        borderRadius: 6,
                        background: isFilterSource ? "#e0e7ff" : "#fff",
                        color: isFilterSource ? "#4338ca" : "#475569",
                        cursor: "pointer",
                      }}
                    >
                      <IconFunnel />
                    </button>
                  </td>
                  <th
                    scope="row"
                    style={{
                      position: "sticky",
                      left: STICKY_RUN_LEFT,
                      zIndex: 2,
                      background: zebraRun,
                      borderBottom: "1px solid #e2e8f0",
                      borderRight: "1px solid #e2e8f0",
                      padding: "0.55rem 0.75rem",
                      textAlign: "left",
                      fontWeight: 600,
                      color: "#1e293b",
                      whiteSpace: "nowrap",
                    }}
                  >
                    <span
                      style={{
                        display: "block",
                        fontSize: "0.72rem",
                        fontWeight: 700,
                        color: "#6366f1",
                        marginBottom: 2,
                      }}
                    >
                      {row.experiment}
                    </span>
                    {formatRunRowLabel(row.timestamp)}
                  </th>
                  {visibleColumns.map((col) => {
                    const cell = row.cells[col];
                    const present = cell?.present ?? false;
                    const symbol = cell?.symbol ?? null;
                    const label =
                      !present ? "—" : symbol && symbol.length ? symbol : "…";
                    const styleCore = present
                      ? cellStyle(outcomeColor(symbol))
                      : absentCellStyle();
                    return (
                      <td
                        key={col || "__single__"}
                        title={
                          !present
                            ? "Absent for this run"
                            : symbol
                              ? `Outcome: ${symbol}`
                              : "Present; no classification symbol"
                        }
                        style={{
                          ...styleCore,
                          borderBottom: "1px solid #e2e8f0",
                          borderRight: "1px solid #e2e8f0",
                          padding: "0.45rem 0.35rem",
                          textAlign: "center",
                          fontWeight: present ? 800 : 600,
                          fontSize: "1rem",
                          minWidth: 52,
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

export default function TablePage() {
  return (
    <Suspense fallback={<main style={{ padding: "1rem" }}>Loading…</main>}>
      <TableContent />
    </Suspense>
  );
}
