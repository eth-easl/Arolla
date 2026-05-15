import fs from "fs/promises";
import type { Dirent } from "fs";
import path from "path";

/** Safe segment: experiment / run directory names */
const SEG_RE = /^[\w.=-]+$/;

/** PDF or PNG filename */
const FILE_RE = /^[\w.-]+\.(pdf|png)$/i;

export function getPrototypeRoot(): string {
  return (
    process.env.OUTPUTS_PROTOTYPE_ROOT ||
    path.resolve(process.cwd(), "..", "outputs", "prototype")
  );
}

function assertSegment(name: string, label: string): void {
  if (!SEG_RE.test(name)) {
    throw new Error(`Invalid ${label}`);
  }
}

function assertRelativeRunPath(runPath: string): string[] {
  const parts = runPath.split("/");
  if (!parts.length || parts.some((p) => !p)) {
    throw new Error("Invalid run");
  }
  for (const p of parts) assertSegment(p, "run segment");
  return parts;
}

export function plotsDir(experiment: string, runId: string): string {
  assertSegment(experiment, "experiment");
  const runParts = assertRelativeRunPath(runId);
  return path.join(getPrototypeRoot(), experiment, ...runParts, "plots");
}

export async function assertUnderPrototype(resolvedPath: string): Promise<void> {
  const proto = path.resolve(getPrototypeRoot());
  const resolved = path.resolve(resolvedPath);
  if (!resolved.startsWith(proto + path.sep) && resolved !== proto) {
    throw new Error("Path escapes prototype outputs root");
  }
}

export async function listExperiments(): Promise<string[]> {
  const root = path.resolve(getPrototypeRoot());
  try {
    const names = await fs.readdir(root, { withFileTypes: true }).then((ents) =>
      ents
        .filter((d) => d.isDirectory() && !d.name.startsWith("_"))
        .map((d) => d.name),
    );
    return names.sort();
  } catch {
    return [];
  }
}

export async function listRuns(experiment: string): Promise<string[]> {
  assertSegment(experiment, "experiment");
  const expPath = path.join(getPrototypeRoot(), experiment);
  await assertUnderPrototype(expPath);
  const runs: string[] = [];

  async function walk(dir: string, relParts: string[]): Promise<void> {
    await assertUnderPrototype(dir);
    let ents: Dirent[];
    try {
      ents = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }

    if (ents.some((e) => e.isDirectory() && e.name === "plots")) {
      const plotsPath = path.join(dir, "plots");
      try {
        const names = await fs.readdir(plotsPath);
        if (names.some((n) => FILE_RE.test(n))) runs.push(relParts.join("/"));
      } catch {
        /* skip */
      }
      return;
    }

    for (const e of ents) {
      if (
        !e.isDirectory() ||
        !SEG_RE.test(e.name) ||
        e.name.startsWith("_")
      ) continue;
      await walk(path.join(dir, e.name), [...relParts, e.name]);
    }
  }

  await walk(expPath, []);
  return runs.sort();
}

export async function listPlotFiles(
  experiment: string,
  runId: string,
): Promise<string[]> {
  const dir = plotsDir(experiment, runId);
  await assertUnderPrototype(dir);
  let names: string[];
  try {
    names = await fs.readdir(dir);
  } catch {
    return [];
  }
  return names.filter((n) => FILE_RE.test(n)).sort();
}

export function validatePlotFilename(file: string): void {
  if (!FILE_RE.test(file)) throw new Error("Invalid plot file");
}

export function plotFilePath(experiment: string, runId: string, file: string): string {
  validatePlotFilename(file);
  const dir = plotsDir(experiment, runId);
  return path.join(dir, file);
}

/** Matches classify_runs.plot_symbol_for_label (+ / - / ~). */
function symbolFromClassificationLabel(label: string): string | undefined {
  if (label === "recovered") return "+";
  if (label === "metastable") return "-";
  if (label === "ambiguous") return "~";
  return "~";
}

/**
 * plot_rl_comparison.write_comparison_health emits { policies: { slug: {
 * label, classification, healthy } } } — no top-level `symbol`. The first
 * policy entry matches the first --policy (RL sweep) in run_rl_v1.sh.
 */
function symbolFromComparisonHealthJson(raw: unknown): string | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const j = raw as {
    symbol?: unknown;
    policies?: unknown;
  };
  if (typeof j.symbol === "string" && /^[~\-+=]$/.test(j.symbol)) return j.symbol;

  const pol = j.policies;
  if (!pol || typeof pol !== "object" || Array.isArray(pol)) return undefined;
  const first = Object.values(pol)[0] as { classification?: unknown } | undefined;
  if (!first || typeof first !== "object") return undefined;
  const cls = first.classification;
  if (typeof cls !== "string") return undefined;
  return symbolFromClassificationLabel(cls);
}

export async function readComparisonHealthSymbol(
  experiment: string,
  runId: string,
): Promise<string | undefined> {
  assertSegment(experiment, "experiment");
  const parts = assertRelativeRunPath(runId);
  const abs = path.join(
    getPrototypeRoot(),
    experiment,
    ...parts,
    "comparison-health.json",
  );
  await assertUnderPrototype(abs);
  try {
    const buf = await fs.readFile(abs, "utf8");
    return symbolFromComparisonHealthJson(JSON.parse(buf));
  } catch {
    return undefined;
  }
}

/** classify_runs.py emits + / - / ~ (recovered / metastable / unhealthy). */
export async function readClassificationSymbol(
  experiment: string,
  runId: string,
): Promise<string | undefined> {
  assertSegment(experiment, "experiment");
  const parts = assertRelativeRunPath(runId);
  const abs = path.join(
    getPrototypeRoot(),
    experiment,
    ...parts,
    "classification.json",
  );
  await assertUnderPrototype(abs);
  try {
    const buf = await fs.readFile(abs, "utf8");
    const j = JSON.parse(buf) as { symbol?: unknown };
    if (typeof j.symbol === "string" && /^[+\-~]$/.test(j.symbol)) return j.symbol;
    return undefined;
  } catch {
    return undefined;
  }
}

export async function readRunOutcomeSymbol(
  experiment: string,
  runId: string,
): Promise<string | undefined> {
  if (/^rb-rl-v/.test(experiment)) {
    const ch = await readComparisonHealthSymbol(experiment, runId);
    if (ch) return ch;
  }
  return readClassificationSymbol(experiment, runId);
}

/** First-level timestamp directories under an experiment (excludes _-prefixed dirs). */
export async function listTimestamps(experiment: string): Promise<string[]> {
  assertSegment(experiment, "experiment");
  const expPath = path.join(getPrototypeRoot(), experiment);
  await assertUnderPrototype(expPath);
  try {
    const ents = await fs.readdir(expPath, { withFileTypes: true });
    return ents
      .filter((d) => d.isDirectory() && SEG_RE.test(d.name) && !d.name.startsWith("_"))
      .map((d) => d.name);
  } catch {
    return [];
  }
}

/**
 * Scenario sub-directories within a timestamp that contain plot files.
 * Returns [] when plots/ lives directly inside the timestamp dir (no sub-scenarios).
 */
export async function listScenarios(
  experiment: string,
  timestamp: string,
): Promise<string[]> {
  assertSegment(experiment, "experiment");
  assertSegment(timestamp, "timestamp");
  const tsPath = path.join(getPrototypeRoot(), experiment, timestamp);
  await assertUnderPrototype(tsPath);
  try {
    const ents = await fs.readdir(tsPath, { withFileTypes: true });
    // Plots directly at timestamp level → no sub-scenarios
    if (ents.some((e) => e.isDirectory() && e.name === "plots")) {
      const plotsPath = path.join(tsPath, "plots");
      try {
        const names = await fs.readdir(plotsPath);
        if (names.some((n) => FILE_RE.test(n))) return [];
      } catch { /* fall through */ }
    }
    // Collect scenario dirs that contain a plots/ child with plot files
    const scenarios: string[] = [];
    for (const e of ents) {
      if (
        !e.isDirectory() ||
        !SEG_RE.test(e.name) ||
        e.name.startsWith("_") ||
        e.name === "heatmap"
      ) continue;
      const scenPath = path.join(tsPath, e.name);
      await assertUnderPrototype(scenPath);
      try {
        const subEnts = await fs.readdir(scenPath, { withFileTypes: true });
        if (subEnts.some((s) => s.isDirectory() && s.name === "plots")) {
          const plotsPath = path.join(scenPath, "plots");
          const names = await fs.readdir(plotsPath);
          if (names.some((n) => FILE_RE.test(n))) scenarios.push(e.name);
        }
      } catch { /* skip */ }
    }
    return scenarios.sort();
  } catch {
    return [];
  }
}

/**
 * Return path to the heatmap directory for a given experiment + timestamp.
 * Structure: <root>/<experiment>/<timestamp>/heatmap/
 * Pass the first segment of a run ID as `timestamp`.
 */
export function heatmapDir(experiment: string, timestamp: string): string {
  assertSegment(experiment, "experiment");
  assertSegment(timestamp, "timestamp");
  return path.join(getPrototypeRoot(), experiment, timestamp, "heatmap");
}

export async function listHeatmapFiles(
  experiment: string,
  timestamp: string,
): Promise<string[]> {
  const dir = heatmapDir(experiment, timestamp);
  await assertUnderPrototype(dir);
  let names: string[];
  try {
    names = await fs.readdir(dir);
  } catch {
    return [];
  }
  return names.filter((n) => FILE_RE.test(n)).sort();
}

export function heatmapFilePath(
  experiment: string,
  timestamp: string,
  file: string,
): string {
  validatePlotFilename(file);
  return path.join(heatmapDir(experiment, timestamp), file);
}
