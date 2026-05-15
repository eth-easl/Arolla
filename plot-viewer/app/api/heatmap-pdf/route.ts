import { NextRequest, NextResponse } from "next/server";
import fs from "fs/promises";
import { assertUnderPrototype, heatmapFilePath } from "@/lib/fs";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const experiment = req.nextUrl.searchParams.get("experiment");
  const run = req.nextUrl.searchParams.get("run");
  const file = req.nextUrl.searchParams.get("file");
  if (!experiment || !run || !file) {
    return NextResponse.json(
      { error: "experiment, run, and file are required" },
      { status: 400 },
    );
  }
  const timestamp = run.split("/")[0] ?? run;
  let abs: string;
  try {
    abs = heatmapFilePath(experiment, timestamp, file);
  } catch {
    return NextResponse.json({ error: "invalid path" }, { status: 400 });
  }
  try {
    await assertUnderPrototype(abs);
    const buf = await fs.readFile(abs);
    const contentType = abs.toLowerCase().endsWith(".png")
      ? "image/png"
      : "application/pdf";
    return new NextResponse(buf, {
      headers: {
        "Content-Type": contentType,
        "Cache-Control": "private, max-age=60",
      },
    });
  } catch {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
}
