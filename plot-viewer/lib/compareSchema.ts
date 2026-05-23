export type Phases = {
  warmup_start_rel_s: number;
  prefault_start_rel_s: number;
  fault_start_rel_s: number;
  fault_end_rel_s: number;
  recovery_end_rel_s: number;
  cooldown_end_rel_s: number;
};

export type PolicySeries = {
  t_rel_s: number[];
  goodput_rps: number[];
  total_rps: number[];
  success_rate: (number | null)[];
  retry_amplification: (number | null)[];
  latency_p50_ms: (number | null)[];
  latency_p90_ms: (number | null)[];
  latency_p95_ms: (number | null)[];
  latency_p99_ms: (number | null)[];
  cart_pod_cpu_mcores: (number | null)[];
  cart_sidecar_cpu_mcores: (number | null)[];
  cart_pod_mem_mib: (number | null)[];
  cart_sidecar_mem_mib: (number | null)[];
  rl_pod_cpu_mcores: (number | null)[];
  rl_pod_mem_mib: (number | null)[];
  rl_action_percent: (number | null)[];
  rl_action_minRetryConcurrency: (number | null)[];
};

export type Timeseries = {
  scenario_label: string;
  policies: string[];
  phases: Phases;
  series: Record<string, PolicySeries>;
};

/** Metric definitions surfaced as charts on the /compare page. */
export const METRIC_PANELS = [
  { key: "goodput_rps",            title: "Goodput (req/s)",                  yLabel: "req/s" },
  { key: "success_rate",           title: "Success rate",                     yLabel: "fraction", yDomain: [0, 1] as [number, number] },
  { key: "total_rps",              title: "Total RPS (incl. retries)",        yLabel: "req/s" },
  { key: "retry_amplification",    title: "Retry amplification",              yLabel: "× original" },
  { key: "latency_p50_ms",         title: "Latency p50",                       yLabel: "ms" },
  { key: "latency_p90_ms",         title: "Latency p90",                       yLabel: "ms" },
  { key: "latency_p95_ms",         title: "Latency p95",                       yLabel: "ms" },
  { key: "latency_p99_ms",         title: "Latency p99",                       yLabel: "ms" },
  { key: "cart_pod_cpu_mcores",    title: "Cart pod CPU (server)",            yLabel: "mCPU" },
  { key: "cart_sidecar_cpu_mcores",title: "Cart sidecar CPU (istio-proxy)",   yLabel: "mCPU" },
  { key: "cart_pod_mem_mib",       title: "Cart pod memory",                  yLabel: "MiB" },
  { key: "rl_pod_cpu_mcores",      title: "RL pod CPU",                       yLabel: "mCPU" },
  { key: "rl_pod_mem_mib",         title: "RL pod memory",                    yLabel: "MiB" },
  { key: "rl_action_percent",      title: "RL action: retry budget %",        yLabel: "%" },
  { key: "rl_action_minRetryConcurrency", title: "RL action: minRetryConcurrency", yLabel: "n" },
] as const;

export type MetricPanel = (typeof METRIC_PANELS)[number];
