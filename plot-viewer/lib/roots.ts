import path from "path";

const DEFAULT_ROOTS = ["prototype", "prototype-new"] as const;

/** Returns the configured outputs roots in display order. */
export function listOutputsRoots(): string[] {
  const env = process.env.OUTPUTS_ROOTS;
  if (env && env.trim()) {
    return env.split(",").map((s) => s.trim()).filter(Boolean);
  }
  return [...DEFAULT_ROOTS];
}

const ROOT_NAME_RE = /^[a-zA-Z0-9._-]+$/;

/** Absolute path for a configured root name. Throws on unknown / unsafe names. */
export function resolveOutputsRoot(name: string): string {
  if (!ROOT_NAME_RE.test(name)) {
    throw new Error("Invalid root name");
  }
  if (!listOutputsRoots().includes(name)) {
    throw new Error(`Unknown root: ${name}`);
  }
  const envKey = `OUTPUTS_${name.toUpperCase().replace(/-/g, "_")}_ROOT`;
  const fromEnv = process.env[envKey];
  if (fromEnv) return path.resolve(fromEnv);
  return path.resolve(process.cwd(), "..", "outputs", name);
}
