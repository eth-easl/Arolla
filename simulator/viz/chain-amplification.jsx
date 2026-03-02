import { useState, useCallback } from "react";

// ============================================================
// API Client
// ============================================================

const API_BASE = "http://localhost:8642";

async function apiChain(configPath, depths) {
  const res = await fetch(`${API_BASE}/api/chain`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config_path: configPath, depths }),
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

async function apiRun(configPath) {
  const res = await fetch(`${API_BASE}/api/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config_path: configPath }),
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
// Color Utilities
// ============================================================

const SERIES_COLORS = ["#67e8f9", "#f59e0b", "#a78bfa", "#ef4444", "#22c55e", "#fb923c", "#f472b6"];

// ============================================================
// Tab 1: Chain Depth Amplification
// ============================================================

function ChainWaterfall({ chainResult, T, width = 620, height = 170 }) {
  if (!chainResult || !chainResult.per_hop || chainResult.per_hop.length === 0) return null;

  const hops = chainResult.per_hop;
  const padL = 40, padR = 40, padT = 30, padB = 24;
  const cW = width - padL - padR;
  const cH = height - padT - padB;

  const svcMap = {};
  for (const row of hops) {
    const svc = row.service;
    if (!svcMap[svc]) svcMap[svc] = { total: 0, retries: 0, success: 0 };
    svcMap[svc].total += row.total_requests || 0;
    svcMap[svc].retries += row.retries || 0;
    svcMap[svc].success += row.success || 0;
  }

  const svcNames = Object.keys(svcMap);
  if (svcNames.length === 0) return null;

  const maxTotal = Math.max(1, ...svcNames.map((s) => svcMap[s].total));
  const boxW = Math.min(100, (cW - (svcNames.length - 1) * 50) / svcNames.length);
  const gapW = (cW - svcNames.length * boxW) / Math.max(1, svcNames.length - 1);

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      <defs>
        <marker id="chain-arrow" viewBox="0 0 10 6" refX="10" refY="3" markerWidth="8" markerHeight="6" orient="auto">
          <path d="M0,0 L10,3 L0,6" fill={T.svgLabel} />
        </marker>
      </defs>
      <text x={width / 2} y={16} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace">
        Depth {chainResult.depth} — Request Volume per Hop
      </text>
      {svcNames.map((svc, i) => {
        const x = padL + i * (boxW + gapW);
        const ratio = svcMap[svc].total / maxTotal;
        const barH = Math.max(12, ratio * cH);
        const y = padT + (cH - barH) / 2;
        const retryRatio = svcMap[svc].retries / Math.max(1, svcMap[svc].total);

        return (
          <g key={svc}>
            {i > 0 && (
              <line
                x1={padL + (i - 1) * (boxW + gapW) + boxW + 2} y1={padT + cH / 2}
                x2={x - 2} y2={padT + cH / 2}
                stroke={T.svgLabel} strokeWidth={Math.max(1, ratio * 4)}
                markerEnd="url(#chain-arrow)" opacity={0.7}
              />
            )}
            <rect x={x} y={y} width={boxW} height={barH * (1 - retryRatio)}
              fill="#22c55e" opacity={0.7} rx={3} />
            <rect x={x} y={y + barH * (1 - retryRatio)} width={boxW} height={barH * retryRatio}
              fill="#ef4444" opacity={0.7} rx={3} />
            <text x={x + boxW / 2} y={padT + cH + 18} textAnchor="middle" fill={T.svgLabel} fontSize={10}
              fontFamily="'JetBrains Mono', monospace">{svc}</text>
            <text x={x + boxW / 2} y={y - 5} textAnchor="middle" fill={T.text} fontSize={11}
              fontFamily="'JetBrains Mono', monospace" fontWeight={600}>{svcMap[svc].total}</text>
          </g>
        );
      })}
      <g transform={`translate(${padL}, ${height - 2})`}>
        <rect x={0} y={-7} width={8} height={7} fill="#22c55e" opacity={0.7} rx={1} />
        <text x={12} y={0} fill={T.svgLabel} fontSize={10} fontFamily="'JetBrains Mono', monospace">Original</text>
        <rect x={80} y={-7} width={8} height={7} fill="#ef4444" opacity={0.7} rx={1} />
        <text x={92} y={0} fill={T.svgLabel} fontSize={10} fontFamily="'JetBrains Mono', monospace">Retries</text>
      </g>
    </svg>
  );
}

function ChainDepthChart({ seriesData, depths, T, width = 640, height = 320 }) {
  if (!seriesData || seriesData.length === 0) return null;

  const padL = 60, padR = 20, padT = 25, padB = 55;
  const cW = width - padL - padR;
  const cH = height - padT - padB;

  const n = depths.length;
  const xScale = (i) => padL + (i / Math.max(1, n - 1)) * cW;
  const yScale = (v) => padT + (1 - v) * cH;

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {[0, 0.25, 0.5, 0.75, 1].map((v) => (
        <g key={v}>
          <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
          <text x={padL - 8} y={yScale(v) + 4} textAnchor="end" fill={T.svgLabel} fontSize={11}
            fontFamily="'JetBrains Mono', monospace">{(v * 100).toFixed(0)}%</text>
        </g>
      ))}

      {seriesData.map((series, si) => {
        const results = series.results || [];
        if (results.length === 0) return null;
        const points = results.map((d, i) => `${xScale(i)},${yScale(d.success_rate || 0)}`).join(" ");
        const color = SERIES_COLORS[si % SERIES_COLORS.length];
        return (
          <g key={si}>
            <polyline points={points} fill="none" stroke={color} strokeWidth={2.5} opacity={0.85} />
            {results.map((d, i) => (
              <circle key={i} cx={xScale(i)} cy={yScale(d.success_rate || 0)} r={3.5}
                fill={color} opacity={0.7} />
            ))}
          </g>
        );
      })}

      {depths.map((d, i) => (
        <text key={i} x={xScale(i)} y={height - 14} textAnchor="middle" fill={T.svgLabel} fontSize={12}
          fontFamily="'JetBrains Mono', monospace">{d}</text>
      ))}

      <text x={padL + cW / 2} y={height} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace">Chain Depth (hops)</text>
      <text x={8} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace" transform={`rotate(-90, 8, ${padT + cH / 2})`}>
        Success Rate
      </text>

      <g transform={`translate(${padL + 8}, ${padT + 4})`}>
        {seriesData.map((series, i) => (
          <g key={i} transform={`translate(${(i % 3) * 190}, ${Math.floor(i / 3) * 18})`}>
            <rect x={0} y={-8} width={12} height={3} rx={1} fill={SERIES_COLORS[i % SERIES_COLORS.length]} opacity={0.9} />
            <text x={16} y={-3} fill={T.text} fontSize={11} fontFamily="'JetBrains Mono', monospace">
              {series.label}
            </text>
          </g>
        ))}
      </g>
    </svg>
  );
}

// ============================================================
// Tab 2: Client Diversity — Per-client bar chart
// ============================================================

function ClientBarChart({ clients, T, width = 620, height = 280 }) {
  if (!clients || clients.length === 0) return null;

  const padL = 60, padR = 20, padT = 25, padB = 70;
  const cW = width - padL - padR;
  const cH = height - padT - padB;
  const n = clients.length;
  const barW = Math.min(60, (cW - (n - 1) * 10) / n);
  const gap = (cW - n * barW) / Math.max(1, n + 1);

  const yScale = (v) => padT + (1 - v) * cH;

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {[0, 0.25, 0.5, 0.75, 1].map((v) => (
        <g key={v}>
          <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
          <text x={padL - 8} y={yScale(v) + 4} textAnchor="end" fill={T.svgLabel} fontSize={11}
            fontFamily="'JetBrains Mono', monospace">{(v * 100).toFixed(0)}%</text>
        </g>
      ))}

      {clients.map((c, i) => {
        const sr = c.summary.success_rate || 0;
        const x = padL + gap + i * (barW + gap);
        const barH = sr * cH;
        const color = SERIES_COLORS[i % SERIES_COLORS.length];
        return (
          <g key={i}>
            <rect x={x} y={yScale(sr)} width={barW} height={barH} fill={color} opacity={0.75} rx={3} />
            <text x={x + barW / 2} y={yScale(sr) - 7} textAnchor="middle" fill={color} fontSize={12}
              fontWeight={600} fontFamily="'JetBrains Mono', monospace">
              {(sr * 100).toFixed(1)}%
            </text>
            <text x={x + barW / 2} y={height - 34} textAnchor="middle" fill={T.svgLabel} fontSize={10}
              fontFamily="'JetBrains Mono', monospace"
              transform={`rotate(-25, ${x + barW / 2}, ${height - 34})`}>
              {c.name}
            </text>
          </g>
        );
      })}

      <text x={8} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace" transform={`rotate(-90, 8, ${padT + cH / 2})`}>
        Success Rate
      </text>
    </svg>
  );
}

// ============================================================
// Tab 2: Client Diversity — Per-client timeline
// ============================================================

function ClientTimelineChart({ clients, faultEvents, T, width = 620, height = 260 }) {
  if (!clients || clients.length === 0) return null;

  const padL = 60, padR = 20, padT = 25, padB = 45;
  const cW = width - padL - padR;
  const cH = height - padT - padB;

  let maxT = 0;
  for (const c of clients) {
    if (c.timeseries && c.timeseries.length > 0) {
      const last = c.timeseries[c.timeseries.length - 1];
      if (last.timepoint > maxT) maxT = last.timepoint;
    }
  }
  if (maxT === 0) return null;

  const xScale = (t) => padL + (t / maxT) * cW;
  const yScale = (v) => padT + (1 - v) * cH;

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {[0, 0.25, 0.5, 0.75, 1].map((v) => (
        <g key={v}>
          <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
          <text x={padL - 8} y={yScale(v) + 4} textAnchor="end" fill={T.svgLabel} fontSize={11}
            fontFamily="'JetBrains Mono', monospace">{(v * 100).toFixed(0)}%</text>
        </g>
      ))}

      {(faultEvents || []).map((fe, i) => (
        <g key={`fault-${i}`}>
          <rect x={xScale(fe.start_time_s)} y={padT}
            width={Math.max(0, xScale(fe.end_time_s) - xScale(fe.start_time_s))} height={cH}
            fill="#ef4444" opacity={0.12} rx={2} />
          <text x={(xScale(fe.start_time_s) + xScale(fe.end_time_s)) / 2} y={padT + 16}
            textAnchor="middle" fill="#ef4444" fontSize={11} fontWeight={600}
            fontFamily="'JetBrains Mono', monospace">
            {fe.parameters?.p_fail
              ? `Partial Failure (${(fe.parameters.p_fail * 100).toFixed(0)}%)`
              : "Fault Injection"}
          </text>
        </g>
      ))}

      {clients.map((c, ci) => {
        const ts = c.timeseries || [];
        if (ts.length === 0) return null;
        const points = ts.map((d) => {
          const sr = d.root_requests > 0 ? d.success_root / d.root_requests : 1;
          return `${xScale(d.timepoint)},${yScale(sr)}`;
        }).join(" ");
        const color = SERIES_COLORS[ci % SERIES_COLORS.length];
        return (
          <polyline key={ci} points={points} fill="none" stroke={color} strokeWidth={2} opacity={0.8} />
        );
      })}

      {[0, 0.25, 0.5, 0.75, 1].map((f) => (
        <text key={f} x={xScale(f * maxT)} y={height - 10} textAnchor="middle" fill={T.svgLabel} fontSize={11}
          fontFamily="'JetBrains Mono', monospace">{(f * maxT).toFixed(0)}s</text>
      ))}

      <text x={padL + cW / 2} y={height} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace">Time (s)</text>
      <text x={8} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace" transform={`rotate(-90, 8, ${padT + cH / 2})`}>
        Success Rate
      </text>

      <g transform={`translate(${padL + 8}, ${padT + 4})`}>
        {clients.map((c, i) => (
          <g key={i} transform={`translate(${(i % 3) * 180}, ${Math.floor(i / 3) * 18})`}>
            <rect x={0} y={-8} width={12} height={3} rx={1} fill={SERIES_COLORS[i % SERIES_COLORS.length]} opacity={0.9} />
            <text x={16} y={-3} fill={T.text} fontSize={11} fontFamily="'JetBrains Mono', monospace">
              {c.name}
            </text>
          </g>
        ))}
      </g>
    </svg>
  );
}

// ============================================================
// Main App
// ============================================================

export default function App() {
  const T = useTheme();
  const [activeTab, setActiveTab] = useState("chain");
  const [configs, setConfigs] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [savedPath, setSavedPath] = useState(null);

  // Tab 1 state
  const [chainConfigs, setChainConfigs] = useState([]);
  const [depths, setDepths] = useState([1, 2, 3, 4, 5]);
  const [chainSeriesData, setChainSeriesData] = useState([]);
  const [selectedDepthIdx, setSelectedDepthIdx] = useState(4);

  // Tab 2 state
  const [diversityConfig, setDiversityConfig] = useState("");
  const [diversityResult, setDiversityResult] = useState(null);

  const loadConfigs = useCallback(async () => {
    try {
      const data = await apiListConfigs();
      setConfigs(data.configs);
    } catch (e) {
      setError(`Failed to connect to server: ${e.message}`);
    }
  }, []);

  const addChainConfig = useCallback((path) => {
    if (path && !chainConfigs.includes(path)) {
      setChainConfigs((prev) => [...prev, path]);
    }
  }, [chainConfigs]);

  const removeChainConfig = useCallback((path) => {
    setChainConfigs((prev) => prev.filter((c) => c !== path));
    setChainSeriesData((prev) => prev.filter((s) => s.configPath !== path));
  }, []);

  const runChainStudy = useCallback(async () => {
    if (chainConfigs.length === 0) return;
    setLoading(true);
    setError(null);
    setSavedPath(null);
    const results = [];
    for (const configPath of chainConfigs) {
      try {
        const data = await apiChain(configPath, depths);
        const label = configPath.split("/").pop().replace(".yaml", "");
        results.push({ configPath, label, results: data.results });
      } catch (e) {
        results.push({ configPath, label: configPath, results: [], error: e.message });
      }
    }
    setChainSeriesData(results);
    try {
      const saveRes = await apiSaveResults("chain_depth", { series: results, depths }, "chain_amplification");
      setSavedPath(saveRes.path);
    } catch (e) { /* save failure is non-critical */ }
    setLoading(false);
  }, [chainConfigs, depths]);

  const runDiversity = useCallback(async () => {
    if (!diversityConfig) return;
    setLoading(true);
    setError(null);
    setSavedPath(null);
    try {
      const data = await apiRun(diversityConfig);
      setDiversityResult(data);
      const saveRes = await apiSaveResults("diversity", data, diversityConfig.replace(/\//g, "_").replace(".yaml", ""));
      setSavedPath(saveRes.path);
    } catch (e) {
      setError(e.message);
    }
    setLoading(false);
  }, [diversityConfig]);

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
    { key: "chain", label: "Chain Depth" },
    { key: "diversity", label: "Client Diversity" },
  ];

  const selectedDepth = depths[selectedDepthIdx] || depths[depths.length - 1];
  const waterfallData = chainSeriesData.length > 0
    ? (chainSeriesData[0].results || []).find((r) => r.depth === selectedDepth)
    : null;

  return (
    <div style={{
      minHeight: "100vh", color: T.text,
      fontFamily: "'JetBrains Mono', 'Fira Code', monospace", padding: "20px",
    }}>
      <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet" />

      <div style={{ maxWidth: 1060, margin: "0 auto" }}>
        <div style={{ marginBottom: 24, borderBottom: `1px solid ${T.border}`, paddingBottom: 14 }}>
          <h1 style={{ fontSize: 24, fontWeight: 700, color: T.accent, margin: 0 }}>
            Retry Amplification in Depth
          </h1>
          <p style={{ fontSize: 14, color: T.dim, margin: "6px 0 0" }}>
            How retries compound across chain depth and how heterogeneous clients interfere
          </p>
        </div>

        {/* Tabs */}
        <div style={{ display: "flex", gap: 2, marginBottom: 14 }}>
          {tabs.map((tab) => (
            <button key={tab.key} onClick={() => setActiveTab(tab.key)} style={{
              padding: "8px 20px",
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
          {/* Left — Controls */}
          <div style={{ width: 300, flexShrink: 0 }}>
            {!configs && (
              <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
                <button onClick={loadConfigs} style={btnStyle}>Load Configs from Server</button>
              </div>
            )}

            {activeTab === "chain" && configs && (
              <>
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
                  <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                    Chain Configs to Compare
                  </h3>
                  <select onChange={(e) => addChainConfig(e.target.value)} value=""
                    style={{ ...selectStyle, width: "100%", marginBottom: 8 }}>
                    <option value="">+ Add a config...</option>
                    {configs.filter((c) => !chainConfigs.includes(c)).map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                  {chainConfigs.map((c) => (
                    <div key={c} style={{
                      display: "flex", alignItems: "center", gap: 6, marginBottom: 4,
                      padding: "5px 10px", background: T.chipBg,
                      border: `1px solid ${T.border}`, borderRadius: 4, fontSize: 12,
                    }}>
                      <span style={{ flex: 1, color: T.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {c.split("/").pop().replace(".yaml", "")}
                      </span>
                      <button onClick={() => removeChainConfig(c)}
                        style={{ background: "none", border: "none", color: "#ef4444", cursor: "pointer", fontSize: 14, padding: 0 }}>
                        x
                      </button>
                    </div>
                  ))}
                </div>

                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
                  <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                    Chain Depths
                  </h3>
                  <label style={{ fontSize: 12, color: T.muted, display: "block", marginBottom: 4 }}>
                    Depths (comma-separated)
                  </label>
                  <input type="text" value={depths.join(",")}
                    onChange={(e) => {
                      const vals = e.target.value.split(",").map(Number).filter((n) => n > 0);
                      if (vals.length > 0) setDepths(vals);
                    }}
                    style={{ ...selectStyle, width: "100%" }} />
                </div>

                <button onClick={runChainStudy} disabled={loading || chainConfigs.length === 0} style={{
                  ...btnStyle,
                  opacity: loading || chainConfigs.length === 0 ? 0.5 : 1,
                  cursor: loading ? "wait" : "pointer", marginBottom: 14,
                }}>
                  {loading ? `Running ${chainConfigs.length} configs...` : `Run Chain Study (${chainConfigs.length} configs)`}
                </button>

                {chainSeriesData.length > 0 && (
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16 }}>
                    <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                      Inspect Depth
                    </h3>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                      <span style={{ fontSize: 13, color: T.muted }}>Depth</span>
                      <span style={{ fontSize: 16, color: T.accent, fontWeight: 700 }}>{selectedDepth}</span>
                    </div>
                    <input type="range" min={0} max={depths.length - 1} step={1} value={selectedDepthIdx}
                      onChange={(e) => setSelectedDepthIdx(parseInt(e.target.value))}
                      style={{ width: "100%", accentColor: T.accent }} />
                  </div>
                )}
              </>
            )}

            {activeTab === "diversity" && configs && (
              <>
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
                  <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                    Multi-Client Config
                  </h3>
                  <select value={diversityConfig} onChange={(e) => setDiversityConfig(e.target.value)}
                    style={{ ...selectStyle, width: "100%" }}>
                    <option value="">Select a config...</option>
                    {configs.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                  <p style={{ fontSize: 12, color: T.faint, margin: "8px 0 0" }}>
                    Pick a config with multiple clients using different retry strategies
                  </p>
                </div>

                <button onClick={runDiversity} disabled={loading || !diversityConfig} style={{
                  ...btnStyle,
                  opacity: loading || !diversityConfig ? 0.5 : 1,
                  cursor: loading ? "wait" : "pointer", marginBottom: 14,
                }}>
                  {loading ? "Running..." : "Run Experiment"}
                </button>
              </>
            )}

            {error && (
              <div style={{
                background: T.errorBg, border: `1px solid ${T.errorBorder}`,
                borderRadius: 6, padding: 12, marginTop: 14, fontSize: 12, color: T.errorText,
              }}>
                {error}
              </div>
            )}
            {savedPath && !loading && (
              <div style={{
                background: T.hintBg, border: `1px solid ${T.border}`,
                borderRadius: 6, padding: "8px 12px", marginTop: 14, fontSize: 11, color: T.muted,
              }}>
                Saved to: <span style={{ color: T.accent }}>{savedPath}</span>
              </div>
            )}
          </div>

          {/* Right — Visualization */}
          <div style={{ flex: 1, minWidth: 0 }}>
            {/* ===== Tab 1: Chain Depth ===== */}
            {activeTab === "chain" && (
              <>
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20, minHeight: 360 }}>
                  <h3 style={{ fontSize: 16, color: T.text, margin: "0 0 14px", fontWeight: 600 }}>
                    Success Rate vs. Chain Depth
                  </h3>
                  {chainSeriesData.length > 0 ? (
                    <div style={{ overflowX: "auto" }}>
                      <ChainDepthChart seriesData={chainSeriesData} depths={depths} T={T} width={640} height={320} />
                    </div>
                  ) : (
                    <div style={{ textAlign: "center", padding: 70, color: T.faint, fontSize: 14 }}>
                      <p>Add chain configs, then click "Run Chain Study"</p>
                      <p style={{ fontSize: 12, marginTop: 10 }}>
                        The simulator will build chains of varying depth and measure retry amplification at each.
                        Compare no-control vs AIMD vs SYSNAME by adding multiple configs.
                      </p>
                    </div>
                  )}
                </div>

                {waterfallData && (
                  <div style={{ marginTop: 12, background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20 }}>
                    <ChainWaterfall chainResult={waterfallData} T={T} width={640} height={170} />
                  </div>
                )}

                {chainSeriesData.length > 0 && (
                  <div style={{ marginTop: 12, background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, overflowX: "auto" }}>
                    <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase" }}>
                      Results at Depth {selectedDepth}
                    </h3>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                      <thead>
                        <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                          <th style={{ textAlign: "left", padding: "6px 10px", color: T.muted }}>Config</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Success Rate</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Amplification</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>P99 (ms)</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Time (s)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {chainSeriesData.map((series, i) => {
                          const point = (series.results || []).find((r) => r.depth === selectedDepth);
                          return (
                            <tr key={i} style={{ borderBottom: `1px solid ${T.rowBorder}` }}>
                              <td style={{ padding: "6px 10px", color: T.text }}>{series.label}</td>
                              <td style={{ padding: "6px 10px", textAlign: "right",
                                color: point?.success_rate > 0.8 ? "#22c55e" : point?.success_rate > 0.5 ? "#f59e0b" : "#ef4444" }}>
                                {point ? `${(point.success_rate * 100).toFixed(1)}%` : "—"}
                              </td>
                              <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                                {point?.amplification ? `${point.amplification.toFixed(2)}x` : "—"}
                              </td>
                              <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                                {point?.p99 ? `${point.p99.toFixed(0)}` : "—"}
                              </td>
                              <td style={{ padding: "6px 10px", textAlign: "right", color: T.faint }}>
                                {point?.elapsed_s || "—"}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}

                <div style={{
                  marginTop: 12, padding: "12px 16px",
                  background: T.hintBg, border: `1px solid ${T.border}`, borderRadius: 6,
                  fontSize: 12, color: T.dim, lineHeight: 1.7,
                }}>
                  <strong style={{ color: T.muted }}>Analytic reference:</strong> Without control,
                  amplification grows as <code style={{ color: T.accent }}>A(d) = (1 + fail_rate x max_retries)^d</code>.
                  With 50% failure and 3 retries per hop: A(5) = 2.5^5 = 97.7x. Server-side budgets
                  (AIMD, SYSNAME) bound this to near-linear growth.
                </div>
              </>
            )}

            {/* ===== Tab 2: Client Diversity ===== */}
            {activeTab === "diversity" && (
              <>
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20, minHeight: 320 }}>
                  <h3 style={{ fontSize: 16, color: T.text, margin: "0 0 14px", fontWeight: 600 }}>
                    Per-Client Success Rate
                  </h3>
                  {diversityResult ? (
                    <div style={{ overflowX: "auto" }}>
                      <ClientBarChart clients={diversityResult.clients} T={T} width={640} height={280} />
                    </div>
                  ) : (
                    <div style={{ textAlign: "center", padding: 70, color: T.faint, fontSize: 14 }}>
                      <p>Select a multi-client config and click "Run Experiment"</p>
                      <p style={{ fontSize: 12, marginTop: 10 }}>
                        Use configs where multiple clients with different retry strategies share a service
                        (e.g., motivation-mixed.yaml). Shows how aggressive clients starve polite ones.
                      </p>
                    </div>
                  )}
                </div>

                {diversityResult && (
                  <div style={{ marginTop: 12, background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20 }}>
                    <h3 style={{ fontSize: 16, color: T.text, margin: "0 0 14px", fontWeight: 600 }}>
                      Success Rate Over Time
                    </h3>
                    <div style={{ overflowX: "auto" }}>
                      <ClientTimelineChart
                        clients={diversityResult.clients}
                        faultEvents={diversityResult.fault_events}
                        T={T} width={640} height={260}
                      />
                    </div>
                  </div>
                )}

                {diversityResult && (
                  <div style={{ marginTop: 12, background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, overflowX: "auto" }}>
                    <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase" }}>
                      Client Summary
                    </h3>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                      <thead>
                        <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                          <th style={{ textAlign: "left", padding: "6px 10px", color: T.muted }}>Client</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Total</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Success Rate</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Retries/Root</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>P99 (ms)</th>
                          <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Queue Drops</th>
                        </tr>
                      </thead>
                      <tbody>
                        {diversityResult.clients.map((c, i) => (
                          <tr key={i} style={{ borderBottom: `1px solid ${T.rowBorder}` }}>
                            <td style={{ padding: "6px 10px", color: SERIES_COLORS[i % SERIES_COLORS.length], fontWeight: 600 }}>
                              {c.name}
                            </td>
                            <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                              {c.summary.total}
                            </td>
                            <td style={{ padding: "6px 10px", textAlign: "right",
                              color: c.summary.success_rate > 0.8 ? "#22c55e" : c.summary.success_rate > 0.5 ? "#f59e0b" : "#ef4444" }}>
                              {(c.summary.success_rate * 100).toFixed(1)}%
                            </td>
                            <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                              {c.summary.retries_per_root.toFixed(2)}
                            </td>
                            <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                              {c.summary.p99.toFixed(0)}
                            </td>
                            <td style={{ padding: "6px 10px", textAlign: "right", color: c.summary.dropped_queue > 0 ? "#f59e0b" : T.faint }}>
                              {c.summary.dropped_queue}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}

                {diversityResult && (() => {
                  const clients = diversityResult.clients;
                  const rates = clients.map((c) => c.summary.success_rate);
                  const best = Math.max(...rates);
                  const worst = Math.min(...rates);
                  const fairness = worst / Math.max(best, 0.001);
                  const totalRetries = clients.reduce((s, c) => s + c.summary.total * c.summary.retries_per_root, 0);
                  const totalRequests = clients.reduce((s, c) => s + c.summary.total, 0);

                  return (
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 10, marginTop: 12 }}>
                      {[
                        { label: "Best Client", value: `${(best * 100).toFixed(1)}%`, color: "#22c55e" },
                        { label: "Worst Client", value: `${(worst * 100).toFixed(1)}%`, color: "#ef4444" },
                        { label: "Fairness", value: `${(fairness * 100).toFixed(0)}%`, color: fairness > 0.8 ? "#22c55e" : "#f59e0b" },
                        { label: "Total Amplification", value: `${((totalRequests + totalRetries) / Math.max(totalRequests, 1)).toFixed(2)}x`, color: T.accent },
                      ].map((stat) => (
                        <div key={stat.label} style={{
                          background: T.panel, border: `1px solid ${T.border}`, borderRadius: 6, padding: "12px 14px", textAlign: "center",
                        }}>
                          <div style={{ fontSize: 11, color: T.dim, marginBottom: 5, textTransform: "uppercase", letterSpacing: "0.06em" }}>{stat.label}</div>
                          <div style={{ fontSize: 18, color: stat.color, fontWeight: 700 }}>{stat.value}</div>
                        </div>
                      ))}
                    </div>
                  );
                })()}

                <div style={{
                  marginTop: 12, padding: "12px 16px",
                  background: T.hintBg, border: `1px solid ${T.border}`, borderRadius: 6,
                  fontSize: 12, color: T.dim, lineHeight: 1.7,
                }}>
                  <strong style={{ color: T.muted }}>Key insight:</strong> Without server-side control,
                  aggressive clients (high retry counts, no backoff) consume disproportionate server capacity
                  during faults, starving polite clients. Server-side retry budgets restore fairness.
                </div>
              </>
            )}

            {/* Shared footer */}
            <div style={{
              marginTop: 12, padding: "12px 16px",
              background: T.hintBg, border: `1px solid ${T.border}`, borderRadius: 6,
              fontSize: 12, color: T.dim, lineHeight: 1.7,
            }}>
              <strong style={{ color: T.muted }}>How it works:</strong> This visualization calls the real Python simulator
              via the API server (port 8642). Start server: <code style={{ color: T.accent }}>cd simulator && python3 bin/viz_server.py</code>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
