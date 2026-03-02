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

const STRATEGY_COLORS = {
  no_retries: "#8b8b8b",
  three_retries: "#4a86c8",
  exponential_backoff_jitter: "#e07b39",
  circuit_breaker: "#5ba05b",
  retry_budget: "#c75050",
};

const STRATEGIES = [
  {
    key: "three_retries",
    label: "Fixed Retries",
    color: "#4a86c8",
    clientName: "three_retries",
    sweeps: [
      { param: "Max Attempts", path: "clients.1.retry.max_attempts", values: [1, 2, 3, 4, 5, 6, 8] },
      { param: "Retry Delay (ms)", path: "clients.1.retry.delay_ms", values: [0, 50, 100, 200, 500, 1000] },
    ],
  },
  {
    key: "exponential_backoff_jitter",
    label: "Exp Backoff + Jitter",
    color: "#e07b39",
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
    color: "#5ba05b",
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
    color: "#c75050",
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
  // Professional diverging: muted red → pale → muted teal
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

// ============================================================
// Time Series Chart — matplotlib-style
// ============================================================

function TimeSeriesChart({ clients, faultEvents, T, width = 700, height = 380 }) {
  if (!clients || clients.length === 0) return null;
  const padL = 65, padR = 20, padT = 20, padB = 50;
  const cW = width - padL - padR;
  const cH = height - padT - padB;

  let maxT = 0;
  for (const c of clients) {
    const ts = c.timeseries || [];
    if (ts.length > 0 && ts[ts.length - 1].timepoint > maxT) maxT = ts[ts.length - 1].timepoint;
  }
  if (maxT === 0) return null;

  const xScale = (t) => padL + (t / maxT) * cW;
  const yScale = (v) => padT + (1 - v) * cH;

  const xTicks = [];
  for (let t = 0; t <= maxT; t += 10) xTicks.push(t);

  const defaultColors = ["#8b8b8b", "#4a86c8", "#e07b39", "#5ba05b", "#c75050", "#7a6cb2", "#c4853e"];

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {[0, 0.25, 0.5, 0.75, 1].map((v) => (
        <g key={v}>
          <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
          <text x={padL - 10} y={yScale(v) + 4} textAnchor="end" fill={T.svgLabel} fontSize={12}
            fontFamily="'JetBrains Mono', monospace">{(v * 100).toFixed(0)}%</text>
        </g>
      ))}

      {(faultEvents || []).map((fe, i) => (
        <g key={`f-${i}`}>
          <rect x={xScale(fe.start_time_s)} y={padT}
            width={Math.max(0, xScale(fe.end_time_s) - xScale(fe.start_time_s))} height={cH}
            fill="#e59a9a" opacity={0.12} rx={2} />
          <text x={(xScale(fe.start_time_s) + xScale(fe.end_time_s)) / 2} y={padT + 18}
            textAnchor="middle" fill="#e59a9a" fontSize={12} fontWeight={600}
            fontFamily="'JetBrains Mono', monospace">
            {fe.parameters?.p_fail
              ? `Partial Failure (${(fe.parameters.p_fail * 100).toFixed(0)}%)`
              : "Fault Injection"}
          </text>
        </g>
      ))}

      {clients.map((c, ci) => {
        const ts = c.timeseries || [];
        if (ts.length < 2) return null;
        const rawSR = ts.map((d) => d.root_requests > 0 ? d.success_root / d.root_requests : NaN);
        const sr = smoothArray(rawSR, 3);
        const color = STRATEGY_COLORS[c.name] || defaultColors[ci % defaultColors.length];

        const segments = [];
        let seg = [];
        for (let i = 0; i < sr.length; i++) {
          if (!isNaN(sr[i])) {
            seg.push(`${xScale(ts[i].timepoint).toFixed(1)},${yScale(Math.max(0, Math.min(1, sr[i]))).toFixed(1)}`);
          } else if (seg.length > 0) {
            segments.push(seg);
            seg = [];
          }
        }
        if (seg.length > 0) segments.push(seg);

        return segments.map((pts, si) => (
          <polyline key={`${ci}-${si}`} points={pts.join(" ")} fill="none"
            stroke={color} strokeWidth={2.5} opacity={0.85} />
        ));
      })}

      {xTicks.map((t) => (
        <text key={t} x={xScale(t)} y={height - 14} textAnchor="middle" fill={T.svgLabel} fontSize={11}
          fontFamily="'JetBrains Mono', monospace">{t}</text>
      ))}

      <text x={padL + cW / 2} y={height - 1} textAnchor="middle" fill={T.svgAxis} fontSize={13}
        fontFamily="'JetBrains Mono', monospace" fontWeight={500}>Time (s)</text>
      <text x={14} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={13}
        fontFamily="'JetBrains Mono', monospace" fontWeight={500}
        transform={`rotate(-90, 14, ${padT + cH / 2})`}>Success Rate (%)</text>

      <g transform={`translate(${padL + cW - 210}, ${padT + 34})`}>
        <rect x={-8} y={-12} width={218} height={clients.length * 20 + 8}
          fill={T.panel} stroke={T.border} strokeWidth={0.5} rx={4} opacity={0.85} />
        {clients.map((c, i) => {
          const color = STRATEGY_COLORS[c.name] || defaultColors[i % defaultColors.length];
          return (
            <g key={i} transform={`translate(0, ${i * 20})`}>
              <line x1={0} y1={0} x2={20} y2={0} stroke={color} strokeWidth={2.5} />
              <text x={26} y={4} fill={T.text} fontSize={11}
                fontFamily="'JetBrains Mono', monospace">{c.name}</text>
            </g>
          );
        })}
      </g>
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
  const [configPath, setConfigPath] = useState("");
  const [activeTab, setActiveTab] = useState("timeseries");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [progressInfo, setProgressInfo] = useState(null);
  const [savedPath, setSavedPath] = useState(null);

  // Tab 1: Time Series
  const [tsResult, setTsResult] = useState(null);

  // Tab 2: Sweep
  const [selectedStrategy, setSelectedStrategy] = useState(0);
  const [selectedSweep, setSelectedSweep] = useState(0);
  const [sweepResult, setSweepResult] = useState(null);

  // Tab 3: Tornado
  const [tornadoData, setTornadoData] = useState(null);

  // Tab 4: Heatmap
  const [hmStrategy, setHmStrategy] = useState(0);
  const [hmParamX, setHmParamX] = useState(0);
  const [hmParamY, setHmParamY] = useState(1);
  const [heatmapResult, setHeatmapResult] = useState(null);

  const loadConfigs = useCallback(async () => {
    try {
      const data = await apiListConfigs();
      setConfigs(data.configs);
      const def = data.configs.find((c) => c.includes("motivation-varied")) || data.configs[0];
      if (def && !configPath) setConfigPath(def);
    } catch (e) {
      setError(`Failed to load configs: ${e.message}. Is the server running?`);
    }
  }, [configPath]);

  const runTimeSeries = useCallback(async () => {
    if (!configPath) return;
    setLoading(true); setError(null); setSavedPath(null);
    setProgressInfo({ text: "Running simulation...", current: 0, total: 0, startTime: Date.now() });
    try {
      const data = await apiRun(configPath);
      setTsResult(data);
      const saveRes = await apiSaveResults("timeseries", data, configPath.replace(/\//g, "_").replace(".yaml", ""));
      setSavedPath(saveRes.path);
    } catch (e) { setError(e.message); }
    setLoading(false); setProgressInfo(null);
  }, [configPath]);

  const runSweep = useCallback(async () => {
    if (!configPath) return;
    const strat = STRATEGIES[selectedStrategy];
    const sweep = strat.sweeps[selectedSweep];
    setLoading(true); setError(null); setSavedPath(null);
    setProgressInfo({ text: `Sweeping ${strat.label} / ${sweep.param}`, current: 0, total: sweep.values.length, startTime: Date.now() });
    try {
      const data = await apiSweep(configPath, sweep.path, sweep.values);
      const result = { strategy: strat, paramLabel: sweep.param, ...data };
      setSweepResult(result);
      const saveRes = await apiSaveResults("sweep", result, `${strat.key}_${sweep.param.replace(/\s+/g, "_")}`);
      setSavedPath(saveRes.path);
    } catch (e) { setError(e.message); }
    setLoading(false); setProgressInfo(null);
  }, [configPath, selectedStrategy, selectedSweep]);

  const runTornado = useCallback(async () => {
    if (!configPath) return;
    setLoading(true); setError(null); setSavedPath(null);
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
          const data = await apiSweep(configPath, sweep.path, [vals[0], vals[vals.length - 1]]);
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
      const saveRes = await apiSaveResults("tornado", { entries: results, config: configPath }, "all_strategies");
      setSavedPath(saveRes.path);
    } catch (e) { setError(e.message); }
    setLoading(false); setProgressInfo(null);
  }, [configPath]);

  const runHeatmap = useCallback(async () => {
    if (!configPath) return;
    const strat = STRATEGIES[hmStrategy];
    if (strat.sweeps.length < 2 || hmParamX === hmParamY) return;
    const sx = strat.sweeps[hmParamX];
    const sy = strat.sweeps[hmParamY];
    const gridSize = sx.values.length * sy.values.length;
    setLoading(true); setError(null); setSavedPath(null);
    setProgressInfo({ text: `Running ${sx.values.length}x${sy.values.length} grid (${gridSize} sims)`, current: 0, total: gridSize, startTime: Date.now() });
    try {
      const data = await apiHeatmap(configPath, sx.path, sx.values, sy.path, sy.values);
      const result = { strategy: strat, labelX: sx.param, labelY: sy.param,
        valuesX: sx.values, valuesY: sy.values, data: data.results };
      setHeatmapResult(result);
      const saveRes = await apiSaveResults("heatmap", result, `${strat.key}_${sx.param}_vs_${sy.param}`.replace(/\s+/g, "_"));
      setSavedPath(saveRes.path);
    } catch (e) { setError(e.message); }
    setLoading(false); setProgressInfo(null);
  }, [configPath, hmStrategy, hmParamX, hmParamY]);

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
    { key: "timeseries", label: "Time Series" },
    { key: "sweep", label: "Parameter Sweep" },
    { key: "tornado", label: "Tornado Chart" },
    { key: "heatmap", label: "Interaction Heatmap" },
  ];

  return (
    <div style={{
      minHeight: "100vh", color: T.text,
      fontFamily: "'JetBrains Mono', 'Fira Code', monospace", padding: "20px",
    }}>
      <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet" />

      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <div style={{ marginBottom: 24, borderBottom: `1px solid ${T.border}`, paddingBottom: 14 }}>
          <h1 style={{ fontSize: 24, fontWeight: 700, color: T.accent, margin: 0 }}>
            Retry Control Sensitivity Analysis
          </h1>
          <p style={{ fontSize: 14, color: T.dim, margin: "6px 0 0" }}>
            Every retry mechanism has a cliff — explore parameter sensitivity for all strategies
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

        <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
          {/* ===== Left: Controls ===== */}
          <div style={{ width: 290, flexShrink: 0 }}>
            {/* Config selector */}
            <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
              <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Base Config
              </h3>
              {!configs ? (
                <button onClick={loadConfigs} style={btnStyle}>Load Configs from Server</button>
              ) : (
                <select value={configPath} onChange={(e) => setConfigPath(e.target.value)}
                  style={{ ...selectStyle, width: "100%" }}>
                  {configs.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              )}
              <p style={{ fontSize: 11, color: T.faint, margin: "8px 0 0" }}>
                Default: motivation-varied.yaml (isolated services per strategy)
              </p>
            </div>

            {/* Tab-specific controls */}
            {activeTab === "timeseries" && (
              <button onClick={runTimeSeries} disabled={loading || !configPath} style={{
                ...btnStyle, opacity: loading || !configPath ? 0.5 : 1,
                cursor: loading ? "wait" : "pointer", marginBottom: 14,
              }}>
                {loading ? "Running simulation..." : "Run & Plot Time Series"}
              </button>
            )}

            {activeTab === "sweep" && (
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
                <button onClick={runSweep} disabled={loading || !configPath} style={{
                  ...btnStyle, opacity: loading || !configPath ? 0.5 : 1,
                  cursor: loading ? "wait" : "pointer",
                }}>
                  {loading ? "Running sweep..." : "Run Sweep"}
                </button>
              </div>
            )}

            {activeTab === "tornado" && (
              <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
                <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  Tornado Analysis
                </h3>
                <p style={{ fontSize: 12, color: T.muted, marginBottom: 12, lineHeight: 1.6 }}>
                  For each strategy, sweeps every parameter between its min and max value
                  to measure impact on success rate. Shows which parameters matter most.
                </p>
                <button onClick={runTornado} disabled={loading || !configPath} style={{
                  ...btnStyle, opacity: loading || !configPath ? 0.5 : 1,
                  cursor: loading ? "wait" : "pointer",
                }}>
                  {loading ? "Running..." : `Run Tornado (${STRATEGIES.reduce((s, st) => s + st.sweeps.length, 0)} sweeps)`}
                </button>
              </div>
            )}

            {activeTab === "heatmap" && (
              <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
                <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  2D Interaction
                </h3>
                <div style={{ marginBottom: 10 }}>
                  <label style={{ fontSize: 12, color: T.muted, display: "block", marginBottom: 4 }}>Strategy</label>
                  <select value={hmStrategy} onChange={(e) => { setHmStrategy(parseInt(e.target.value)); setHmParamX(0); setHmParamY(1); }}
                    style={{ ...selectStyle, width: "100%" }}>
                    {STRATEGIES.filter((s) => s.sweeps.length >= 2).map((s, i) => (
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
                    <button onClick={runHeatmap} disabled={loading || !configPath || hmParamX === hmParamY}
                      style={{
                        ...btnStyle, opacity: loading || !configPath || hmParamX === hmParamY ? 0.5 : 1,
                        cursor: loading ? "wait" : "pointer",
                      }}>
                      {loading ? "Running..." : `Run Heatmap (${gridSize} sims)`}
                    </button>
                  );
                })()}
              </div>
            )}

            {/* Progress / Error */}
            {progressInfo && !error && (
              <ProgressBar info={progressInfo} T={T} />
            )}
            {error && (
              <div style={{
                background: T.errorBg, border: `1px solid ${T.errorBorder}`,
                borderRadius: 6, padding: 12, marginBottom: 14, fontSize: 12, color: T.errorText,
              }}>
                {error}
              </div>
            )}
            {savedPath && !loading && (
              <div style={{
                background: T.hintBg, border: `1px solid ${T.border}`,
                borderRadius: 6, padding: "8px 12px", marginBottom: 14, fontSize: 11, color: T.muted,
              }}>
                Saved to: <span style={{ color: T.accent }}>{savedPath}</span>
              </div>
            )}
          </div>

          {/* ===== Right: Visualization ===== */}
          <div style={{ flex: 1, minWidth: 0 }}>

            {/* Tab 1: Time Series */}
            {activeTab === "timeseries" && (
              <>
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20, minHeight: 400 }}>
                  <h3 style={{ fontSize: 16, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>
                    Success Rate Over Time
                  </h3>
                  <p style={{ fontSize: 13, color: T.dim, margin: "0 0 16px" }}>
                    Per-strategy success rate with fault injection window highlighted
                  </p>
                  {tsResult ? (
                    <div style={{ overflowX: "auto" }}>
                      <TimeSeriesChart
                        clients={tsResult.clients}
                        faultEvents={tsResult.fault_events}
                        T={T} width={700} height={380}
                      />
                    </div>
                  ) : (
                    <div style={{ textAlign: "center", padding: 70, color: T.faint, fontSize: 14 }}>
                      <p>Select a config with multiple retry strategies and click "Run & Plot"</p>
                      <p style={{ fontSize: 12, marginTop: 10 }}>
                        Default: motivation-varied.yaml — each strategy runs on an isolated service,
                        showing pure strategy behavior during a partial failure window.
                      </p>
                    </div>
                  )}
                </div>

                {tsResult && (
                  <div style={{ marginTop: 12, background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, overflowX: "auto" }}>
                    <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase" }}>
                      Strategy Summary
                    </h3>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                      <thead>
                        <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                          <th style={{ textAlign: "left", padding: "6px 10px", color: T.muted }}>Strategy</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Success Rate</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Retries/Root</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>P99 (ms)</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Queue Drops</th>
                        </tr>
                      </thead>
                      <tbody>
                        {tsResult.clients.map((c, i) => {
                          const color = STRATEGY_COLORS[c.name] || "#4a86c8";
                          return (
                            <tr key={i} style={{ borderBottom: `1px solid ${T.rowBorder}` }}>
                              <td style={{ padding: "6px 10px", color, fontWeight: 600 }}>{c.name}</td>
                              <td style={{ padding: "6px 10px", textAlign: "right",
                                color: c.summary.success_rate > 0.8 ? "#388e3c" : c.summary.success_rate > 0.5 ? "#e07b39" : "#c75050" }}>
                                {(c.summary.success_rate * 100).toFixed(1)}%
                              </td>
                              <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                                {c.summary.retries_per_root.toFixed(2)}
                              </td>
                              <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                                {c.summary.p99.toFixed(0)}
                              </td>
                              <td style={{ padding: "6px 10px", textAlign: "right",
                                color: c.summary.dropped_queue > 0 ? "#e07b39" : T.faint }}>
                                {c.summary.dropped_queue}
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

            {/* Tab 2: Parameter Sweep */}
            {activeTab === "sweep" && (
              <>
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20, minHeight: 360 }}>
                  <h3 style={{ fontSize: 16, color: T.text, margin: "0 0 4px", fontWeight: 600 }}>
                    Parameter Sweep — {STRATEGIES[selectedStrategy].label}
                  </h3>
                  <p style={{ fontSize: 13, color: T.dim, margin: "0 0 16px" }}>
                    {STRATEGIES[selectedStrategy].sweeps[selectedSweep].param} — showing {STRATEGIES[selectedStrategy].clientName} client's success rate
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
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Overall SR</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Amplification</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Time (s)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sweepResult.results.map((r, i) => {
                          let clientSR = r.success_rate;
                          if (r.per_client && sweepResult.strategy) {
                            const pc = r.per_client.find((c) => c.name === sweepResult.strategy.clientName);
                            if (pc) clientSR = pc.success_rate;
                          }
                          return (
                            <tr key={i} style={{ borderBottom: `1px solid ${T.rowBorder}` }}>
                              <td style={{ padding: "6px 10px", color: T.accent }}>{fmtVal(r.param_value)}</td>
                              <td style={{ padding: "6px 10px", textAlign: "right",
                                color: clientSR > 0.8 ? "#388e3c" : clientSR > 0.5 ? "#e07b39" : "#c75050" }}>
                                {r.error ? "ERR" : `${(clientSR * 100).toFixed(1)}%`}
                              </td>
                              <td style={{ padding: "6px 10px", textAlign: "right", color: T.muted }}>
                                {r.success_rate ? `${(r.success_rate * 100).toFixed(1)}%` : "—"}
                              </td>
                              <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                                {r.amplification ? `${r.amplification.toFixed(2)}x` : "—"}
                              </td>
                              <td style={{ padding: "6px 10px", textAlign: "right", color: T.faint }}>
                                {r.elapsed_s || "—"}
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

            {/* Tab 3: Tornado */}
            {activeTab === "tornado" && (
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

            {/* Tab 4: Heatmap */}
            {activeTab === "heatmap" && (
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

            {/* Footer */}
            <div style={{
              marginTop: 12, padding: "12px 16px",
              background: T.hintBg, border: `1px solid ${T.border}`, borderRadius: 6,
              fontSize: 12, color: T.dim, lineHeight: 1.7,
            }}>
              <strong style={{ color: T.muted }}>How it works:</strong> This visualization calls the real Python simulator
              via the API server. Each data point is a full simulation run. Strategies and parameters are
              derived from <code style={{ color: T.accent }}>motivation-varied.yaml</code> where each strategy
              runs on an isolated service. Start server: <code style={{ color: T.accent }}>cd simulator && python3 bin/viz_server.py</code>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
