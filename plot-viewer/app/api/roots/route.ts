import { NextResponse } from "next/server";
import { listOutputsRoots } from "@/lib/roots";

export const dynamic = "force-dynamic";

export async function GET() {
  return NextResponse.json({ roots: listOutputsRoots() });
}
