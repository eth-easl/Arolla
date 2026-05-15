import { NextRequest, NextResponse } from "next/server";
import { listScenarios, readRunOutcomeSymbol } from "@/lib/fs";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const experiment = req.nextUrl.searchParams.get("experiment");
  const timestamp = req.nextUrl.searchParams.get("timestamp");
  if (!experiment || !timestamp) {
    return NextResponse.json(
      { error: "experiment and timestamp are required" },
      { status: 400 },
    );
  }
  try {
    const scenarios = await listScenarios(experiment, timestamp);
    // Full run IDs: timestamp/scenario, or just timestamp when no sub-scenarios
    const runs = scenarios.length
      ? scenarios.map((s) => `${timestamp}/${s}`)
      : [timestamp];
    const entries = await Promise.all(
      runs.map(async (runId) => {
        const sym = await readRunOutcomeSymbol(experiment, runId);
        return [runId, sym ?? ""] as const;
      }),
    );
    const runSymbols = Object.fromEntries(entries.filter(([, s]) => s.length));
    return NextResponse.json({ runs, run_symbols: runSymbols });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "failed";
    return NextResponse.json({ error: msg }, { status: 400 });
  }
}
