import { NextRequest, NextResponse } from "next/server";
import fs from "fs/promises";
import path from "path";
import { resolveOutputsRoot } from "@/lib/roots";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const root = req.nextUrl.searchParams.get("root");
  const experiment = req.nextUrl.searchParams.get("experiment") || "full-sweep";
  if (!root) {
    return NextResponse.json({ error: "root is required" }, { status: 400 });
  }
  try {
    const base = path.join(resolveOutputsRoot(root), experiment);
    const ents = await fs.readdir(base, { withFileTypes: true });
    const timestamps: string[] = [];
    for (const e of ents) {
      if (!e.isDirectory() || e.name.startsWith("_") || e.name.startsWith(".")) continue;
      const tsDir = path.join(base, e.name);
      let hasAny = false;
      try {
        const scen = await fs.readdir(tsDir, { withFileTypes: true });
        for (const s of scen) {
          if (!s.isDirectory() || !s.name.startsWith("rate_rps=")) continue;
          try {
            const st = await fs.stat(path.join(tsDir, s.name, "timeseries.json"));
            if (st.isFile()) { hasAny = true; break; }
          } catch { /* skip */ }
        }
      } catch { /* skip */ }
      if (hasAny) timestamps.push(e.name);
    }
    return NextResponse.json({ timestamps: timestamps.sort().reverse() });
  } catch {
    return NextResponse.json({ timestamps: [] });
  }
}
