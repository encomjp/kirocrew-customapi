#!/usr/bin/env node
// Gate vitest on Node >=22 (H7/bounds: declared engines node >=22).
// On older Node, skip gracefully (exit 0) so CI/local with stale Node doesn't red.
const major = parseInt(process.versions.node.split(".")[0], 10);
if (major < 22) {
  console.log(`skip: vitest requires Node >=22 (found ${process.versions.node}) — Node 22+ gate`);
  process.exit(0);
}
// Node >=22 — exec vitest with coverage, inheriting args.
import { spawnSync } from "node:child_process";
const result = spawnSync("npx", ["vitest", "run", "--coverage"], { stdio: "inherit" });
process.exit(result.status ?? 1);
