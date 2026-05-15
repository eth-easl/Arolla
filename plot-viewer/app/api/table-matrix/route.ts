import { NextRequest, NextResponse } from "next/server";
import { buildTableMatrix } from "@/lib/tableMatrix";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const experiment = req.nextUrl.searchParams.get("experiment");
  if (!experiment) {
    return NextResponse.json(
      { error: "experiment is required" },
      { status: 400 },
    );
  }
  try {
    const data = await buildTableMatrix(experiment);
    return NextResponse.json(data);
  } catch (e) {
    const msg = e instanceof Error ? e.message : "failed";
    return NextResponse.json({ error: msg }, { status: 400 });
  }
}
