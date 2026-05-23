import { NextRequest, NextResponse } from "next/server";
import { listExperiments } from "@/lib/fs";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const root = req.nextUrl.searchParams.get("root");
  if (!root) {
    return NextResponse.json({ error: "root is required" }, { status: 400 });
  }
  try {
    const experiments = await listExperiments(root);
    return NextResponse.json({ experiments });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "failed";
    return NextResponse.json({ error: msg }, { status: 400 });
  }
}
