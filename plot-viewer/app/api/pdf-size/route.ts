import { NextRequest, NextResponse } from "next/server";
import fs from "fs/promises";
import { extractLargestPageBox } from "@/lib/pdfMediaBox";
import {
  assertUnderRoot,
  plotFilePath,
} from "@/lib/fs";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const root = req.nextUrl.searchParams.get("root");
  const experiment = req.nextUrl.searchParams.get("experiment");
  const run = req.nextUrl.searchParams.get("run");
  const file = req.nextUrl.searchParams.get("file");
  if (!root) {
    return NextResponse.json({ error: "root is required" }, { status: 400 });
  }
  if (!experiment || !run || !file) {
    return NextResponse.json(
      { error: "experiment, run, and file are required" },
      { status: 400 },
    );
  }
  let abs: string;
  try {
    abs = plotFilePath(root, experiment, run, file);
  } catch {
    return NextResponse.json({ error: "invalid path" }, { status: 400 });
  }
  try {
    await assertUnderRoot(root, abs);
    const buf = await fs.readFile(abs);
    const dims = extractLargestPageBox(buf);
    if (!dims) {
      return NextResponse.json(
        { error: "could not read page dimensions" },
        { status: 422 },
      );
    }
    return NextResponse.json({
      width: dims.width,
      height: dims.height,
    });
  } catch {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
}
