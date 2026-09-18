/**
 * Runtime environment detection: OS, Node version, CI, and opt-outs.
 *
 * Everything reads `process.env` at call time so a CLI can flip an env var
 * after import (or replace `process.env` entirely) and still be honored.
 */

type EnvLike = Record<string, string | undefined>;

function env(): EnvLike {
  return typeof process !== "undefined" && process.env ? process.env : {};
}

// Generic + common provider CI signals. Kept as a comma-joined string so the
// minified bundle stays tiny (a split at runtime is cheaper than an array).
const CI_KEYS =
  "CI,CONTINUOUS_INTEGRATION,GITHUB_ACTIONS,GITLAB_CI,CIRCLECI,TRAVIS,JENKINS_URL,BUILDKITE,TF_BUILD,APPVEYOR,CODEBUILD_BUILD_ID,VERCEL,NETLIFY,TEAMCITY_VERSION,BUILD_NUMBER";

/** True when a known CI provider (or the generic CI flag) is present. */
export function isCI(): boolean {
  const e = env();
  const ci = e.CI;
  if (ci && ci !== "0" && ci.toLowerCase() !== "false") return true;
  for (const key of CI_KEYS.split(",")) {
    if (e[key]) return true;
  }
  return false;
}

/** True when the developer opted out via `DO_NOT_TRACK=1` or `STATLESS_OPTOUT=1`. */
export function isOptedOut(): boolean {
  const e = env();
  return e.DO_NOT_TRACK === "1" || e.STATLESS_OPTOUT === "1";
}

/** `process.platform` (e.g. "darwin", "linux", "win32"), or "unknown". */
export function osPlatform(): string {
  return (typeof process !== "undefined" && process.platform) || "unknown";
}

/** Node.js major version, or 0 when it cannot be determined. */
export function nodeMajor(): number {
  const version = typeof process !== "undefined" && process.versions ? process.versions.node : "";
  return parseInt(version, 10) || 0;
}

/** Optional shared ingest token for private (token-gated) collectors. */
export function ingestToken(): string {
  return env().STATLESS_TELEMETRY_TOKEN || "";
}

/** Collector endpoint override, or "" when unset. */
export function endpointOverride(): string {
  return env().STATLESS_TELEMETRY_URL || "";
}
