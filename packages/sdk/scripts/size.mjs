import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const BUDGET_BYTES = 2048;

let failed = false;
for (const file of ["dist/index.js", "dist/index.cjs"]) {
  const gzipped = gzipSync(readFileSync(join(root, file))).length;
  const ok = gzipped <= BUDGET_BYTES;
  if (!ok) failed = true;
  console.log(`${ok ? "ok  " : "OVER"}  ${file}  ${gzipped} B gzipped (budget ${BUDGET_BYTES} B)`);
}

if (failed) {
  console.error("\nBundle exceeds the 2KB gzipped budget. Trim src/ before shipping.");
  process.exit(1);
}
