import { useState, useCallback } from "react";

// ============================================================
// API Client
// ============================================================

const API_BASE = "http://localhost:8642";

async function apiScaling(configPath, clientCounts, totalRps) {
  const res = await fetch(`${API_BASE}/api/scaling`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config_path: configPath, client_counts: clientCounts, total_rps: totalRps }),
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
// Constants
// ============================================================

const DEFAULT_CLIENT_COUNTS = [1, 5, 10, 50, 100, 500, 1000];
// Strategy visual styles: vivid colors + distinct dash patterns (matching paper figures)
const STRATEGY_STYLES = {
  no_retries:                 { color: "#8b8b8b", dash: "3,4",       width: 2   },
  three_retries:              { color: "#2563eb", dash: "",           width: 2.5 },
  exponential_backoff_jitter: { color: "#ea7317", dash: "10,4",      width: 2.5 },
  circuit_breaker:            { color: "#d63384", dash: "3,5",       width: 2.5 },
  retry_budget:               { color: "#40916c", dash: "10,3,3,3",  width: 2.5 },
  arolla_retry_budget:        { color: "#006d77", dash: "",           width: 3.5 },
  arolla_budget:              { color: "#006d77", dash: "",           width: 3.5 },
  arolla:                     { color: "#006d77", dash: "",           width: 3.5 },
  sysname:                    { color: "#006d77", dash: "",           width: 3.5 },
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

function displayName(s) { return s.replace(/arolla[\w_]*/gi, "Arolla").replace(/sysname/gi, "Arolla"); }

// ============================================================
// Reusable: ScalingMetricChart (log-scale X)
// ============================================================

function ScalingMetricChart({
  seriesData, clientCounts, metricKey, yLabel, yDomain, yFormat, selectedIdx,
  T, width = 340, height = 240,
}) {
  if (!seriesData || seriesData.length === 0) return null;

  const padL = 56, padR = 14, padT = 22, padB = 48;
  const cW = width - padL - padR;
  const cH = height - padT - padB;

  // Log-scale X
  const logCounts = clientCounts.map((c) => Math.log10(Math.max(1, c)));
  const minLog = Math.min(...logCounts);
  const maxLog = Math.max(...logCounts);
  const logRange = maxLog - minLog || 1;
  const xScale = (i) => padL + ((logCounts[i] - minLog) / logRange) * cW;

  // Y-axis domain
  let yMin = 0, yMax = 1;
  if (yDomain) {
    [yMin, yMax] = yDomain;
  } else {
    // Auto-scale from data
    let allVals = [];
    for (const s of seriesData) {
      for (const r of (s.results || [])) {
        const v = r[metricKey];
        if (v != null && isFinite(v)) allVals.push(v);
      }
    }
    if (allVals.length > 0) {
      yMin = Math.min(0, Math.min(...allVals));
      yMax = Math.max(...allVals) * 1.1 || 1;
    }
  }
  const yRange = yMax - yMin || 1;
  const yScale = (v) => padT + ((yMax - v) / yRange) * cH;

  // Y tick values
  const nTicks = 5;
  const yTicks = Array.from({ length: nTicks }, (_, i) => yMin + (i / (nTicks - 1)) * yRange);

  const fmt = yFormat || ((v) => {
    if (yMax <= 1.5 && yMin >= -0.5) return `${(v * 100).toFixed(0)}%`;
    if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
    return v.toFixed(v < 10 ? 1 : 0);
  });

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {yTicks.map((v, i) => (
        <g key={i}>
          <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
          <text x={padL - 6} y={yScale(v) + 4} textAnchor="end" fill={T.svgLabel} fontSize={10}
            fontFamily="'JetBrains Mono', monospace">{fmt(v)}</text>
        </g>
      ))}

      {selectedIdx >= 0 && selectedIdx < clientCounts.length && (
        <rect x={xScale(selectedIdx) - 4} y={padT} width={8} height={cH}
          fill={T.accent} opacity={0.06} rx={3} />
      )}

      {seriesData.map((series, si) => {
        const results = series.results || [];
        if (results.length === 0) return null;
        const pts = results.map((d, i) => {
          const v = d[metricKey];
          return v != null && isFinite(v) ? `${xScale(i)},${yScale(v)}` : null;
        }).filter(Boolean);
        if (pts.length === 0) return null;
        const style = getStyle(series.config_name, si);
        return (
          <g key={si}>
            <polyline points={pts.join(" ")} fill="none" stroke={style.color}
              strokeWidth={style.width} strokeDasharray={style.dash || undefined} opacity={0.9} />
            {results.map((d, i) => {
              const v = d[metricKey];
              if (v == null || !isFinite(v)) return null;
              return (
                <circle key={i} cx={xScale(i)} cy={yScale(v)} r={3}
                  fill={style.color} opacity={selectedIdx === i ? 1 : 0.6}
                  stroke={selectedIdx === i ? T.text : "none"} strokeWidth={selectedIdx === i ? 1.5 : 0} />
              );
            })}
          </g>
        );
      })}

      {clientCounts.map((c, i) => (
        <text key={i} x={xScale(i)} y={height - 12} textAnchor="middle"
          fill={selectedIdx === i ? T.accent : T.svgLabel} fontSize={10}
          fontWeight={selectedIdx === i ? 600 : 400}
          fontFamily="'JetBrains Mono', monospace">{c}</text>
      ))}

      <text x={padL + cW / 2} y={height} textAnchor="middle" fill={T.svgAxis} fontSize={10}
        fontFamily="'JetBrains Mono', monospace">Client Count N (log)</text>
      <text x={6} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={10}
        fontFamily="'JetBrains Mono', monospace" transform={`rotate(-90, 6, ${padT + cH / 2})`}>
        {yLabel}
      </text>
    </svg>
  );
}

// ============================================================
// Chart legend (shared across all panels)
// ============================================================

function ChartLegend({ seriesData, T }) {
  if (!seriesData || seriesData.length === 0) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 16px", marginBottom: 10 }}>
      {seriesData.map((s, i) => {
        const style = getStyle(s.config_name, i);
        return (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: T.text }}>
            <svg width={24} height={6}>
              <line x1={0} y1={3} x2={24} y2={3}
                stroke={style.color} strokeWidth={style.width}
                strokeDasharray={style.dash || undefined} />
            </svg>
            {displayName(s.label)}
          </div>
        );
      })}
    </div>
  );
}

// ============================================================
// Main App
// ============================================================

export default function App() {
  const T = useTheme();
  const [configs, setConfigs] = useState(null);
  const [selectedConfigs, setSelectedConfigs] = useState([]);
  const [totalRps, setTotalRps] = useState(200);
  const [clientCounts, setClientCounts] = useState(DEFAULT_CLIENT_COUNTS);
  const [selectedClientIdx, setSelectedClientIdx] = useState(2);
  const [seriesData, setSeriesData] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [savedPath, setSavedPath] = useState(null);

  const loadConfigs = useCallback(async () => {
    try {
      const data = await apiListConfigs();
      setConfigs(data.configs);
    } catch (e) {
      setError(`Failed to connect to server: ${e.message}`);
    }
  }, []);

  const addConfig = useCallback((path) => {
    if (path && !selectedConfigs.includes(path)) {
      setSelectedConfigs((prev) => [...prev, path]);
    }
  }, [selectedConfigs]);

  const removeConfig = useCallback((path) => {
    setSelectedConfigs((prev) => prev.filter((c) => c !== path));
    setSeriesData((prev) => prev.filter((s) => s.configPath !== path));
  }, []);

  const runScaling = useCallback(async () => {
    if (selectedConfigs.length === 0) return;
    setLoading(true);
    setError(null);
    setSavedPath(null);
    const results = [];
    for (const configPath of selectedConfigs) {
      try {
        const data = await apiScaling(configPath, clientCounts, totalRps);
        const label = configPath.split("/").pop().replace(".yaml", "");
        results.push({ configPath, label, results: data.results });
      } catch (e) {
        results.push({ configPath, label: configPath, results: [], error: e.message });
      }
    }
    setSeriesData(results);
    try {
      const saveRes = await apiSaveResults("scaling", { series: results, clientCounts, totalRps }, "multi_client");
      setSavedPath(saveRes.path);
    } catch (e) { /* save failure is non-critical */ }
    setLoading(false);
  }, [selectedConfigs, clientCounts, totalRps]);

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

  const selectedCount = clientCounts[selectedClientIdx] || clientCounts[0];

  return (
    <div style={{
      minHeight: "100vh", color: T.text,
      fontFamily: "'JetBrains Mono', 'Fira Code', monospace", padding: "20px",
    }}>
      <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet" />

      <div style={{ maxWidth: 1120, margin: "0 auto" }}>
        <div style={{ marginBottom: 24, borderBottom: `1px solid ${T.border}`, paddingBottom: 14 }}>
          <h1 style={{ fontSize: 24, fontWeight: 700, color: T.accent, margin: 0 }}>
            Multi-Client & Single-Service
          </h1>
          <p style={{ fontSize: 14, color: T.dim, margin: "6px 0 0" }}>
            How retry strategies scale with client count — goodput, amplification, retry efficiency vs N
          </p>
        </div>

        <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
          {/* Left — Controls */}
          <div style={{ width: 280, flexShrink: 0 }}>
            <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
              <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Configs to Compare
              </h3>
              {!configs ? (
                <button onClick={loadConfigs} style={btnStyle}>Load Configs from Server</button>
              ) : (
                <>
                  <select onChange={(e) => addConfig(e.target.value)} value=""
                    style={{ ...selectStyle, width: "100%", marginBottom: 8 }}>
                    <option value="">+ Add a config...</option>
                    {configs.filter((c) => !selectedConfigs.includes(c)).map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                  {selectedConfigs.map((c) => (
                    <div key={c} style={{
                      display: "flex", alignItems: "center", gap: 6, marginBottom: 4,
                      padding: "5px 10px", background: T.chipBg,
                      border: `1px solid ${T.border}`, borderRadius: 4, fontSize: 12,
                    }}>
                      <span style={{ flex: 1, color: T.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {displayName(c.split("/").pop().replace(".yaml", ""))}
                      </span>
                      <button onClick={() => removeConfig(c)}
                        style={{ background: "none", border: "none", color: "#b74444", cursor: "pointer", fontSize: 14, padding: 0 }}>
                        x
                      </button>
                    </div>
                  ))}
                </>
              )}
            </div>

            <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16, marginBottom: 14 }}>
              <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                Scaling Parameters
              </h3>
              <div style={{ marginBottom: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                  <span style={{ fontSize: 13, color: T.muted }}>Total RPS</span>
                  <span style={{ fontSize: 13, color: T.accent, fontWeight: 600 }}>{totalRps}</span>
                </div>
                <input type="range" min={50} max={500} step={10} value={totalRps}
                  onChange={(e) => setTotalRps(parseInt(e.target.value))}
                  style={{ width: "100%", accentColor: T.accent }} />
              </div>
              <div style={{ marginBottom: 12 }}>
                <label style={{ fontSize: 12, color: T.muted, display: "block", marginBottom: 4 }}>
                  Client counts (comma-separated)
                </label>
                <input type="text" value={clientCounts.join(",")}
                  onChange={(e) => {
                    const vals = e.target.value.split(",").map(Number).filter((n) => n > 0);
                    if (vals.length > 0) setClientCounts(vals);
                  }}
                  style={{ ...selectStyle, width: "100%" }} />
              </div>
            </div>

            <button onClick={runScaling} disabled={loading || selectedConfigs.length === 0} style={{
              ...btnStyle,
              opacity: loading || selectedConfigs.length === 0 ? 0.5 : 1,
              cursor: loading ? "wait" : "pointer", marginBottom: 14,
            }}>
              {loading ? `Running ${selectedConfigs.length} configs...` : `Run Scaling Study (${selectedConfigs.length} configs)`}
            </button>

            {seriesData.length > 0 && (
              <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 16 }}>
                <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  Inspect
                </h3>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                  <span style={{ fontSize: 13, color: T.muted }}>Clients</span>
                  <span style={{ fontSize: 16, color: T.accent, fontWeight: 700 }}>{selectedCount}</span>
                </div>
                <input type="range" min={0} max={clientCounts.length - 1} step={1} value={selectedClientIdx}
                  onChange={(e) => setSelectedClientIdx(parseInt(e.target.value))}
                  style={{ width: "100%", accentColor: T.accent }} />
              </div>
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

          {/* Right — Chart Grid */}
          <div style={{ flex: 1, minWidth: 0 }}>
            {seriesData.length > 0 ? (
              <>
                <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "14px 16px 6px" }}>
                  <ChartLegend seriesData={seriesData} T={T} />
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginTop: 10 }}>
                  {/* Success Rate vs N */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "12px 10px" }}>
                    <h4 style={{ fontSize: 12, color: T.muted, margin: "0 0 4px", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                      Success Rate vs N
                    </h4>
                    <ScalingMetricChart
                      seriesData={seriesData} clientCounts={clientCounts} metricKey="success_rate"
                      yLabel="Success Rate" yDomain={[0, 1]}
                      yFormat={(v) => `${(v * 100).toFixed(0)}%`}
                      selectedIdx={selectedClientIdx} T={T} />
                  </div>

                  {/* Amplification vs N */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "12px 10px" }}>
                    <h4 style={{ fontSize: 12, color: T.muted, margin: "0 0 4px", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                      Amplification vs N
                    </h4>
                    <ScalingMetricChart
                      seriesData={seriesData} clientCounts={clientCounts} metricKey="amplification"
                      yLabel="Amplification A"
                      yFormat={(v) => `${v.toFixed(1)}x`}
                      selectedIdx={selectedClientIdx} T={T} />
                  </div>

                  {/* Goodput vs N */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "12px 10px" }}>
                    <h4 style={{ fontSize: 12, color: T.muted, margin: "0 0 4px", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                      Goodput vs N
                    </h4>
                    <ScalingMetricChart
                      seriesData={seriesData} clientCounts={clientCounts} metricKey="goodput_rps"
                      yLabel="Goodput (RPS)"
                      yFormat={(v) => v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0)}
                      selectedIdx={selectedClientIdx} T={T} />
                  </div>

                  {/* Retry Efficiency vs N */}
                  <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: "12px 10px" }}>
                    <h4 style={{ fontSize: 12, color: T.muted, margin: "0 0 4px", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                      Retry Efficiency vs N
                    </h4>
                    <ScalingMetricChart
                      seriesData={seriesData} clientCounts={clientCounts} metricKey="retry_efficiency"
                      yLabel="Retry Efficiency"  yDomain={[0, 1]}
                      yFormat={(v) => `${(v * 100).toFixed(0)}%`}
                      selectedIdx={selectedClientIdx} T={T} />
                  </div>
                </div>

                {/* Summary table */}
                <div style={{
                  marginTop: 10, background: T.panel, border: `1px solid ${T.border}`,
                  borderRadius: 8, padding: 16, overflowX: "auto",
                }}>
                  <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase" }}>
                    At N = {selectedCount}
                  </h3>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                    <thead>
                      <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                        <th style={{ textAlign: "left", padding: "5px 8px", color: T.muted }}>Config</th>
                        <th style={{ textAlign: "right", padding: "5px 8px", color: T.muted }}>Success</th>
                        <th style={{ textAlign: "right", padding: "5px 8px", color: T.muted }}>Amplif.</th>
                        <th style={{ textAlign: "right", padding: "5px 8px", color: T.muted }}>Goodput</th>
                        <th style={{ textAlign: "right", padding: "5px 8px", color: T.muted }}>Retry Eff.</th>
                        <th style={{ textAlign: "right", padding: "5px 8px", color: T.muted }}>Fairness</th>
                        <th style={{ textAlign: "right", padding: "5px 8px", color: T.muted }}>P99</th>
                      </tr>
                    </thead>
                    <tbody>
                      {seriesData.map((series, i) => {
                        const point = (series.results || [])[selectedClientIdx];
                        return (
                          <tr key={i} style={{ borderBottom: `1px solid ${T.rowBorder}` }}>
                            <td style={{ padding: "5px 8px", color: getStyle(series.config_name, i).color, fontWeight: 600 }}>
                              {displayName(series.label)}
                            </td>
                            <td style={{ padding: "5px 8px", textAlign: "right",
                              color: point?.success_rate > 0.8 ? "#388e3c" : point?.success_rate > 0.5 ? "#e07b39" : "#c75050" }}>
                              {point ? `${(point.success_rate * 100).toFixed(1)}%` : "—"}
                            </td>
                            <td style={{ padding: "5px 8px", textAlign: "right", color: T.text }}>
                              {point?.amplification ? `${point.amplification.toFixed(2)}x` : "—"}
                            </td>
                            <td style={{ padding: "5px 8px", textAlign: "right", color: T.text }}>
                              {point?.goodput_rps != null ? `${point.goodput_rps.toFixed(1)}` : "—"}
                            </td>
                            <td style={{ padding: "5px 8px", textAlign: "right", color: T.text }}>
                              {point?.retry_efficiency != null ? `${(point.retry_efficiency * 100).toFixed(1)}%` : "—"}
                            </td>
                            <td style={{ padding: "5px 8px", textAlign: "right", color: T.text }}>
                              {point?.fairness_share_shift != null ? `${(point.fairness_share_shift * 100).toFixed(1)}%` : "—"}
                            </td>
                            <td style={{ padding: "5px 8px", textAlign: "right", color: T.text }}>
                              {point?.p99 ? `${point.p99.toFixed(0)}` : "—"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </>
            ) : (
              <div style={{
                background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20, minHeight: 400,
                display: "flex", alignItems: "center", justifyContent: "center",
              }}>
                <div style={{ textAlign: "center", color: T.faint, fontSize: 14 }}>
                  <p>Add configs to compare, then click "Run Scaling Study"</p>
                  <p style={{ fontSize: 12, marginTop: 10, maxWidth: 420 }}>
                    Each config represents a different retry strategy. The simulator runs each
                    at varying client counts (log scale) while keeping total RPS constant.
                    Compare success rate, amplification, goodput, and retry efficiency.
                  </p>
                </div>
              </div>
            )}

            <div style={{
              marginTop: 10, padding: "12px 16px",
              background: T.hintBg, border: `1px solid ${T.border}`, borderRadius: 6,
              fontSize: 12, color: T.dim, lineHeight: 1.7,
            }}>
              <strong style={{ color: T.muted }}>How it works:</strong> For each config, the simulator runs with N client replicas
              sharing the total RPS. X-axis is log-scaled. Metrics:
              <strong> Goodput</strong> = successful RPS,
              <strong> Amplification</strong> = total attempts / original requests,
              <strong> Retry Efficiency</strong> = fraction of retries that succeed,
              <strong> Fairness</strong> = max goodput share shift (0 = fair).
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
