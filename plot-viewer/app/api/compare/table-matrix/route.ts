import { NextRequest, NextResponse } from "next/server";
import fs from "fs/promises";
import path from "path";
import { resolveOutputsRoot } from "@/lib/roots";
import { listExperiments } from "@/lib/fs";
import { sortRbRlV1RunIds } from "@/lib/rbRlSort";
import { compareTimestampId } from "@/lib/tableMatrix";
import {
  classifyPolicyFromSeries,
  comparePolicy,
} from "@/lib/healthFromTimeseries";
import type { Timeseries } from "@/lib/compareSchema";

export const dynamic = "force-dynamic";

/**
 * Live (policy @ timestamp) × scenario classification matrix for
 * `outputs/prototype-new`-style sweeps.
 *
 * Walks `<root>/<experiment>/<timestamp>/<scenario>/timeseries.json`,
 * runs `classifyPolicyFromSeries` on every policy series inside each
 * scenario, and pivots the result so each row is a single policy at a
 * single timestamp (across all scenarios in that timestamp's sweep).
 *
 * This is the prototype-new equivalent of `/api/table-matrix-all`, but
 * computed on the fly because prototype-new does not emit per-scenario
 * `comparison-health.json` / `classification.json` files.
 */

const SEG_RE = /^[\w.=-]+$/;

type CellPayload = {
  present: boolean;
  symbol: string | null;
  /** Human-readable reason; useful for `title=` tooltips on the client. */
  reason?: string | null;
};

type Row = {
  key: string;
  experiment: string;
  timestamp: string;
  policy: string;
  cells: Record<string, CellPayload>;
};

async function readTimeseriesIfPresent(file: string): Promise<Timeseries | null> {
  try {
    const buf = await fs.readFile(file, "utf8");
    return JSON.parse(buf) as Timeseries;
  } catch {
    return null;
  }
}

/** Returns scenario folders under <experiment>/<timestamp> that have a timeseries.json. */
async function listScenariosWithTimeseries(
  root: string,
  experiment: string,
  timestamp: string,
): Promise<string[]> {
  const base = path.join(resolveOutputsRoot(root), experiment, timestamp);
  let ents: { name: string; isDirectory: () => boolean }[];
  try {
    ents = await fs.readdir(base, { withFileTypes: true });
  } catch {
    return [];
  }
  const out: string[] = [];
  await Promise.all(
    ents.map(async (e) => {
      if (!e.isDirectory()) return;
      if (!SEG_RE.test(e.name)) return;
      if (!e.name.startsWith("rate_rps=")) return;
      try {
        const st = await fs.stat(path.join(base, e.name, "timeseries.json"));
        if (st.isFile()) out.push(e.name);
      } catch {
        /* skip */
      }
    }),
  );
  return out;
}

async function listTimestampsWithTimeseries(
  root: string,
  experiment: string,
): Promise<string[]> {
  const base = path.join(resolveOutputsRoot(root), experiment);
  let ents: { name: string; isDirectory: () => boolean }[];
  try {
    ents = await fs.readdir(base, { withFileTypes: true });
  } catch {
    return [];
  }
  const out: string[] = [];
  await Promise.all(
    ents.map(async (e) => {
      if (!e.isDirectory()) return;
      if (!SEG_RE.test(e.name)) return;
      if (e.name.startsWith("_") || e.name.startsWith(".")) return;
      const scenarios = await listScenariosWithTimeseries(root, experiment, e.name);
      if (scenarios.length) out.push(e.name);
    }),
  );
  return out;
}

type ScenarioBlock = {
  experiment: string;
  timestamp: string;
  scenario: string;
  /** policy → cell payload (only includes policies present in timeseries.json) */
  perPolicy: Record<string, CellPayload>;
};

async function buildScenarioBlock(
  root: string,
  experiment: string,
  timestamp: string,
  scenario: string,
): Promise<ScenarioBlock> {
  const file = path.join(
    resolveOutputsRoot(root),
    experiment,
    timestamp,
    scenario,
    "timeseries.json",
  );
  const ts = await readTimeseriesIfPresent(file);
  const perPolicy: Record<string, CellPayload> = {};
  if (ts) {
    for (const policy of ts.policies) {
      const health = classifyPolicyFromSeries(ts, policy);
      if (!health) continue;
      perPolicy[policy] = {
        present: true,
        symbol: health.symbol,
        reason: health.reason,
      };
    }
  }
  return { experiment, timestamp, scenario, perPolicy };
}

export async function GET(req: NextRequest) {
  const root = req.nextUrl.searchParams.get("root");
  if (!root) {
    return NextResponse.json({ error: "root is required" }, { status: 400 });
  }
  try {
    const experiments = await listExperiments(root);
    const blocks: ScenarioBlock[] = [];

    for (const experiment of experiments) {
      const timestamps = await listTimestampsWithTimeseries(root, experiment);
      for (const timestamp of timestamps) {
        const scenarios = await listScenariosWithTimeseries(
          root,
          experiment,
          timestamp,
        );
        const built = await Promise.all(
          scenarios.map((s) =>
            buildScenarioBlock(root, experiment, timestamp, s),
          ),
        );
        for (const b of built) blocks.push(b);
      }
    }

    // Columns: union of scenarios across all blocks, in canonical difficulty order.
    const scenarioSet = new Set<string>();
    for (const b of blocks) scenarioSet.add(b.scenario);
    const columns = sortRbRlV1RunIds([...scenarioSet]);

    // Rows: union of (experiment, timestamp, policy). One row per cell.
    const rowMap = new Map<string, Row>();
    for (const b of blocks) {
      for (const [policy, cell] of Object.entries(b.perPolicy)) {
        const key = `${root}::${b.experiment}::${b.timestamp}::${policy}`;
        let row = rowMap.get(key);
        if (!row) {
          const cells: Record<string, CellPayload> = {};
          for (const col of columns) {
            cells[col] = { present: false, symbol: null };
          }
          row = {
            key,
            experiment: b.experiment,
            timestamp: b.timestamp,
            policy,
            cells,
          };
          rowMap.set(key, row);
        }
        row.cells[b.scenario] = cell;
      }
    }

    const rows = [...rowMap.values()].sort((a, b) => {
      const ex = a.experiment.localeCompare(b.experiment);
      if (ex !== 0) return ex;
      const ts = compareTimestampId(a.timestamp, b.timestamp);
      if (ts !== 0) return ts;
      return comparePolicy(a.policy, b.policy);
    });

    return NextResponse.json({ columns, rows });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "failed";
    return NextResponse.json({ error: msg }, { status: 400 });
  }
}
