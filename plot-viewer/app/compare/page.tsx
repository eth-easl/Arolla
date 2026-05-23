"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { METRIC_PANELS, type Timeseries } from "@/lib/compareSchema";

const POLICY_COLORS: Record<string, string> = {
  "no-control":          "#94a3b8",
  "circuit-breaker":     "#f97316",
  "envoy-retry-budget":  "#22c55e",
  "arolla":              "#a855f7",
  "rb-rl-v1-a":          "#0ea5e9",
  "rb-rl-v1-b":          "#06b6d4",
  "rb-rl-v2":            "#6366f1",
  "rb-rl-v3":            "#ef4444",
  "rb-rl-v4":            "#f59e0b",
};

function colorFor(policy: string): string {
  return POLICY_COLORS[policy] ?? "#475569";
}

/** Compare view is locked to the new sweep root; the legacy `prototype/` runs do not ship `timeseries.json`. */
const COMPARE_ROOT = "prototype-new";

type MetricKey = (typeof METRIC_PANELS)[number]["key"];

type ChartRow = { t: number } & Record<string, number | null>;

function reshape(ts: Timeseries, metric: MetricKey): ChartRow[] {
  // Build a unified table: row per t_rel_s present in any policy.
  const allT = new Set<number>();
  for (const p of ts.policies) for (const t of ts.series[p].t_rel_s) allT.add(t);
  const tSorted = [...allT].sort((a, b) => a - b);
  return tSorted.map((t) => {
    const row: ChartRow = { t };
    for (const p of ts.policies) {
      const s = ts.series[p];
      const idx = s.t_rel_s.indexOf(t);
      const arr = (s as unknown as Record<string, (number | null)[] | undefined>)[
        metric
      ];
      const v = idx === -1 ? null : arr?.[idx] ?? null;
      row[p] = v;
    }
    return row;
  });
}

function PageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const root = COMPARE_ROOT;
  const [timestamps, setTimestamps] = useState<string[]>([]);
  const [timestamp, setTimestamp] = useState(
    () => searchParams.get("timestamp") || "",
  );
  const [scenarios, setScenarios] = useState<string[]>([]);
  const [scenario, setScenario] = useState(
    () => searchParams.get("scenario") || "",
  );
  const [data, setData] = useState<Timeseries | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [focused, setFocused] = useState<MetricKey | null>(null);

  useEffect(() => {
    if (!root) return;
    let cancel = false;
    (async () => {
      try {
        const q = new URLSearchParams({ root, experiment: "full-sweep" });
        const res = await fetch(`/api/compare/timestamps?${q.toString()}`);
        const j = await res.json();
        if (cancel) return;
        const list: string[] = j.timestamps ?? [];
        setTimestamps(list);
        setTimestamp((prev) =>
          prev && list.includes(prev) ? prev : list[0] ?? "",
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : "timestamps fetch failed");
      }
    })();
    return () => { cancel = true; };
  }, [root]);

  useEffect(() => {
    if (!root || !timestamp) return;
    let cancel = false;
    (async () => {
      try {
        const q = new URLSearchParams({
          root,
          experiment: "full-sweep",
          timestamp,
        });
        const res = await fetch(`/api/compare/scenarios?${q.toString()}`);
        const j = await res.json();
        if (cancel) return;
        const list: string[] = j.scenarios ?? [];
        setScenarios(list);
        setScenario((prev) =>
          prev && list.includes(prev) ? prev : list[0] ?? "",
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : "scenarios fetch failed");
      }
    })();
    return () => { cancel = true; };
  }, [root, timestamp]);

  useEffect(() => {
    if (!root || !timestamp || !scenario) {
      setData(null);
      return;
    }
    let cancel = false;
    (async () => {
      try {
        const q = new URLSearchParams({
          root,
          experiment: "full-sweep",
          timestamp,
          scenario,
        });
        const res = await fetch(`/api/compare/timeseries?${q.toString()}`);
        if (!res.ok) throw new Error(await res.text());
        const j: Timeseries = await res.json();
        if (cancel) return;
        setData(j);
        setError(null);
      } catch (e) {
        if (!cancel) {
          setError(
            e instanceof Error ? e.message : "timeseries fetch failed",
          );
          setData(null);
        }
      }
    })();
    return () => { cancel = true; };
  }, [root, timestamp, scenario]);

  useEffect(() => {
    const p = new URLSearchParams();
    if (timestamp) p.set("timestamp", timestamp);
    if (scenario) p.set("scenario", scenario);
    router.replace(`?${p.toString()}`, { scroll: false });
  }, [timestamp, scenario, router]);

  const togglePolicy = (p: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });
  };

  const visiblePolicies = useMemo(
    () => (data?.policies ?? []).filter((p) => !hidden.has(p)),
    [data, hidden],
  );

  useEffect(() => {
    if (!focused) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setFocused(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focused]);

  const renderChart = (
    m: (typeof METRIC_PANELS)[number],
    rows: ChartRow[],
    height: number | string,
    lineWidth: number,
  ) => (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart
        data={rows}
        margin={{ top: 6, right: 12, bottom: 18, left: 0 }}
      >
        <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
        <XAxis
          dataKey="t"
          type="number"
          domain={["dataMin", "dataMax"]}
          tick={{ fontSize: 11 }}
          label={{
            value: "t − fault_start (s)",
            position: "insideBottom",
            offset: -2,
            fontSize: 11,
          }}
        />
        <YAxis
          tick={{ fontSize: 11 }}
          domain={"yDomain" in m ? m.yDomain : ["auto", "auto"]}
          label={{
            value: m.yLabel,
            angle: -90,
            position: "insideLeft",
            offset: 12,
            fontSize: 11,
          }}
        />
        <Tooltip
          formatter={(v) => (typeof v === "number" ? v.toFixed(3) : v)}
          labelFormatter={(t) => `t=${t}s`}
        />
        {/*
          Recharts v2 iterates over `LineChart`'s direct children to figure out
          which auxiliary components (ReferenceArea / ReferenceLine / ...)
          should be rendered against the chart's coord system. React Fragments
          break that scan: any ReferenceArea/Line wrapped in a `<>...</>`
          silently disappears. Keep these as direct siblings of `<LineChart>`.
        */}
        {data ? (
          <ReferenceArea
            x1={data.phases.fault_start_rel_s}
            x2={data.phases.fault_end_rel_s}
            fill="#94a3b8"
            fillOpacity={0.25}
            ifOverflow="extendDomain"
          />
        ) : null}
        {data ? (
          <ReferenceLine
            x={data.phases.prefault_start_rel_s}
            stroke="#94a3b8"
            strokeDasharray="2 4"
          />
        ) : null}
        {data ? (
          <ReferenceLine
            x={data.phases.fault_start_rel_s}
            stroke="#dc2626"
          />
        ) : null}
        {data ? (
          <ReferenceLine
            x={data.phases.fault_end_rel_s}
            stroke="#dc2626"
          />
        ) : null}
        {data ? (
          <ReferenceLine
            x={data.phases.recovery_end_rel_s}
            stroke="#16a34a"
            strokeDasharray="2 4"
          />
        ) : null}
        {visiblePolicies.map((p) => (
          <Line
            key={p}
            type="monotone"
            dataKey={p}
            stroke={colorFor(p)}
            strokeWidth={lineWidth}
            dot={false}
            isAnimationActive={false}
            connectNulls
          />
        ))}
        <Legend
          verticalAlign="top"
          height={20}
          iconSize={10}
          wrapperStyle={{ fontSize: 11 }}
        />
      </LineChart>
    </ResponsiveContainer>
  );

  const focusedPanel = useMemo(
    () =>
      focused ? METRIC_PANELS.find((m) => m.key === focused) ?? null : null,
    [focused],
  );

  return (
    <main style={{ padding: 0, margin: 0 }}>
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
          <span
            style={{ fontSize: "0.75rem", fontWeight: 600, color: "#64748b" }}
          >
            Sweep
          </span>
          <select
            value={timestamp}
            onChange={(e) => setTimestamp(e.target.value)}
            style={{ minWidth: 220, padding: "0.4rem 0.5rem" }}
          >
            {timestamps.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span
            style={{ fontSize: "0.75rem", fontWeight: 600, color: "#64748b" }}
          >
            Scenario
          </span>
          <select
            value={scenario}
            onChange={(e) => setScenario(e.target.value)}
            style={{ minWidth: 360, padding: "0.4rem 0.5rem" }}
          >
            {scenarios.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
        {data && (
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "0.4rem",
              alignItems: "center",
            }}
          >
            {data.policies.map((p) => (
              <button
                key={p}
                onClick={() => togglePolicy(p)}
                style={{
                  background: hidden.has(p) ? "#f1f5f9" : colorFor(p),
                  color: hidden.has(p) ? "#475569" : "#fff",
                  border: "1px solid #cbd5e1",
                  borderRadius: 6,
                  padding: "0.25rem 0.55rem",
                  fontSize: "0.78rem",
                  fontWeight: 600,
                  cursor: "pointer",
                  opacity: hidden.has(p) ? 0.6 : 1,
                }}
              >
                {p}
              </button>
            ))}
          </div>
        )}
      </section>

      {error && (
        <p role="alert" style={{ color: "#b91c1c", padding: "0.5rem 0.75rem" }}>
          {error}
        </p>
      )}

      {data && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
            gap: 12,
            padding: 12,
          }}
        >
          {METRIC_PANELS.map((m) => {
            const rows = reshape(data, m.key);
            const rlOnly = m.key.startsWith("rl_");
            const hasRl = visiblePolicies.some((p) => p.startsWith("rb-rl-"));
            if (rlOnly && !hasRl) return null;
            return (
              <div
                key={m.key}
                style={{
                  background: "#fff",
                  border: "1px solid #e2e8f0",
                  borderRadius: 8,
                  padding: 8,
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: 6,
                    gap: 8,
                  }}
                >
                  <h3
                    style={{
                      margin: 0,
                      fontSize: "0.85rem",
                      color: "#1e293b",
                    }}
                  >
                    {m.title}
                  </h3>
                  <button
                    type="button"
                    onClick={() => setFocused(m.key)}
                    title="Focus this chart"
                    style={{
                      background: "#f8fafc",
                      border: "1px solid #cbd5e1",
                      borderRadius: 6,
                      padding: "0.15rem 0.5rem",
                      fontSize: "0.7rem",
                      fontWeight: 600,
                      color: "#475569",
                      cursor: "pointer",
                    }}
                  >
                    Focus
                  </button>
                </div>
                {renderChart(m, rows, 260, 2)}
              </div>
            );
          })}
        </div>
      )}

      {data && focusedPanel && (
        <div
          role="dialog"
          aria-modal="true"
          onClick={() => setFocused(null)}
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(15, 23, 42, 0.55)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 50,
            padding: "2vh 2vw",
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              background: "#fff",
              border: "1px solid #e2e8f0",
              borderRadius: 12,
              padding: 16,
              boxShadow: "0 20px 50px rgba(15, 23, 42, 0.25)",
              width: "min(96vw, 1400px)",
              height: "min(92vh, 900px)",
              display: "flex",
              flexDirection: "column",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: 10,
                gap: 12,
              }}
            >
              <h2
                style={{
                  margin: 0,
                  fontSize: "1.05rem",
                  color: "#0f172a",
                }}
              >
                {focusedPanel.title}
              </h2>
              <button
                type="button"
                onClick={() => setFocused(null)}
                title="Close (Esc)"
                style={{
                  background: "#f1f5f9",
                  border: "1px solid #cbd5e1",
                  borderRadius: 6,
                  padding: "0.3rem 0.7rem",
                  fontSize: "0.8rem",
                  fontWeight: 600,
                  color: "#334155",
                  cursor: "pointer",
                }}
              >
                Close
              </button>
            </div>
            <div style={{ flex: 1, minHeight: 0 }}>
              {renderChart(
                focusedPanel,
                reshape(data, focusedPanel.key),
                "100%",
                2.5,
              )}
            </div>
          </div>
        </div>
      )}
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
