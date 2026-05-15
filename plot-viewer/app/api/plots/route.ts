import { NextRequest, NextResponse } from "next/server";
import { listPlotFiles } from "@/lib/fs";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const experiment = req.nextUrl.searchParams.get("experiment");
  const run = req.nextUrl.searchParams.get("run");
  if (!experiment || !run) {
    return NextResponse.json(
      { error: "experiment and run are required" },
      { status: 400 },
    );
  }
  try {
    const files = await listPlotFiles(experiment, run);
    return NextResponse.json({ files });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "failed";
    return NextResponse.json({ error: msg }, { status: 400 });
  }
}
