import { NextRequest, NextResponse } from "next/server";
import fs from "fs/promises";
import path from "path";
import { resolveOutputsRoot } from "@/lib/roots";

export const dynamic = "force-dynamic";
const SEG = /^[\w.=-]+$/;

export async function GET(req: NextRequest) {
  const root = req.nextUrl.searchParams.get("root");
  const experiment = req.nextUrl.searchParams.get("experiment") || "full-sweep";
  const timestamp = req.nextUrl.searchParams.get("timestamp");
  const scenario = req.nextUrl.searchParams.get("scenario");
  if (!root || !timestamp || !scenario) {
    return NextResponse.json(
      { error: "root, timestamp, scenario required" },
      { status: 400 },
    );
  }
  for (const seg of [experiment, timestamp, scenario]) {
    if (!SEG.test(seg)) {
      return NextResponse.json({ error: "bad segment" }, { status: 400 });
    }
  }
  try {
    const file = path.join(
      resolveOutputsRoot(root),
      experiment,
      timestamp,
      scenario,
      "timeseries.json",
    );
    const buf = await fs.readFile(file, "utf8");
    return new NextResponse(buf, {
      headers: { "content-type": "application/json" },
    });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "fetch failed" },
      { status: 404 },
    );
  }
}
