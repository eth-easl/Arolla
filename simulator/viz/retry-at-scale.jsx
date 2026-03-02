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
// Strategy configs to compare
// ============================================================

const DEFAULT_CLIENT_COUNTS = [1, 5, 10, 50, 100];

// ============================================================
// SVG Charts
// ============================================================

function ScalingWallChart({ seriesData, clientCounts, selectedIdx, T, width = 640, height = 360 }) {
  if (!seriesData || seriesData.length === 0) return null;

  const padL = 60, padR = 20, padT = 25, padB = 55;
  const cW = width - padL - padR;
  const cH = height - padT - padB;

  const n = clientCounts.length;
  const xScale = (i) => padL + (i / Math.max(1, n - 1)) * cW;
  const yScale = (v) => padT + (1 - v) * cH;

  const colors = ["#4a86c8", "#e07b39", "#5ba05b", "#c75050", "#7a6cb2", "#c4853e", "#8b8b8b"];

  return (
    <svg width={width} height={height} style={{ overflow: "visible" }}>
      {[0, 0.25, 0.5, 0.75, 1].map((v) => (
        <g key={v}>
          <line x1={padL} y1={yScale(v)} x2={padL + cW} y2={yScale(v)} stroke={T.grid} strokeWidth={0.5} />
          <text x={padL - 8} y={yScale(v) + 4} textAnchor="end" fill={T.svgLabel} fontSize={11}
            fontFamily="'JetBrains Mono', monospace">{(v * 100).toFixed(0)}%</text>
        </g>
      ))}

      {selectedIdx >= 0 && selectedIdx < n && (
        <rect x={xScale(selectedIdx) - 5} y={padT} width={10} height={cH}
          fill={T.accent} opacity={0.06} rx={4} />
      )}

      {seriesData.map((series, si) => {
        const results = series.results || [];
        if (results.length === 0) return null;
        const points = results.map((d, i) => `${xScale(i)},${yScale(d.success_rate || 0)}`).join(" ");
        const color = colors[si % colors.length];
        return (
          <g key={si}>
            <polyline points={points} fill="none" stroke={color} strokeWidth={2.5} opacity={0.85} />
            {results.map((d, i) => (
              <circle key={i} cx={xScale(i)} cy={yScale(d.success_rate || 0)} r={3.5}
                fill={color} opacity={selectedIdx === i ? 1 : 0.7}
                stroke={selectedIdx === i ? T.text : "none"} strokeWidth={selectedIdx === i ? 1.5 : 0} />
            ))}
          </g>
        );
      })}

      {clientCounts.map((c, i) => (
        <text key={i} x={xScale(i)} y={height - 14} textAnchor="middle"
          fill={selectedIdx === i ? T.accent : T.svgLabel} fontSize={12}
          fontWeight={selectedIdx === i ? 600 : 400}
          fontFamily="'JetBrains Mono', monospace">{c}</text>
      ))}

      <text x={padL + cW / 2} y={height} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace">Number of Clients</text>
      <text x={8} y={padT + cH / 2} textAnchor="middle" fill={T.svgAxis} fontSize={12}
        fontFamily="'JetBrains Mono', monospace" transform={`rotate(-90, 8, ${padT + cH / 2})`}>
        Success Rate
      </text>

      <g transform={`translate(${padL + 8}, ${padT + 4})`}>
        {seriesData.map((series, i) => (
          <g key={i} transform={`translate(${(i % 3) * 190}, ${Math.floor(i / 3) * 18})`}>
            <rect x={0} y={-8} width={12} height={3} rx={1} fill={colors[i % colors.length]} opacity={0.9} />
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
      const saveRes = await apiSaveResults("scaling", { series: results, clientCounts, totalRps }, "scaling_wall");
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

      <div style={{ maxWidth: 1060, margin: "0 auto" }}>
        <div style={{ marginBottom: 24, borderBottom: `1px solid ${T.border}`, paddingBottom: 14 }}>
          <h1 style={{ fontSize: 24, fontWeight: 700, color: T.accent, margin: 0 }}>
            The Scaling Wall
          </h1>
          <p style={{ fontSize: 14, color: T.dim, margin: "6px 0 0" }}>
            Compare how different retry strategies perform as client count grows — using the real simulator
          </p>
        </div>

        <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
          {/* Left — Controls */}
          <div style={{ width: 300, flexShrink: 0 }}>
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
                        {c.split("/").pop().replace(".yaml", "")}
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

          {/* Right — Chart */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ background: T.panel, border: `1px solid ${T.border}`, borderRadius: 8, padding: 20, minHeight: 400 }}>
              <h3 style={{ fontSize: 16, color: T.text, margin: "0 0 14px", fontWeight: 600 }}>
                Success Rate vs. Client Count
              </h3>
              {seriesData.length > 0 ? (
                <div style={{ overflowX: "auto" }}>
                  <ScalingWallChart
                    seriesData={seriesData} clientCounts={clientCounts}
                    selectedIdx={selectedClientIdx} T={T} width={640} height={360}
                  />
                </div>
              ) : (
                <div style={{ textAlign: "center", padding: 70, color: T.faint, fontSize: 14 }}>
                  <p>Add configs to compare, then click "Run Scaling Study"</p>
                  <p style={{ fontSize: 12, marginTop: 10 }}>
                    Each config represents a different retry strategy. The simulator will run each
                    at varying client counts while keeping total RPS constant.
                  </p>
                </div>
              )}
            </div>

            {seriesData.length > 0 && (
              <div style={{
                marginTop: 12, background: T.panel, border: `1px solid ${T.border}`,
                borderRadius: 8, padding: 16, overflowX: "auto",
              }}>
                <h3 style={{ fontSize: 13, color: T.accent, margin: "0 0 10px", textTransform: "uppercase" }}>
                  At {selectedCount} Clients
                </h3>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead>
                    <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                      <th style={{ textAlign: "left", padding: "6px 10px", color: T.muted }}>Config</th>
                      <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Success Rate</th>
                      <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>Amplification</th>
                      <th style={{ textAlign: "right", padding: "6px 10px", color: T.muted }}>P99 (ms)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {seriesData.map((series, i) => {
                      const point = (series.results || [])[selectedClientIdx];
                      return (
                        <tr key={i} style={{ borderBottom: `1px solid ${T.rowBorder}` }}>
                          <td style={{ padding: "6px 10px", color: T.text }}>{series.label}</td>
                          <td style={{ padding: "6px 10px", textAlign: "right", color: point?.success_rate > 0.8 ? "#388e3c" : point?.success_rate > 0.5 ? "#e07b39" : "#c75050" }}>
                            {point ? `${(point.success_rate * 100).toFixed(1)}%` : "—"}
                          </td>
                          <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                            {point?.amplification ? `${point.amplification.toFixed(2)}x` : "—"}
                          </td>
                          <td style={{ padding: "6px 10px", textAlign: "right", color: T.text }}>
                            {point?.p99 ? `${point.p99.toFixed(0)}` : "—"}
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
              <strong style={{ color: T.muted }}>How it works:</strong> For each config, the simulator runs with N client replicas
              sharing the total RPS. The server adjusts <code style={{ color: T.accent }}>replicas</code> and <code style={{ color: T.accent }}>base_rps</code> per client.
              Start server: <code style={{ color: T.accent }}>cd simulator && python3 bin/viz_server.py</code>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
