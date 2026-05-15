/** Scenario path parsing for rb-rl-v1 dropdown order (pure, client-safe). */

export function rawSweepParamValues(runId: string): Record<string, string> {
  const raw: Record<string, string> = {};
  const seen = new Set<string>();
  for (const seg of runId.split("/")) {
    for (const part of seg.split("__")) {
      const m =
        /^([^=]+)=(.+)$/.exec(part) ??
        /^([a-zA-Z][a-zA-Z0-9_]*)-([0-9].*)$/.exec(part);
      if (!m || seen.has(m[1])) continue;
      seen.add(m[1]);
      raw[m[1]] = m[2];
    }
  }
  return raw;
}

/** Increasing difficulty: lower RPS, shorter fault duration, lower fault rate first. */
export function rbRlScenarioDifficultyKey(runId: string): [number, number, number] {
  const r = rawSweepParamValues(runId);
  const rps = Number(r.rate_rps);
  const fd = Number(r.fault_duration);
  const frRaw = r.fault_rate ?? "";
  let fr = Number.NaN;
  if (/^\d+$/.test(frRaw)) fr = Number(frRaw);
  else {
    const m = /^cartservice-(\d+)pct$/i.exec(frRaw);
    if (m) fr = Number(m[1]);
  }
  return [
    Number.isFinite(rps) ? rps : Infinity,
    Number.isFinite(fd) ? fd : Infinity,
    Number.isFinite(fr) ? fr : Infinity,
  ];
}

export function cmpScenarioDifficulty(a: string, b: string): number {
  const [a1, a2, a3] = rbRlScenarioDifficultyKey(a);
  const [b1, b2, b3] = rbRlScenarioDifficultyKey(b);
  if (a1 !== b1) return a1 - b1;
  if (a2 !== b2) return a2 - b2;
  if (a3 !== b3) return a3 - b3;
  return a.localeCompare(b);
}

export function sortRbRlV1RunIds(ids: readonly string[]): string[] {
  return [...ids].sort(cmpScenarioDifficulty);
}
