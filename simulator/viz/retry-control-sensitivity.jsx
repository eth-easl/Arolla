import { useState, useCallback } from "react";

// ============================================================
// API Client
// ============================================================

const API_BASE = "http://localhost:8642";

async function apiRun(configPath) {
  const res = await fetch(`${API_BASE}/api/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config_path: configPath }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

async function apiSweep(configPath, paramPath, values) {
  const res = await fetch(`${API_BASE}/api/sweep`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config_path: configPath, param_path: paramPath, values }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

async function apiHeatmap(configPath, paramPathX, valuesX, paramPathY, valuesY) {
  const res = await fetch(`${API_BASE}/api/heatmap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      config_path: configPath,
      param_path_x: paramPathX, values_x: valuesX,
      param_path_y: paramPathY, values_y: valuesY,
    }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

async function apiListConfigs() {
  const res = await fetch(`${API_BASE}/api/configs`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

async function apiParamCompare(configPath, paramName, values) {
  const res = await fetch(`${API_BASE}/api/param_compare`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config_path: configPath, param_name: paramName, values }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

async function apiSaveResults(analysisType, data, label = "") {
  const res = await fetch(`${API_BASE}/api/save_results`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ analysis_type: analysisType, data, label }),
  });
  if (!res.ok) throw new Error(`Save error: ${res.status}`);
  return res.json();
}

// ============================================================
// Strategy Definitions (derived from motivation-varied.yaml)
// ============================================================

function displayName(s) { return s.replace(/arolla[\w_]*/gi, "Arolla").replace(/sysname/gi, "Arolla"); }

// Strategy visual styles: vivid colors + distinct dash patterns (matching paper figures)
const STRATEGY_STYLES = {
  no_retries:                 { color: "#8b8b8b", dash: "3,4",       width: 3   },
  three_retries:              { color: "#6f95e7", dash: "",           width: 3 },
  exponential_backoff_jitter: { color: "#ea7317", dash: "10,4",      width: 3 },
  no_control:                { color: "#e83c0d", dash: "6,3",       width: 3   },
  circuit_breaker:            { color: "#ea7317", dash: "3,5",       width: 3 },
  retry_budget:               { color: "#6f95e7", dash: "10,3,3,3",  width: 3 },
  arolla_retry_budget:        { color: "#40916c", dash: "",           width: 3 },
  arolla_budget:              { color: "#40916c", dash: "",           width: 3 },
  arolla:                     { color: "#40916c", dash: "",           width: 2 },
};
const STYLE_LIST = [
  { color: "#2563eb", dash: "",           width: 2.5 },
  { color: "#ea7317", dash: "10,4",       width: 2.5 },
  { color: "#d63384", dash: "3,5",        width: 2.5 },
  { color: "#40916c", dash: "10,3,3,3",   width: 2.5 },
  { color: "#006d77", dash: "",           width: 3   },
  { color: "#8b6914", dash: "6,3",        width: 2   },
  { color: "#8b8b8b", dash: "3,4",        width: 2   },
];
function getStyle(name, idx) {
  if (name) {
    const key = name.toLowerCase().replace(/-/g, "_");
    if (STRATEGY_STYLES[key]) return STRATEGY_STYLES[key];
    for (const [k, v] of Object.entries(STRATEGY_STYLES)) {
      if (key.includes(k) || k.includes(key)) return v;
    }
  }
  return STYLE_LIST[idx % STYLE_LIST.length];
}

const STRATEGIES = [
  {
    key: "three_retries",
    label: "Fixed Retries",
    color: "#2563eb",
    clientName: "three_retries",
    sweeps: [
      { param: "Max Attempts", path: "clients.1.retry.max_attempts", values: [1, 2, 3, 4, 5, 6, 8] },
      { param: "Retry Delay (ms)", path: "clients.1.retry.delay_ms", values: [0, 50, 100, 200, 500, 1000] },
    ],
  },
  {
    key: "exponential_backoff_jitter",
    label: "Exp Backoff + Jitter",
    color: "#ea7317",
    clientName: "exponential_backoff_jitter",
    sweeps: [
      { param: "Max Attempts", path: "clients.2.retry.max_attempts", values: [1, 2, 3, 4, 5, 6, 8] },
      { param: "Initial Delay (ms)", path: "clients.2.retry.initial_delay_ms", values: [10, 50, 100, 200, 500, 1000] },
      { param: "Max Delay (ms)", path: "clients.2.retry.max_delay_ms", values: [200, 500, 1000, 2000, 5000, 10000] },
    ],
  },
  {
    key: "circuit_breaker",
    label: "Circuit Breaker",
    color: "#d63384",
    clientName: "circuit_breaker",
    sweeps: [
      { param: "Failure Threshold", path: "clients.3.circuit_breaker.failure_threshold", values: [0.01, 0.05, 0.1, 0.2, 0.3, 0.5] },
      { param: "Half-Open Delay (ms)", path: "clients.3.circuit_breaker.half_open_delay_ms", values: [50, 100, 200, 500, 1000, 2000] },
      { param: "Window Duration (ms)", path: "clients.3.circuit_breaker.window_duration_ms", values: [100, 500, 1000, 2000, 5000] },
    ],
  },
  {
    key: "retry_budget",
    label: "Client Retry Budget",
    color: "#40916c",
    clientName: "retry_budget",
    sweeps: [
      { param: "Budget Ratio", path: "clients.4.retry_budget.budget_ratio", values: [0.01, 0.05, 0.1, 0.2, 0.5, 1.0] },
      { param: "Max Retries", path: "clients.4.retry_budget.max_retries", values: [1, 5, 10, 20, 50, 100] },
    ],
  },
];

// ============================================================
// Utilities
// ============================================================

function smoothArray(arr, windowSize) {
  const half = Math.floor(windowSize / 2);
  return arr.map((_, i) => {
    let sum = 0, count = 0;
    for (let j = Math.max(0, i - half); j <= Math.min(arr.length - 1, i + half); j++) {
      if (!isNaN(arr[j])) { sum += arr[j]; count++; }
    }
    return count > 0 ? sum / count : 0;
  });
}

function heatColor(sr) {
  const t = Math.max(0, Math.min(1, sr));
  if (t < 0.5) {
    const s = t * 2;
    const r = Math.round(178 + s * 42);
    const g = Math.round(60 + s * 120);
    const b = Math.round(60 + s * 100);
    return `rgb(${r}, ${g}, ${b})`;
  }
  const s = (t - 0.5) * 2;
  const r = Math.round(220 - s * 140);
  const g = Math.round(180 + s * 10);
  const b = Math.round(160 - s * 50);
  return `rgb(${r}, ${g}, ${b})`;
}

function fmtVal(v) {
  if (typeof v !== "number") return String(v);
  return v < 1 && v > 0 ? v.toFixed(2) : v < 10 ? v.toFixed(1) : String(Math.round(v));
}

function clientColor(name, idx) {
  return getStyle(name, idx).color;
}

// ============================================================
// Generic Metric Time-Series Chart
// ============================================================

function MetricTimeSeriesChart({ clients, faultEvents, metricKey, yLabel, yFormat, yDomain, T, width = 700, height = 260 }) {
  if (!clients || clients.length === 0) return null;
  const padL = 65, padR = 20, padT = 20, padB = 44;
  const cW = width - padL - padR;
  const cH = height - padT - padB;

  let maxT = 0;
  for (const c of clients) {
    const ts = c.timeseries || [];
    if (ts.length > 0 && ts[ts.length - 1].timepoint > maxT) maxT = ts[ts.length - 1].timepoint;
  }
  if (maxT === 0) return null;

  // Determine Y range
  let yMin = yDomain ? yDomain[0] : Infinity;
  let yMax = yDomain ? yDomain[1] : -Infinity;
  if (!yDomain) {
    for (const c of clients) {
      for (const d of (c.timeseries || [])) {
        let v;
        if (metricKey === "_success_rate") {
          v = d.root_requests > 0 ? d.success_root / d.root_requests : NaN;
        } else {
          v = d[metricKey];
        }
        if (v != null && !isNaN(v)) {
          if (v < yMin) yMin = v;
          if (v > yMax) yMax = v;
        }
      }
    }
    if (yMin === Infinity) { yMin = 0; yMax = 1; }
    const pad = (yMax - yMin) * 0.1 || 0.1;
    yMin = Math.max(0, yMin - pad);
    yMax = yMax + pad;
  }

  const xScale = (t) => padL + (t / maxT) * cW;
  const yScale = (v) => padT + ((yMax - v) / (yMax - yMin || 1)) * cH;

  const xTicks = [];
  for (let t = 0; t <= maxT; t += 10) xTicks.push(t);
  const yTicks = 5;
  const yTickVals = [];
  for (let i = 0; i <= yTicks; i++) yTickVals.push(yMin + (i / yTicks) * (yMax - yMin));

  const fmt = yFormat || ((v) => v < 1 && v >= 0 ? `${(v * 100).toFixed(0)}%` : v.toFixed(1));

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {yTickVals.map((v, i) => (
        <g key={i}>
          <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
          <text x={padL - 10} y={yScale(v) + 4} textAnchor="end" fill={T.svgLabel} fontSize={11}
            fontFamily="'JetBrains Mono', monospace">{fmt(v)}</text>
        </g>
      ))}

      {(faultEvents || []).map((fe, i) => (
        <g key={`f-${i}`}>
          <rect x={xScale(fe.start_time_s)} y={padT}
            width={Math.max(0, xScale(fe.end_time_s) - xScale(fe.start_time_s))} height={cH}
            fill="#e59a9a" opacity={0.12} rx={2} />
          {i === 0 && (
            <text x={(xScale(fe.start_time_s) + xScale(fe.end_time_s)) / 2} y={padT + 14}
              textAnchor="middle" fill="#e59a9a" fontSize={10} fontWeight={600}
              fontFamily="'JetBrains Mono', monospace">Fault</text>
          )}
        </g>
      ))}

      {clients.map((c, ci) => {
        const ts = c.timeseries || [];
        if (ts.length < 2) return null;
        const rawVals = ts.map((d) => {
          if (metricKey === "_success_rate") {
            return d.root_requests > 0 ? d.success_root / d.root_requests : NaN;
          }
          return d[metricKey] != null ? d[metricKey] : NaN;
        });
        const vals = smoothArray(rawVals, 3);
        const style = getStyle(c.name, ci);

        const segments = [];
        let seg = [];
        for (let i = 0; i < vals.length; i++) {
          if (!isNaN(vals[i])) {
            const yVal = Math.max(yMin, Math.min(yMax, vals[i]));
            seg.push(`${xScale(ts[i].timepoint).toFixed(1)},${yScale(yVal).toFixed(1)}`);
          } else if (seg.length > 0) {
            segments.push(seg);
            seg = [];
          }
        }
        if (seg.length > 0) segments.push(seg);

        return segments.map((pts, si) => (
          <polyline key={`${ci}-${si}`} points={pts.join(" ")} fill="none"
            stroke={style.color} strokeWidth={style.width}
            strokeDasharray={style.dash || undefined} opacity={0.9} />
        ));
      })}

      {xTicks.map((t) => (
        <text key={t} x={xScale(t)} y={height - 10} textAnchor="middle" fill={T.svgLabel} fontSize={10}
          fontFamily="'JetBrains Mono', monospace">{t}</text>
      ))}

      <text x={padL + cW / 2} y={height} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace" fontWeight={500}>Time (s)</text>
      <text x={14} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace" fontWeight={500}
        transform={`rotate(-90, 14, ${padT + cH / 2})`}>{yLabel}</text>
    </svg>
  );
}

// ============================================================
// Load Decomposition Chart (stacked area per client)
// ============================================================

function LoadDecompositionChart({ clients, faultEvents, T, width = 700, height = 280 }) {
  const [selectedIdx, setSelectedIdx] = useState(0);
  if (!clients || clients.length === 0) return null;
  const c = clients[selectedIdx] || clients[0];
  const ts = c.timeseries || [];
  if (ts.length < 2) return null;

  const padL = 65, padR = 20, padT = 20, padB = 44;
  const cW = width - padL - padR;
  const cH = height - padT - padB;

  const maxT = ts[ts.length - 1].timepoint || 1;

  // Compute stacked layers per bucket
  const layers = ts.map((d) => {
    const succFirst = d.success_root || 0;
    const succRetry = Math.max(0, (d.retries || 0) - (d.failure_retry || 0));
    const failFirst = d.failure_root || 0;
    const failRetry = d.failure_retry || 0;
    return { t: d.timepoint, succFirst, succRetry, failFirst, failRetry };
  });

  let yMax = 0;
  for (const l of layers) {
    const total = l.succFirst + l.succRetry + l.failFirst + l.failRetry;
    if (total > yMax) yMax = total;
  }
  yMax = yMax * 1.08 || 1;

  const xScale = (t) => padL + (t / maxT) * cW;
  const yScale = (v) => padT + ((yMax - v) / yMax) * cH;

  // Build stacked area paths (bottom-up order)
  const stackOrder = [
    { key: "succFirst", color: "#388e3c", label: "Success (1st attempt)" },
    { key: "succRetry", color: "#66bb6a", label: "Success (retry)" },
    { key: "failFirst", color: "#e07b39", label: "Failed (1st attempt)" },
    { key: "failRetry", color: "#c75050", label: "Failed (retry)" },
  ];

  // Compute cumulative baselines for each point
  const baselines = layers.map(() => 0);
  const areaPaths = stackOrder.map(({ key, color, label }) => {
    const topPoints = [];
    const bottomPoints = [];
    for (let i = 0; i < layers.length; i++) {
      const base = baselines[i];
      const val = layers[i][key];
      const x = xScale(layers[i].t).toFixed(1);
      bottomPoints.push(`${x},${yScale(base).toFixed(1)}`);
      topPoints.push(`${x},${yScale(base + val).toFixed(1)}`);
      baselines[i] = base + val;
    }
    const d = `M${topPoints.join(" L")} L${bottomPoints.reverse().join(" L")} Z`;
    return { d, color, label };
  });

  const xTicks = [];
  for (let t = 0; t <= maxT; t += 10) xTicks.push(t);
  const yTicks = 5;
  const yTickVals = [];
  for (let i = 0; i <= yTicks; i++) yTickVals.push((i / yTicks) * yMax);

  return (
    <div>
      {/* Client selector */}
      {clients.length > 1 && (
        <div style={{ marginBottom: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
          {clients.map((cl, i) => (
            <button key={i} onClick={() => setSelectedIdx(i)} style={{
              padding: "3px 10px", fontSize: 11, borderRadius: 4, cursor: "pointer",
              background: i === selectedIdx ? getStyle(cl.name, i).color : "transparent",
              color: i === selectedIdx ? "#fff" : T.dim,
              border: `1px solid ${i === selectedIdx ? getStyle(cl.name, i).color : T.border}`,
              fontFamily: "'JetBrains Mono', monospace",
            }}>{displayName(cl.name)}</button>
          ))}
        </div>
      )}

      <svg width={width} height={height} style={{ overflow: "visible" }}>
        {/* Y grid */}
        {yTickVals.map((v, i) => (
          <g key={i}>
            <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
            <text x={padL - 10} y={yScale(v) + 4} textAnchor="end" fill={T.svgLabel} fontSize={11}
              fontFamily="'JetBrains Mono', monospace">{v.toFixed(0)}</text>
          </g>
        ))}

        {/* Fault shading */}
        {(faultEvents || []).map((fe, i) => (
          <g key={`f-${i}`}>
            <rect x={xScale(fe.start_time_s)} y={padT}
              width={Math.max(0, xScale(fe.end_time_s) - xScale(fe.start_time_s))} height={cH}
              fill="#e59a9a" opacity={0.12} rx={2} />
            {i === 0 && (
              <text x={(xScale(fe.start_time_s) + xScale(fe.end_time_s)) / 2} y={padT + 14}
                textAnchor="middle" fill="#e59a9a" fontSize={10} fontWeight={600}
                fontFamily="'JetBrains Mono', monospace">Fault</text>
            )}
          </g>
        ))}

        {/* Stacked areas */}
        {areaPaths.map((a, i) => (
          <path key={i} d={a.d} fill={a.color} opacity={0.7} />
        ))}

        {/* X ticks */}
        {xTicks.map((t) => (
          <text key={t} x={xScale(t)} y={height - 10} textAnchor="middle" fill={T.svgLabel} fontSize={10}
            fontFamily="'JetBrains Mono', monospace">{t}</text>
        ))}

        <text x={padL + cW / 2} y={height} textAnchor="middle" fill={T.svgAxis} fontSize={12}
          fontFamily="'JetBrains Mono', monospace" fontWeight={500}>Time (s)</text>
        <text x={14} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
          fontFamily="'JetBrains Mono', monospace" fontWeight={500}
          transform={`rotate(-90, 14, ${padT + cH / 2})`}>Requests / bucket</text>
      </svg>

      {/* Legend */}
      <div style={{ display: "flex", gap: 16, marginTop: 6, flexWrap: "wrap" }}>
        {stackOrder.map((s) => (
          <div key={s.key} style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <div style={{ width: 12, height: 12, borderRadius: 2, background: s.color, opacity: 0.7 }} />
            <span style={{ fontSize: 10, color: T.dim, fontFamily: "'JetBrains Mono', monospace" }}>{s.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ============================================================
// Chart Legend
// ============================================================

function ChartLegend({ clients, T }) {
  if (!clients || clients.length === 0) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "8px 18px", marginBottom: 10 }}>
      {clients.map((c, i) => {
        const style = getStyle(c.name, i);
        return (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <svg width={24} height={6}>
              <line x1={0} y1={3} x2={24} y2={3}
                stroke={style.color} strokeWidth={style.width}
                strokeDasharray={style.dash || undefined} />
            </svg>
            <span style={{ fontSize: 11, color: T.text, fontFamily: "'JetBrains Mono', monospace" }}>
              {displayName(c.name)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ============================================================
// Grouped Bar Chart — parameter comparison across strategies
// ============================================================

function GroupedBarChart({ compareResult, metricKey, yLabel, yFormat, T, width = 700, height = 240 }) {
  if (!compareResult || !compareResult.results || compareResult.results.length === 0) return null;

  const padL = 65, padR = 20, padT = 16, padB = 44;
  const cW = width - padL - padR;
  const cH = height - padT - padB;

  const paramValues = compareResult.results.map(r => r.param_value);
  const strategies = compareResult.results[0].per_client.map(c => c.name);
  const nGroups = paramValues.length;
  const nBars = strategies.length;

  // Compute max value for Y scale
  let maxVal = 0;
  for (const r of compareResult.results) {
    for (const c of r.per_client) {
      const v = c[metricKey];
      if (v != null && v > maxVal) maxVal = v;
    }
  }
  if (maxVal === 0) maxVal = 1;
  // Round up for nice ticks
  const yMax = metricKey === "success_rate" || metricKey === "retry_efficiency"
    ? Math.min(1.05, Math.ceil(maxVal * 10) / 10 + 0.05)
    : Math.ceil(maxVal * 1.15);
  if (yMax === 0) return null;

  const groupWidth = cW / nGroups;
  const barGap = 2;
  const groupPad = groupWidth * 0.15;
  const barAreaWidth = groupWidth - groupPad * 2;
  const barWidth = Math.max(4, (barAreaWidth - barGap * (nBars - 1)) / nBars);

  const yScale = (v) => padT + (1 - v / yMax) * cH;
  const xGroupCenter = (gi) => padL + gi * groupWidth + groupWidth / 2;

  // Y-axis ticks
  const nTicks = 5;
  const yTicks = [];
  for (let i = 0; i <= nTicks; i++) {
    yTicks.push((yMax / nTicks) * i);
  }

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {/* Grid lines */}
      {yTicks.map((v, i) => (
        <g key={i}>
          <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
          <text x={padL - 8} y={yScale(v) + 4} textAnchor="end" fontSize={10} fill={T.svgLabel} fontFamily="'JetBrains Mono', monospace">
            {yFormat ? yFormat(v) : v.toFixed(2)}
          </text>
        </g>
      ))}

      {/* Bars */}
      {compareResult.results.map((r, gi) => {
        const groupX = padL + gi * groupWidth + groupPad;
        return r.per_client.map((c, bi) => {
          const v = c[metricKey] != null ? c[metricKey] : 0;
          const style = getStyle(c.name, bi);
          const x = groupX + bi * (barWidth + barGap);
          const barH = Math.max(0, (v / yMax) * cH);
          const y = padT + cH - barH;
          return (
            <g key={`${gi}-${bi}`}>
              <rect x={x} y={y} width={barWidth} height={barH}
                fill={style.color} opacity={0.85} rx={1} />
              {/* Value label on top of bar if space */}
              {barH > 14 && (
                <text x={x + barWidth / 2} y={y - 3} textAnchor="middle" fontSize={8}
                  fill={T.svgLabel} fontFamily="'JetBrains Mono', monospace">
                  {yFormat ? yFormat(v) : v.toFixed(2)}
                </text>
              )}
            </g>
          );
        });
      })}

      {/* X-axis labels */}
      {paramValues.map((pv, gi) => (
        <text key={gi} x={xGroupCenter(gi)} y={padT + cH + 18} textAnchor="middle"
          fontSize={11} fill={T.svgAxis} fontFamily="'JetBrains Mono', monospace">
          {typeof pv === "number" ? (pv < 1 ? pv.toFixed(2) : pv.toFixed(0)) : String(pv)}
        </text>
      ))}

      {/* X-axis label */}
      <text x={padL + cW / 2} y={height - 4} textAnchor="middle"
        fontSize={11} fill={T.svgLabel} fontFamily="'JetBrains Mono', monospace">
        {compareResult.param_name}
      </text>

      {/* Y-axis label */}
      <text x={14} y={padT + cH / 2} textAnchor="middle"
        fontSize={11} fill={T.svgLabel} fontFamily="'JetBrains Mono', monospace"
        transform={`rotate(-90, 14, ${padT + cH / 2})`}>
        {yLabel}
      </text>

      {/* Axes */}
      <line x1={padL} y1={padT} x2={padL} y2={padT + cH} stroke={T.svgAxis} strokeWidth={1} />
      <line x1={padL} y1={padT + cH} x2={padL + cW} y2={padT + cH} stroke={T.svgAxis} strokeWidth={1} />
    </svg>
  );
}


// ============================================================
// Sweep Chart — single parameter with cliff detection
// ============================================================

function SweepChart({ sweepResults, targetClient, strategyColor, T, width = 640, height = 320 }) {
  if (!sweepResults || sweepResults.length === 0) return null;
  const padL = 65, padR = 20, padT = 25, padB = 50;
  const cW = width - padL - padR;
  const cH = height - padT - padB;
  const n = sweepResults.length;
  const yScale = (v) => padT + (1 - v) * cH;
  const xScale = (i) => padL + (i / Math.max(1, n - 1)) * cW;

  const data = sweepResults.map((d) => {
    let sr = d.success_rate || 0;
    if (d.per_client && targetClient) {
      const pc = d.per_client.find((c) => c.name === targetClient);
      if (pc) sr = pc.success_rate;
    }
    return { ...d, client_sr: sr };
  });

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {[0, 0.25, 0.5, 0.75, 1].map((v) => (
        <g key={v}>
          <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
          <text x={padL - 10} y={yScale(v) + 4} textAnchor="end" fill={T.svgLabel} fontSize={11}
            fontFamily="'JetBrains Mono', monospace">{(v * 100).toFixed(0)}%</text>
        </g>
      ))}

      <polyline
        points={data.map((d, i) => `${xScale(i)},${yScale(d.client_sr)}`).join(" ")}
        fill="none" stroke={strategyColor || T.accent} strokeWidth={2.5} opacity={0.9} />

      {data.map((d, i) => (
        <circle key={i} cx={xScale(i)} cy={yScale(d.client_sr)} r={4}
          fill={strategyColor || T.accent} opacity={0.9} />
      ))}

      {data.map((d, i) => (
        <text key={i} x={xScale(i)} y={height - 10} textAnchor="middle" fill={T.svgLabel} fontSize={11}
          fontFamily="'JetBrains Mono', monospace">{fmtVal(d.param_value)}</text>
      ))}

      {(() => {
        let maxDrop = 0, dropIdx = 0;
        for (let i = 1; i < n; i++) {
          const drop = data[i - 1].client_sr - data[i].client_sr;
          if (drop > maxDrop) { maxDrop = drop; dropIdx = i; }
        }
        if (maxDrop > 0.05) {
          return (
            <g>
              <line x1={xScale(dropIdx)} y1={padT} x2={xScale(dropIdx)} y2={padT + cH}
                stroke="#b74444" strokeWidth={1.5} strokeDasharray="4,3" opacity={0.7} />
              <text x={xScale(dropIdx)} y={padT - 4} textAnchor="middle" fill="#b74444" fontSize={10}
                fontFamily="'JetBrains Mono', monospace">cliff</text>
            </g>
          );
        }
        return null;
      })()}

      <text x={padL + cW / 2} y={height} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace">Parameter Value</text>
      <text x={10} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace" transform={`rotate(-90, 10, ${padT + cH / 2})`}>
        Success Rate</text>
    </svg>
  );
}

// ============================================================
// Tornado Chart — parameter impact ranking
// ============================================================

function TornadoChart({ tornadoData, T, width = 640 }) {
  if (!tornadoData || tornadoData.length === 0) return null;
  const barH = 30, gap = 6;
  const padL = 220, padR = 80, padT = 30, padB = 40;
  const totalH = padT + padB + tornadoData.length * (barH + gap);
  const cW = width - padL - padR;

  const maxDelta = Math.max(0.01, ...tornadoData.map((d) => Math.abs(d.delta)));
  const xScale = (delta) => padL + ((delta / maxDelta + 1) / 2) * cW;
  const centerX = xScale(0);

  return (
    <svg width={width} height={totalH} style={{ overflow: "visible" }}>
      <line x1={centerX} y1={padT - 8} x2={centerX} y2={totalH - padB + 8}
        stroke={T.grid} strokeWidth={1} />

      {tornadoData.map((d, i) => {
        const y = padT + i * (barH + gap);
        const xMin = xScale(Math.min(0, d.delta));
        const xMax = xScale(Math.max(0, d.delta));
        const barWidth = Math.max(3, xMax - xMin);
        return (
          <g key={i}>
            <text x={padL - 10} y={y + barH / 2 + 4} textAnchor="end" fill={T.text} fontSize={11}
              fontFamily="'JetBrains Mono', monospace">
              <tspan fill={d.color} fontWeight={600}>{d.strategy}</tspan>
              <tspan fill={T.muted}> / {d.param}</tspan>
            </text>
            <rect x={xMin} y={y} width={barWidth} height={barH} fill={d.color} opacity={0.75} rx={3} />
            <text x={d.delta >= 0 ? xMax + 6 : xMin - 6} y={y + barH / 2 + 4}
              textAnchor={d.delta >= 0 ? "start" : "end"} fill={d.color} fontSize={11}
              fontFamily="'JetBrains Mono', monospace" fontWeight={600}>
              {d.delta > 0 ? "+" : ""}{(d.delta * 100).toFixed(1)}%
            </text>
          </g>
        );
      })}

      {[-1, -0.5, 0, 0.5, 1].map((f) => (
        <text key={f} x={xScale(f * maxDelta)} y={totalH - 14} textAnchor="middle"
          fill={T.svgLabel} fontSize={10} fontFamily="'JetBrains Mono', monospace">
          {f === 0 ? "0" : `${(f * maxDelta * 100).toFixed(0)}%`}
        </text>
      ))}

      <text x={padL + cW / 2} y={totalH - 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace">Change in Success Rate (min→max param)</text>
    </svg>
  );
}

// ============================================================
// Heatmap Chart — 2D parameter interaction
// ============================================================

function HeatmapChart({ heatmapData, valuesX, valuesY, labelX, labelY, targetClient, T, width = 520, height = 420 }) {
  if (!heatmapData || valuesX.length === 0 || valuesY.length === 0) return null;
  const padL = 85, padR = 65, padT = 20, padB = 65;
  const cW = width - padL - padR;
  const cH = height - padT - padB;
  const cellW = cW / valuesX.length;
  const cellH = cH / valuesY.length;

  const dataMap = {};
  for (const d of heatmapData) {
    let sr = d.success_rate || 0;
    if (d.per_client && targetClient) {
      const pc = d.per_client.find((c) => c.name === targetClient);
      if (pc) sr = pc.success_rate;
    }
    dataMap[`${d.x}_${d.y}`] = sr;
  }

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {valuesY.map((vy, yi) =>
        valuesX.map((vx, xi) => {
          const sr = dataMap[`${vx}_${vy}`] ?? 0;
          const x = padL + xi * cellW;
          const y = padT + (valuesY.length - 1 - yi) * cellH;
          return (
            <g key={`${xi}_${yi}`}>
              <rect x={x + 1} y={y + 1} width={cellW - 2} height={cellH - 2}
                fill={heatColor(sr)} opacity={0.88} rx={3} />
              <text x={x + cellW / 2} y={y + cellH / 2 + 4} textAnchor="middle"
                fill="#fff" fontSize={11} fontWeight={600}
                fontFamily="'JetBrains Mono', monospace">{(sr * 100).toFixed(0)}%</text>
            </g>
          );
        })
      )}

      {valuesX.map((v, i) => (
        <text key={i} x={padL + i * cellW + cellW / 2} y={height - 22} textAnchor="middle"
          fill={T.svgLabel} fontSize={10} fontFamily="'JetBrains Mono', monospace"
          transform={`rotate(-30, ${padL + i * cellW + cellW / 2}, ${height - 22})`}>
          {fmtVal(v)}</text>
      ))}

      {valuesY.map((v, i) => (
        <text key={i} x={padL - 10}
          y={padT + (valuesY.length - 1 - i) * cellH + cellH / 2 + 4}
          textAnchor="end" fill={T.svgLabel} fontSize={10}
          fontFamily="'JetBrains Mono', monospace">{fmtVal(v)}</text>
      ))}

      <text x={padL + cW / 2} y={height - 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace">{labelX}</text>
      <text x={14} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace" transform={`rotate(-90, 14, ${padT + cH / 2})`}>
        {labelY}</text>

      <g transform={`translate(${width - 55}, ${padT})`}>
        {[1, 0.75, 0.5, 0.25, 0].map((v, i) => (
          <g key={i} transform={`translate(0, ${i * 22})`}>
            <rect x={0} y={0} width={14} height={18} fill={heatColor(v)} opacity={0.88} rx={2} />
            <text x={20} y={13} fill={T.svgLabel} fontSize={9}
              fontFamily="'JetBrains Mono', monospace">{(v * 100).toFixed(0)}%</text>
          </g>
        ))}
      </g>
    </svg>
  );
}

// ============================================================
// Progress Bar
// ============================================================

function ProgressBar({ info, T }) {
  const [elapsed, setElapsed] = useState(0);
  const [pulseX, setPulseX] = useState(0);

  useEffect(() => {
    if (!info) { setElapsed(0); return; }
    const start = info.startTime || Date.now();
    const timer = setInterval(() => {
      setElapsed((Date.now() - start) / 1000);
      setPulseX((prev) => (prev + 2) % 200);
    }, 100);
    return () => clearInterval(timer);
  }, [info]);

  if (!info) return null;
  const { text, current, total } = info;
  const pct = total > 0 ? Math.min(100, (current / total) * 100) : null;

  return (
    <div style={{
      padding: "12px 14px", background: T.panel, border: `1px solid ${T.border}`,
      borderRadius: 8, marginBottom: 14,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={{ fontSize: 12, color: T.text }}>{text}</span>
        <span style={{ fontSize: 12, color: T.accent, fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>
          {elapsed.toFixed(1)}s
        </span>
      </div>
      <div style={{
        height: 6, background: T.grid, borderRadius: 3, overflow: "hidden", position: "relative",
      }}>
        {pct !== null ? (
          <div style={{
            height: "100%", background: T.accent, width: `${pct}%`, borderRadius: 3,
            transition: "width 0.3s ease", opacity: 0.85,
          }} />
        ) : (
          <div style={{
            height: "100%", background: T.accent, width: "30%", borderRadius: 3,
            position: "absolute", left: `${pulseX - 30}%`, opacity: 0.7,
          }} />
        )}
      </div>
      {total > 0 && (
        <div style={{ fontSize: 11, color: T.faint, marginTop: 6 }}>
          {current} / {total} simulations complete
        </div>
      )}
    </div>
  );
}

// ============================================================
// Main App
// ============================================================

export default function App() {
  const T = useTheme();
  const [configs, setConfigs] = useState(null);
  const [activeTab, setActiveTab] = useState("dashboard");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [progressInfo, setProgressInfo] = useState(null);


  // --- Tab 1: Parameter Sensitivity ---
  const [sensConfigPath, setSensConfigPath] = useState("");
  const [sensSection, setSensSection] = useState("sweep"); // sweep | tornado | heatmap
  // Sweep state
  const [selectedStrategy, setSelectedStrategy] = useState(0);
  const [selectedSweep, setSelectedSweep] = useState(0);
  const [sweepResult, setSweepResult] = useState(null);
  // Tornado state
  const [tornadoData, setTornadoData] = useState(null);
  // Heatmap state
  const [hmStrategy, setHmStrategy] = useState(0);
  const [hmParamX, setHmParamX] = useState(0);
  const [hmParamY, setHmParamY] = useState(1);
  const [heatmapResult, setHeatmapResult] = useState(null);

  // --- Tab 2: Metrics Dashboard ---
  const [dashConfigPath, setDashConfigPath] = useState("");
  const [dashResult, setDashResult] = useState(null);

  // --- Parameter Comparison (within Dashboard tab) ---
  const [compareParam, setCompareParam] = useState("p_fail");
  const [compareValues, setCompareValues] = useState("0.1, 0.3, 0.5, 0.7, 0.9");
  const [compareResult, setCompareResult] = useState(null);
  const [compareLoading, setCompareLoading] = useState(false);

  const PARAM_PRESETS = {
    p_fail:             { label: "Failure Rate",        defaults: "0.1, 0.3, 0.5, 0.7, 0.9" },
    base_rps:           { label: "Base RPS",            defaults: "50, 100, 150, 200, 250" },
    workers:            { label: "Workers",             defaults: "4, 8, 16, 32, 64" },
    max_attempts:       { label: "Max Attempts",        defaults: "1, 2, 3, 4, 6" },
    attempt_timeout_ms: { label: "Attempt Timeout (ms)", defaults: "100, 150, 200, 300, 500" },
    queue_capacity:     { label: "Queue Capacity",      defaults: "100, 500, 1000, 2000" },
  };

  const loadConfigs = useCallback(async () => {
    try {
      const data = await apiListConfigs();
      setConfigs(data.configs);
      const def = data.configs.find((c) => c.includes("snowflake/motivation")) || data.configs.find((c) => c.includes("motivation-varied")) || data.configs[0];
      if (def) {
        if (!sensConfigPath) setSensConfigPath(def);
        if (!dashConfigPath) setDashConfigPath(def);
      }
    } catch (e) {
      setError(`Failed to load configs: ${e.message}. Is the server running?`);
    }
  }, [sensConfigPath, dashConfigPath]);

  // --- Tab 1 handlers ---
  const runSweep = useCallback(async () => {
    if (!sensConfigPath) return;
    const strat = STRATEGIES[selectedStrategy];
    const sweep = strat.sweeps[selectedSweep];
    setLoading(true); setError(null);
    setProgressInfo({ text: `Sweeping ${strat.label} / ${sweep.param}`, current: 0, total: sweep.values.length, startTime: Date.now() });
    try {
      const data = await apiSweep(sensConfigPath, sweep.path, sweep.values);
      const result = { strategy: strat, paramLabel: sweep.param, ...data };
      setSweepResult(result);
      await apiSaveResults("sweep", result, `${strat.key}_${sweep.param.replace(/\s+/g, "_")}`);
    } catch (e) { setError(e.message); }
    setLoading(false); setProgressInfo(null);
  }, [sensConfigPath, selectedStrategy, selectedSweep]);

  const runTornado = useCallback(async () => {
    if (!sensConfigPath) return;
    setLoading(true); setError(null);
    const results = [];
    let done = 0;
    const totalSweeps = STRATEGIES.reduce((s, st) => s + st.sweeps.length, 0);
    const totalSims = totalSweeps * 2;
    const startTime = Date.now();
    setProgressInfo({ text: "Starting tornado analysis...", current: 0, total: totalSims, startTime });
    try {
      for (const strat of STRATEGIES) {
        for (const sweep of strat.sweeps) {
          setProgressInfo({ text: `${strat.label} / ${sweep.param}`, current: done * 2, total: totalSims, startTime });
          const vals = sweep.values;
          const data = await apiSweep(sensConfigPath, sweep.path, [vals[0], vals[vals.length - 1]]);
          const r = data.results || [];
          if (r.length >= 2) {
            const getSR = (res) => {
              if (res.per_client) {
                const pc = res.per_client.find((c) => c.name === strat.clientName);
                if (pc) return pc.success_rate;
              }
              return res.success_rate || 0;
            };
            results.push({
              strategy: strat.label, param: sweep.param, color: strat.color,
              delta: getSR(r[r.length - 1]) - getSR(r[0]),
              srMin: getSR(r[0]), srMax: getSR(r[r.length - 1]),
            });
          }
          done++;
        }
      }
      results.sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
      setTornadoData(results);
      await apiSaveResults("tornado", { entries: results, config: sensConfigPath }, "all_strategies");
    } catch (e) { setError(e.message); }
    setLoading(false); setProgressInfo(null);
  }, [sensConfigPath]);

  const runHeatmap = useCallback(async () => {
    if (!sensConfigPath) return;
    const strat = STRATEGIES[hmStrategy];
    if (strat.sweeps.length < 2 || hmParamX === hmParamY) return;
    const sx = strat.sweeps[hmParamX];
    const sy = strat.sweeps[hmParamY];
    const gridSize = sx.values.length * sy.values.length;
    setLoading(true); setError(null);
    setProgressInfo({ text: `Running ${sx.values.length}x${sy.values.length} grid (${gridSize} sims)`, current: 0, total: gridSize, startTime: Date.now() });
    try {
      const data = await apiHeatmap(sensConfigPath, sx.path, sx.values, sy.path, sy.values);
      const result = { strategy: strat, labelX: sx.param, labelY: sy.param,
        valuesX: sx.values, valuesY: sy.values, data: data.results };
      setHeatmapResult(result);
      await apiSaveResults("heatmap", result, `${strat.key}_${sx.param}_vs_${sy.param}`.replace(/\s+/g, "_"));
    } catch (e) { setError(e.message); }
    setLoading(false); setProgressInfo(null);
  }, [sensConfigPath, hmStrategy, hmParamX, hmParamY]);

  // --- Tab 2 handler ---
  const runDashboard = useCallback(async () => {
    if (!dashConfigPath) return;
    setLoading(true); setError(null);
    setProgressInfo({ text: "Running simulation...", current: 0, total: 0, startTime: Date.now() });
    try {
      const data = await apiRun(dashConfigPath);
      setDashResult(data);
      await apiSaveResults("dashboard", data, dashConfigPath.replace(/\//g, "_").replace(".yaml", ""));
    } catch (e) { setError(e.message); }
    setLoading(false); setProgressInfo(null);
  }, [dashConfigPath]);

  // --- Parameter comparison handler ---
  const runComparison = useCallback(async () => {
    if (!dashConfigPath) return;
    const values = compareValues.split(",").map(s => parseFloat(s.trim())).filter(v => !isNaN(v));
    if (values.length === 0) return;
    setCompareLoading(true); setError(null);
    setProgressInfo({ text: `Running ${values.length} simulations for ${PARAM_PRESETS[compareParam]?.label || compareParam}...`, current: 0, total: values.length, startTime: Date.now() });
    try {
      const data = await apiParamCompare(dashConfigPath, compareParam, values);
      setCompareResult(data);
    } catch (e) { setError(e.message); }
    setCompareLoading(false); setProgressInfo(null);
  }, [dashConfigPath, compareParam, compareValues]);

  const selectStyle = {
    background: T.inputBg, border: `1px solid ${T.border}`, color: T.text,
    padding: "7px 12px", borderRadius: 4, fontSize: 13,
    fontFamily: "'JetBrains Mono', monospace", cursor: "pointer", outline: "none",
  };
  const btnStyle = {
    ...selectStyle,
    background: T.btnBg, border: `1px solid ${T.accent}`, color: T.accent,
    fontWeight: 600, textAlign: "center", width: "100%", padding: "11px",
  };

  const tabs = [
    { key: "dashboard", label: "Metrics Dashboard" },
    { key: "sensitivity", label: "Parameter Sensitivity" },
  ];

  const sensSections = [
    { key: "sweep", label: "Sweep" },
    { key: "tornado", label: "Tornado" },
    { key: "heatmap", label: "Heatmap" },
  ];

  // Config selector component (reused for both tabs)
  const ConfigSelector = ({ configPath, setConfigPath, label }) => (
    <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
      <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
        {label || "Config"}
      </h3>
      {!configs ? (
        <button onClick={loadConfigs} style={btnStyle}>Load Configs from Server</button>
      ) : (
        <select value={configPath} onChange={(e) => setConfigPath(e.target.value)}
          style={{ ...selectStyle, width: "100%" }}>
          {configs.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      )}
    </div>
  );

  return (
    <div style={{
      minHeight: "100vh", color: T.text,
      fontFamily: "'JetBrains Mono', 'Fira Code', monospace", padding: "20px",
    }}>
      <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet" />

      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <div style={{ marginBottom: 24, borderBottom: `1px solid ${T.border}`, paddingBottom: 14 }}>
          <h1 style={{ fontSize: 24, fontWeight: 700, color: T.accent, margin: 0 }}>
            Single-Client &amp; Single-Service
          </h1>
          <p style={{ fontSize: 14, color: T.dim, margin: "6px 0 0" }}>
            Compare client-side retry strategies — parameter sensitivity and comprehensive metrics
          </p>
        </div>

        {/* Tabs */}
        <div style={{ display: "flex", gap: 2, marginBottom: 14, flexWrap: "wrap" }}>
          {tabs.map((tab) => (
            <button key={tab.key} onClick={() => setActiveTab(tab.key)} style={{
              padding: "8px 18px",
              background: activeTab === tab.key ? T.btnBg : "transparent",
              border: `1px solid ${activeTab === tab.key ? T.accent : T.border}`,
              borderRadius: 5, fontSize: 13, cursor: "pointer",
              color: activeTab === tab.key ? T.accent : T.dim,
              fontFamily: "'JetBrains Mono', monospace",
              fontWeight: activeTab === tab.key ? 600 : 400,
            }}>
              {tab.label}
            </button>
          ))}
        </div>

        {/* ============== TAB 1: PARAMETER SENSITIVITY ============== */}
        {activeTab === "sensitivity" && (
          <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
            {/* Left: Controls */}
            <div style={{ width: 290, flexShrink: 0 }}>
              <ConfigSelector configPath={sensConfigPath} setConfigPath={setSensConfigPath} label="Base Config" />

              {/* Sub-section toggle */}
              <div style={{ display: "flex", gap: 2, marginBottom: 14 }}>
                {sensSections.map((s) => (
                  <button key={s.key} onClick={() => setSensSection(s.key)} style={{
                    flex: 1, padding: "6px 8px", fontSize: 11,
                    background: sensSection === s.key ? T.btnBg : "transparent",
                    border: `1px solid ${sensSection === s.key ? T.accent : T.border}`,
                    borderRadius: 4, cursor: "pointer",
                    color: sensSection === s.key ? T.accent : T.dim,
                    fontFamily: "'JetBrains Mono', monospace",
                    fontWeight: sensSection === s.key ? 600 : 400,
                  }}>
                    {s.label}
                  </button>
                ))}
              </div>

              {/* Sweep controls */}
              {sensSection === "sweep" && (
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
                  <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                    Sweep Config
                  </h3>
                  <div style={{ marginBottom: 10 }}>
                    <label style={{ fontSize: 12, color: T.muted, display: "block", marginBottom: 4 }}>Strategy</label>
                    <select value={selectedStrategy}
                      onChange={(e) => { setSelectedStrategy(parseInt(e.target.value)); setSelectedSweep(0); }}
                      style={{ ...selectStyle, width: "100%" }}>
                      {STRATEGIES.map((s, i) => <option key={s.key} value={i}>{s.label}</option>)}
                    </select>
                  </div>
                  <div style={{ marginBottom: 10 }}>
                    <label style={{ fontSize: 12, color: T.muted, display: "block", marginBottom: 4 }}>Parameter</label>
                    <select value={selectedSweep} onChange={(e) => setSelectedSweep(parseInt(e.target.value))}
                      style={{ ...selectStyle, width: "100%" }}>
                      {STRATEGIES[selectedStrategy].sweeps.map((sw, i) => (
                        <option key={i} value={i}>{sw.param}</option>
                      ))}
                    </select>
                  </div>
                  <div style={{ fontSize: 12, color: T.faint, marginBottom: 12 }}>
                    Values: [{STRATEGIES[selectedStrategy].sweeps[selectedSweep].values.join(", ")}]
                  </div>
                  <button onClick={runSweep} disabled={loading || !sensConfigPath} style={{
                    ...btnStyle, opacity: loading || !sensConfigPath ? 0.5 : 1,
                    cursor: loading ? "wait" : "pointer",
                  }}>
                    {loading ? "Running sweep..." : "Run Sweep"}
                  </button>
                </div>
              )}

              {/* Tornado controls */}
              {sensSection === "tornado" && (
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
                  <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                    Tornado Analysis
                  </h3>
                  <p style={{ fontSize: 12, color: T.muted, marginBottom: 12, lineHeight: 1.6 }}>
                    For each strategy, sweeps every parameter between its min and max value
                    to measure impact on success rate.
                  </p>
                  <button onClick={runTornado} disabled={loading || !sensConfigPath} style={{
                    ...btnStyle, opacity: loading || !sensConfigPath ? 0.5 : 1,
                    cursor: loading ? "wait" : "pointer",
                  }}>
                    {loading ? "Running..." : `Run Tornado (${STRATEGIES.reduce((s, st) => s + st.sweeps.length, 0)} sweeps)`}
                  </button>
                </div>
              )}

              {/* Heatmap controls */}
              {sensSection === "heatmap" && (
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
                  <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                    2D Interaction
                  </h3>
                  <div style={{ marginBottom: 10 }}>
                    <label style={{ fontSize: 12, color: T.muted, display: "block", marginBottom: 4 }}>Strategy</label>
                    <select value={hmStrategy} onChange={(e) => { setHmStrategy(parseInt(e.target.value)); setHmParamX(0); setHmParamY(1); }}
                      style={{ ...selectStyle, width: "100%" }}>
                      {STRATEGIES.filter((s) => s.sweeps.length >= 2).map((s) => (
                        <option key={s.key} value={STRATEGIES.indexOf(s)}>{s.label}</option>
                      ))}
                    </select>
                  </div>
                  <div style={{ marginBottom: 10 }}>
                    <label style={{ fontSize: 12, color: T.muted, display: "block", marginBottom: 4 }}>X-axis Parameter</label>
                    <select value={hmParamX} onChange={(e) => setHmParamX(parseInt(e.target.value))}
                      style={{ ...selectStyle, width: "100%" }}>
                      {STRATEGIES[hmStrategy].sweeps.map((sw, i) => (
                        <option key={i} value={i}>{sw.param}</option>
                      ))}
                    </select>
                  </div>
                  <div style={{ marginBottom: 10 }}>
                    <label style={{ fontSize: 12, color: T.muted, display: "block", marginBottom: 4 }}>Y-axis Parameter</label>
                    <select value={hmParamY} onChange={(e) => setHmParamY(parseInt(e.target.value))}
                      style={{ ...selectStyle, width: "100%" }}>
                      {STRATEGIES[hmStrategy].sweeps.map((sw, i) => (
                        <option key={i} value={i} disabled={i === hmParamX}>{sw.param}</option>
                      ))}
                    </select>
                  </div>
                  {(() => {
                    const sx = STRATEGIES[hmStrategy].sweeps[hmParamX];
                    const sy = STRATEGIES[hmStrategy].sweeps[hmParamY];
                    const gridSize = (sx?.values.length || 0) * (sy?.values.length || 0);
                    return (
                      <button onClick={runHeatmap} disabled={loading || !sensConfigPath || hmParamX === hmParamY}
                        style={{
                          ...btnStyle, opacity: loading || !sensConfigPath || hmParamX === hmParamY ? 0.5 : 1,
                          cursor: loading ? "wait" : "pointer",
                        }}>
                        {loading ? "Running..." : `Run Heatmap (${gridSize} sims)`}
                      </button>
                    );
                  })()}
                </div>
              )}

              {/* Progress / Error / Save */}
              {progressInfo && !error && <ProgressBar info={progressInfo} T={T} />}
              {error && (
                <div style={{
                  background: T.errorBg, border: `1px solid ${T.errorBorder}`,
                  borderRadius: 6, padding: 12, marginBottom: 14, fontSize: 12, color: T.errorText,
                }}>
                  {error}
                </div>
              )}
            </div>

            {/* Right: Visualizations */}
            <div style={{ flex: 1, minWidth: 0 }}>
              {/* Sweep */}
              {sensSection === "sweep" && (
                <>
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20, minHeight: 360 }}>
                    <h3 style={{ fontSize: 16, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>
                      Parameter Sweep — {STRATEGIES[selectedStrategy].label}
                    </h3>
                    <p style={{ fontSize: 13, color: T.dim, margin: "0 0 16px" }}>
                      {STRATEGIES[selectedStrategy].sweeps[selectedSweep].param} — showing {displayName(STRATEGIES[selectedStrategy].clientName)} success rate
                    </p>
                    {sweepResult ? (
                      <div style={{ overflowX: "auto" }}>
                        <SweepChart
                          sweepResults={sweepResult.results}
                          targetClient={sweepResult.strategy.clientName}
                          strategyColor={sweepResult.strategy.color}
                          T={T} width={640} height={320}
                        />
                      </div>
                    ) : (
                      <div style={{ textAlign: "center", padding: 60, color: T.faint, fontSize: 14 }}>
                        Select a strategy and parameter, then click "Run Sweep"
                      </div>
                    )}
                  </div>

                  {sweepResult && (
                    <div style={{ marginTop: 12, background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, overflowX: "auto" }}>
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                        <thead>
                          <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                            <th style={{ textAlign: "left", padding: "6px 10px", color: T.muted }}>Value</th>
                            <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Client SR</th>
                            <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Goodput</th>
                            <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Amplification</th>
                            <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Retry Eff.</th>
                            <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>P95 (ms)</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sweepResult.results.map((r, i) => {
                            let clientSR = r.success_rate;
                            let gp = null, re = null, p95 = null;
                            if (r.per_client && sweepResult.strategy) {
                              const pc = r.per_client.find((c) => c.name === sweepResult.strategy.clientName);
                              if (pc) {
                                clientSR = pc.success_rate;
                                gp = pc.goodput_rps;
                                re = pc.retry_efficiency;
                                p95 = pc.p95;
                              }
                            }
                            return (
                              <tr key={i} style={{ borderBottom: `1px solid ${T.rowBorder}` }}>
                                <td style={{ padding: "6px 10px", color: T.accent }}>{fmtVal(r.param_value)}</td>
                                <td style={{ padding: "6px 10px", textAlign: "right",
                                  color: clientSR > 0.8 ? "#388e3c" : clientSR > 0.5 ? "#e07b39" : "#c75050" }}>
                                  {r.error ? "ERR" : `${(clientSR * 100).toFixed(1)}%`}
                                </td>
                                <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                                  {gp != null ? gp.toFixed(1) : "—"}
                                </td>
                                <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                                  {r.amplification ? `${r.amplification.toFixed(2)}x` : "—"}
                                </td>
                                <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                                  {re != null ? `${(re * 100).toFixed(1)}%` : "—"}
                                </td>
                                <td style={{ padding: "6px 10px", textAlign: "right", color: T.faint }}>
                                  {p95 != null ? p95.toFixed(0) : "—"}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              )}

              {/* Tornado */}
              {sensSection === "tornado" && (
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20, minHeight: 360 }}>
                  <h3 style={{ fontSize: 16, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>
                    Parameter Impact Ranking
                  </h3>
                  <p style={{ fontSize: 13, color: T.dim, margin: "0 0 16px" }}>
                    Which parameters cause the largest swing in success rate across all strategies
                  </p>
                  {tornadoData ? (
                    <div style={{ overflowX: "auto" }}>
                      <TornadoChart tornadoData={tornadoData} T={T} width={700} />
                    </div>
                  ) : (
                    <div style={{ textAlign: "center", padding: 60, color: T.faint, fontSize: 14 }}>
                      <p>Click "Run Tornado" to sweep all parameters across all strategies</p>
                      <p style={{ fontSize: 12, marginTop: 10 }}>
                        Each bar shows how much a parameter changes the target strategy's success rate
                        when swept from its minimum to maximum value.
                      </p>
                    </div>
                  )}
                </div>
              )}

              {/* Heatmap */}
              {sensSection === "heatmap" && (
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20, minHeight: 360 }}>
                  <h3 style={{ fontSize: 16, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>
                    2D Parameter Interaction — {STRATEGIES[hmStrategy].label}
                  </h3>
                  <p style={{ fontSize: 13, color: T.dim, margin: "0 0 16px" }}>
                    How two parameters interact to determine success rate
                  </p>
                  {heatmapResult ? (
                    <div style={{ overflowX: "auto" }}>
                      <HeatmapChart
                        heatmapData={heatmapResult.data}
                        valuesX={heatmapResult.valuesX}
                        valuesY={heatmapResult.valuesY}
                        labelX={heatmapResult.labelX}
                        labelY={heatmapResult.labelY}
                        targetClient={heatmapResult.strategy.clientName}
                        T={T} width={520} height={420}
                      />
                    </div>
                  ) : (
                    <div style={{ textAlign: "center", padding: 60, color: T.faint, fontSize: 14 }}>
                      <p>Select a strategy and two parameters, then click "Run Heatmap"</p>
                      <p style={{ fontSize: 12, marginTop: 10 }}>
                        Each cell shows the success rate for that combination of parameter values.
                        Warning: this runs N×M simulations and may take a few minutes.
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        )}

        {/* ============== TAB 2: METRICS DASHBOARD ============== */}
        {activeTab === "dashboard" && (
          <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
            {/* Left: Controls */}
            <div style={{ width: 290, flexShrink: 0 }}>
              <ConfigSelector configPath={dashConfigPath} setConfigPath={setDashConfigPath} label="Dashboard Config" />

              <button onClick={runDashboard} disabled={loading || !dashConfigPath} style={{
                ...btnStyle, opacity: loading || !dashConfigPath ? 0.5 : 1,
                cursor: loading ? "wait" : "pointer", marginBottom: 14,
              }}>
                {loading ? "Running simulation..." : "Run & Plot Metrics"}
              </button>

              {/* Progress / Error / Save */}
              {progressInfo && !error && <ProgressBar info={progressInfo} T={T} />}
              {error && (
                <div style={{
                  background: T.errorBg, border: `1px solid ${T.errorBorder}`,
                  borderRadius: 6, padding: 12, marginBottom: 14, fontSize: 12, color: T.errorText,
                }}>
                  {error}
                </div>
              )}

              {/* Stat cards (if data available) */}
              {dashResult && (
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {dashResult.clients.map((c, i) => {
                    const s = c.summary;
                    return (
                      <div key={i} style={{
                        background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8,
                        padding: "10px 14px",
                      }}>
                        <div style={{ fontSize: 12, fontWeight: 600, color: clientColor(c.name, i), marginBottom: 6 }}>
                          {displayName(c.name)}
                        </div>
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 14px", fontSize: 11 }}>
                          <span style={{ color: T.muted }}>Recovery</span>
                          <span style={{ color: T.text, textAlign: "right" }}>
                            {s.recovery_time_s != null ? `${s.recovery_time_s.toFixed(1)}s` : "—"}
                          </span>
                          <span style={{ color: T.muted }}>Goodput</span>
                          <span style={{ color: T.text, textAlign: "right" }}>{s.goodput_rps != null ? s.goodput_rps.toFixed(1) : "—"} rps</span>
                          <span style={{ color: T.muted }}>Amplif.</span>
                          <span style={{ color: T.text, textAlign: "right" }}>{s.amplification_factor != null ? `${s.amplification_factor.toFixed(2)}x` : "—"}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Right: Dashboard charts */}
            <div style={{ flex: 1, minWidth: 0 }}>
              {dashResult ? (
                <>
                  <ChartLegend clients={dashResult.clients} T={T} />

                  {/* 1. Success Rate */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 18px", marginBottom: 12 }}>
                    <h3 style={{ fontSize: 14, color: T.text, margin: "0 0 8px", fontWeight: 600 }}>Success Rate</h3>
                    <MetricTimeSeriesChart
                      clients={dashResult.clients} faultEvents={dashResult.fault_events}
                      metricKey="_success_rate" yLabel="Success Rate" yDomain={[0, 1.05]}
                      yFormat={(v) => `${(v * 100).toFixed(0)}%`}
                      T={T} width={700} height={240}
                    />
                  </div>

                  {/* 2. Goodput */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 18px", marginBottom: 12 }}>
                    <h3 style={{ fontSize: 14, color: T.text, margin: "0 0 8px", fontWeight: 600 }}>Goodput (RPS)</h3>
                    <MetricTimeSeriesChart
                      clients={dashResult.clients} faultEvents={dashResult.fault_events}
                      metricKey="goodput_rps" yLabel="Goodput (rps)"
                      yFormat={(v) => v.toFixed(0)}
                      T={T} width={700} height={240}
                    />
                  </div>

                  {/* 3. Amplification Factor */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 18px", marginBottom: 12 }}>
                    <h3 style={{ fontSize: 14, color: T.text, margin: "0 0 8px", fontWeight: 600 }}>Amplification Factor</h3>
                    <MetricTimeSeriesChart
                      clients={dashResult.clients} faultEvents={dashResult.fault_events}
                      metricKey="amplification_factor" yLabel="Amplification"
                      yFormat={(v) => `${v.toFixed(1)}x`}
                      T={T} width={700} height={240}
                    />
                  </div>

                  {/* 4. Retry Efficiency */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 18px", marginBottom: 12 }}>
                    <h3 style={{ fontSize: 14, color: T.text, margin: "0 0 8px", fontWeight: 600 }}>Retry Efficiency</h3>
                    <MetricTimeSeriesChart
                      clients={dashResult.clients} faultEvents={dashResult.fault_events}
                      metricKey="retry_efficiency" yLabel="Retry Efficiency" yDomain={[0, 1.05]}
                      yFormat={(v) => `${(v * 100).toFixed(0)}%`}
                      T={T} width={700} height={240}
                    />
                  </div>

                  {/* 5. Load Decomposition */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 18px", marginBottom: 12 }}>
                    <h3 style={{ fontSize: 14, color: T.text, margin: "0 0 8px", fontWeight: 600 }}>Load Decomposition</h3>
                    <LoadDecompositionChart
                      clients={dashResult.clients} faultEvents={dashResult.fault_events}
                      T={T} width={700} height={280}
                    />
                  </div>

                  {/* Summary Table */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, overflowX: "auto" }}>
                    <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase" }}>
                      Strategy Summary
                    </h3>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                      <thead>
                        <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                          <th style={{ textAlign: "left", padding: "6px 8px", color: T.muted }}>Strategy</th>
                          <th style={{ textAlign: "right", padding: "6px 8px", color: T.muted }}>Success</th>
                          <th style={{ textAlign: "right", padding: "6px 8px", color: T.muted }}>Goodput</th>
                          <th style={{ textAlign: "right", padding: "6px 8px", color: T.muted }}>Amplif.</th>
                          <th style={{ textAlign: "right", padding: "6px 8px", color: T.muted }}>Retry Eff.</th>
                          <th style={{ textAlign: "right", padding: "6px 8px", color: T.muted }}>Recovery</th>
                          <th style={{ textAlign: "right", padding: "6px 8px", color: T.muted }}>P50</th>
                          <th style={{ textAlign: "right", padding: "6px 8px", color: T.muted }}>P95</th>
                          <th style={{ textAlign: "right", padding: "6px 8px", color: T.muted }}>P99</th>
                        </tr>
                      </thead>
                      <tbody>
                        {dashResult.clients.map((c, i) => {
                          const s = c.summary;
                          const color = clientColor(c.name, i);
                          return (
                            <tr key={i} style={{ borderBottom: `1px solid ${T.rowBorder}` }}>
                              <td style={{ padding: "6px 8px", color, fontWeight: 600 }}>{displayName(c.name)}</td>
                              <td style={{ padding: "6px 8px", textAlign: "right",
                                color: s.success_rate > 0.8 ? "#388e3c" : s.success_rate > 0.5 ? "#e07b39" : "#c75050" }}>
                                {(s.success_rate * 100).toFixed(1)}%
                              </td>
                              <td style={{ padding: "6px 8px", textAlign: "right", color: T.text }}>
                                {s.goodput_rps != null ? s.goodput_rps.toFixed(1) : "—"}
                              </td>
                              <td style={{ padding: "6px 8px", textAlign: "right", color: T.text }}>
                                {s.amplification_factor != null ? `${s.amplification_factor.toFixed(2)}x` : "—"}
                              </td>
                              <td style={{ padding: "6px 8px", textAlign: "right", color: T.text }}>
                                {s.retry_efficiency != null ? `${(s.retry_efficiency * 100).toFixed(1)}%` : "—"}
                              </td>
                              <td style={{ padding: "6px 8px", textAlign: "right", color: T.text }}>
                                {s.recovery_time_s != null ? `${s.recovery_time_s.toFixed(1)}s` : "—"}
                              </td>
                              <td style={{ padding: "6px 8px", textAlign: "right", color: T.faint }}>
                                {s.p50 != null ? s.p50.toFixed(0) : "—"}
                              </td>
                              <td style={{ padding: "6px 8px", textAlign: "right", color: T.faint }}>
                                {s.p95 != null ? s.p95.toFixed(0) : "—"}
                              </td>
                              <td style={{ padding: "6px 8px", textAlign: "right", color: T.faint }}>
                                {s.p99 != null ? s.p99.toFixed(0) : "—"}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>

                  {/* ====== Parameter Comparison Section ====== */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginTop: 12 }}>
                    <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 12px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                      Parameter Comparison
                    </h3>
                    <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 12 }}>
                      <div>
                        <label style={{ fontSize: 11, color: T.muted, display: "block", marginBottom: 4 }}>Parameter</label>
                        <select value={compareParam} onChange={(e) => {
                          setCompareParam(e.target.value);
                          const preset = PARAM_PRESETS[e.target.value];
                          if (preset) setCompareValues(preset.defaults);
                        }} style={{ ...selectStyle, width: 180 }}>
                          {Object.entries(PARAM_PRESETS).map(([k, v]) => (
                            <option key={k} value={k}>{v.label}</option>
                          ))}
                        </select>
                      </div>
                      <div style={{ flex: 1, minWidth: 200 }}>
                        <label style={{ fontSize: 11, color: T.muted, display: "block", marginBottom: 4 }}>Values (comma-separated)</label>
                        <input type="text" value={compareValues} onChange={(e) => setCompareValues(e.target.value)}
                          style={{ ...selectStyle, width: "100%" }} />
                      </div>
                      <button onClick={runComparison} disabled={compareLoading || !dashConfigPath}
                        style={{ ...selectStyle, background: T.btnBg, border: `1px solid ${T.accent}`, color: T.accent,
                          fontWeight: 600, padding: "7px 18px", opacity: compareLoading ? 0.5 : 1, cursor: compareLoading ? "wait" : "pointer" }}>
                        {compareLoading ? "Running..." : "Run Comparison"}
                      </button>
                    </div>

                    {compareResult && compareResult.results && compareResult.results.length > 0 && (
                      <>
                        {/* Legend — reuse strategy colors */}
                        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 16px", marginBottom: 10 }}>
                          {compareResult.results[0].per_client.map((c, i) => {
                            const style = getStyle(c.name, i);
                            return (
                              <div key={i} style={{ display: "flex", alignItems: "center", gap: 5 }}>
                                <div style={{ width: 12, height: 12, borderRadius: 2, background: style.color }} />
                                <span style={{ fontSize: 11, color: T.text }}>{displayName(c.name)}</span>
                              </div>
                            );
                          })}
                        </div>

                        {/* Success Rate */}
                        <div style={{ marginBottom: 10 }}>
                          <h4 style={{ fontSize: 12, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>Success Rate</h4>
                          <GroupedBarChart compareResult={compareResult} metricKey="success_rate"
                            yLabel="Success Rate" yFormat={(v) => `${(v * 100).toFixed(0)}%`} T={T} />
                        </div>

                        {/* Goodput */}
                        <div style={{ marginBottom: 10 }}>
                          <h4 style={{ fontSize: 12, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>Goodput (RPS)</h4>
                          <GroupedBarChart compareResult={compareResult} metricKey="goodput_rps"
                            yLabel="Goodput (rps)" yFormat={(v) => v.toFixed(0)} T={T} />
                        </div>

                        {/* Amplification Factor */}
                        <div style={{ marginBottom: 10 }}>
                          <h4 style={{ fontSize: 12, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>Amplification Factor</h4>
                          <GroupedBarChart compareResult={compareResult} metricKey="amplification_factor"
                            yLabel="Amplification" yFormat={(v) => `${v.toFixed(1)}x`} T={T} />
                        </div>

                        {/* Retry Efficiency */}
                        <div style={{ marginBottom: 10 }}>
                          <h4 style={{ fontSize: 12, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>Retry Efficiency</h4>
                          <GroupedBarChart compareResult={compareResult} metricKey="retry_efficiency"
                            yLabel="Retry Efficiency" yFormat={(v) => `${(v * 100).toFixed(0)}%`} T={T} />
                        </div>

                        {/* P99 Latency */}
                        <div style={{ marginBottom: 4 }}>
                          <h4 style={{ fontSize: 12, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>P99 Latency (ms)</h4>
                          <GroupedBarChart compareResult={compareResult} metricKey="p99"
                            yLabel="P99 (ms)" yFormat={(v) => v.toFixed(0)} T={T} />
                        </div>
                      </>
                    )}
                  </div>
                </>
              ) : (
                <div style={{
                  background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8,
                  padding: 20, minHeight: 400, textAlign: "center",
                }}>
                  <div style={{ padding: 70, color: T.faint, fontSize: 14 }}>
                    <p>Select a config and click "Run & Plot Metrics" to see the full dashboard</p>
                    <p style={{ fontSize: 12, marginTop: 10 }}>
                      Shows time-series charts and strategy summary. After running, use Parameter Comparison
                      to sweep a shared parameter and see grouped bar charts across strategies.
                    </p>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Footer */}
        <div style={{
          marginTop: 12, padding: "12px 16px",
          background: T.hintBg, border: `1px solid ${T.border}`, borderRadius: 6,
          fontSize: 12, color: T.dim, lineHeight: 1.7,
        }}>
          <strong style={{ color: T.muted }}>How it works:</strong> This visualization calls the real Python simulator
          via the API server. Each data point is a full simulation run. Start server: <code style={{ color: T.accent }}>cd simulator && python3 bin/viz_server.py</code>
        </div>
      </div>
    </div>
  );
}
