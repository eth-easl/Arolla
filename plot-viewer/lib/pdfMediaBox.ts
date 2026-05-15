/**
 * Largest /MediaBox or /CropBox found in PDF bytes (PDF coordinates, usually points).
 * Heuristic scanner — sufficient for typical matplotlib / cairo PDFs without extra deps.
 */
export function extractLargestPageBox(buf: Buffer): {
  width: number;
  height: number;
} | null {
  const s = buf.toString("latin1");
  const re =
    /\/(?:MediaBox|CropBox)\s*\[\s*([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s*\]/g;
  let best: { width: number; height: number; area: number } | null = null;
  let m: RegExpExecArray | null;
  while ((m = re.exec(s)) !== null) {
    const x0 = Number(m[1]);
    const y0 = Number(m[2]);
    const x1 = Number(m[3]);
    const y1 = Number(m[4]);
    if (!Number.isFinite(x0 + y0 + x1 + y1)) continue;
    const w = Math.abs(x1 - x0);
    const h = Math.abs(y1 - y0);
    if (w < 16 || h < 16) continue;
    const area = w * h;
    if (!best || area > best.area) best = { width: w, height: h, area };
  }
  if (!best) return null;
  return { width: best.width, height: best.height };
}
