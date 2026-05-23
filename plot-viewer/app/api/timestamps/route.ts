import { NextRequest, NextResponse } from "next/server";
import { listTimestamps } from "@/lib/fs";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const root = req.nextUrl.searchParams.get("root");
  const experiment = req.nextUrl.searchParams.get("experiment");
  if (!root) {
    return NextResponse.json({ error: "root is required" }, { status: 400 });
  }
  if (!experiment) {
    return NextResponse.json(
      { error: "experiment is required" },
      { status: 400 },
    );
  }
  try {
    const timestamps = await listTimestamps(root, experiment);
    return NextResponse.json({ timestamps });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "failed";
    return NextResponse.json({ error: msg }, { status: 400 });
  }
}
