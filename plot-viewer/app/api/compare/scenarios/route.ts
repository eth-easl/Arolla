import { NextRequest, NextResponse } from "next/server";
import fs from "fs/promises";
import path from "path";
import { resolveOutputsRoot } from "@/lib/roots";
import { sortRbRlV1RunIds } from "@/lib/rbRlSort";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const root = req.nextUrl.searchParams.get("root");
  const experiment = req.nextUrl.searchParams.get("experiment") || "full-sweep";
  const timestamp = req.nextUrl.searchParams.get("timestamp");
  if (!root || !timestamp) {
    return NextResponse.json(
      { error: "root and timestamp required" },
      { status: 400 },
    );
  }
  try {
    const base = path.join(resolveOutputsRoot(root), experiment, timestamp);
    const ents = await fs.readdir(base, { withFileTypes: true });
    const scenarios: string[] = [];
    for (const e of ents) {
      if (!e.isDirectory() || !e.name.startsWith("rate_rps=")) continue;
      const ts = path.join(base, e.name, "timeseries.json");
      try {
        await fs.access(ts);
        scenarios.push(e.name);
      } catch {
        /* skip */
      }
    }
    return NextResponse.json({ scenarios: sortRbRlV1RunIds(scenarios) });
  } catch {
    return NextResponse.json({ scenarios: [] });
  }
}
