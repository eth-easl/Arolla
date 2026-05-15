import {
  listTimestamps,
  listScenarios,
  readRunOutcomeSymbol,
} from "@/lib/fs";
import { sortRbRlV1RunIds } from "@/lib/rbRlSort";

export type CellPayload = { present: boolean; symbol: string | null };

/** Non-ignored timestamps first (newest-first by string sort), ignored at bottom. */
export function sortTimestampIds(ts: string[]): string[] {
  return [...ts].sort(compareTimestampId);
}

export function compareTimestampId(a: string, b: string): number {
  const aIgn = a.startsWith("ignored-");
  const bIgn = b.startsWith("ignored-");
  if (aIgn !== bIgn) return aIgn ? 1 : -1;
  return b.localeCompare(a);
}

export function orderColumns(cols: string[]): string[] {
  const hasEmpty = cols.includes("");
  const rest = cols.filter((c) => c !== "");
  const sorted =
    rest.length && rest[0].includes("=")
      ? sortRbRlV1RunIds(rest)
      : [...rest].sort((a, b) => a.localeCompare(b));
  return hasEmpty ? ["", ...sorted] : sorted;
}

export async function buildTableMatrix(experiment: string): Promise<{
  timestamps: string[];
  columns: string[];
  cells: Record<string, Record<string, CellPayload>>;
}> {
  const timestamps = sortTimestampIds(await listTimestamps(experiment));
  const colSet = new Set<string>();

  const perTs = await Promise.all(
    timestamps.map(async (ts) => {
      const scenarios = await listScenarios(experiment, ts);
      if (!scenarios.length) colSet.add("");
      else for (const s of scenarios) colSet.add(s);
      return { ts, scenarios };
    }),
  );

  const columns = orderColumns([...colSet]);

  const cells: Record<string, Record<string, CellPayload>> = {};
  await Promise.all(
    perTs.map(async ({ ts, scenarios }) => {
      const row: Record<string, CellPayload> = {};
      const scenSet = new Set(scenarios);
      const isFlat = scenarios.length === 0;

      await Promise.all(
        columns.map(async (col) => {
          if (col === "") {
            if (!isFlat) {
              row[col] = { present: false, symbol: null };
              return;
            }
            const sym = await readRunOutcomeSymbol(experiment, ts);
            row[col] = { present: true, symbol: sym ?? null };
            return;
          }
          if (!scenSet.has(col)) {
            row[col] = { present: false, symbol: null };
          } else {
            const runId = `${ts}/${col}`;
            const sym = await readRunOutcomeSymbol(experiment, runId);
            row[col] = { present: true, symbol: sym ?? null };
          }
        }),
      );
      cells[ts] = row;
    }),
  );

  return { timestamps, columns, cells };
}

export function rowKey(experiment: string, timestamp: string): string {
  return `${experiment}::${timestamp}`;
}

export function mergeColumns(matrices: { columns: string[] }[]): string[] {
  const colSet = new Set<string>();
  for (const m of matrices) for (const c of m.columns) colSet.add(c);
  return orderColumns([...colSet]);
}
