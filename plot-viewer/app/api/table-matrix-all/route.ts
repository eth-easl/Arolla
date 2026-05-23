import { NextRequest, NextResponse } from "next/server";
import { listExperiments } from "@/lib/fs";
import {
  buildTableMatrix,
  compareTimestampId,
  mergeColumns,
  rowKey,
  type CellPayload,
} from "@/lib/tableMatrix";

export const dynamic = "force-dynamic";

type AllTableRow = {
  key: string;
  experiment: string;
  timestamp: string;
  cells: Record<string, CellPayload>;
};

export async function GET(req: NextRequest) {
  const root = req.nextUrl.searchParams.get("root");
  if (!root) {
    return NextResponse.json({ error: "root is required" }, { status: 400 });
  }
  try {
    const experiments = await listExperiments(root);
    const built = await Promise.all(
      experiments.map(async (experiment) => ({
        experiment,
        ...(await buildTableMatrix(root, experiment)),
      })),
    );

    const columns = mergeColumns(built);
    const rows: AllTableRow[] = [];

    for (const b of built) {
      for (const ts of b.timestamps) {
        const cells: Record<string, CellPayload> = {};
        for (const col of columns) {
          cells[col] = b.cells[ts]?.[col] ?? {
            present: false,
            symbol: null,
          };
        }
        rows.push({
          key: rowKey(root, b.experiment, ts),
          experiment: b.experiment,
          timestamp: ts,
          cells,
        });
      }
    }

    rows.sort((a, b) => {
      const ex = a.experiment.localeCompare(b.experiment);
      if (ex !== 0) return ex;
      return compareTimestampId(a.timestamp, b.timestamp);
    });

    return NextResponse.json({ columns, rows });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "failed";
    return NextResponse.json({ error: msg }, { status: 400 });
  }
}
