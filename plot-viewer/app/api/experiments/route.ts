import { NextResponse } from "next/server";
import { listExperiments } from "@/lib/fs";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const experiments = await listExperiments();
    return NextResponse.json({ experiments });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "failed";
    return NextResponse.json({ error: msg }, { status: 400 });
  }
}
