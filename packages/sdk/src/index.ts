/**
 * statless-telemetry - zero-dependency usage telemetry for developer CLIs.
 *
 * ```ts
 * import { configure, track } from "statless-telemetry";
 * configure({ endpoint: "https://telemetry.example.com/v1/telemetry/ping" });
 * const started = Date.now();
 * // ... run the command ...
 * void track({ package: "mytool", version: "1.2.3", command: "build", durationMs: Date.now() - started });
 * ```
 *
 * Privacy: nothing is sent unless an endpoint is configured, when `DO_NOT_TRACK=1`
 * or `STATLESS_OPTOUT=1` is set, or when explicitly disabled. No arguments,
 * paths, or command output ever leave the machine.
 */

import { endpointOverride, ingestToken, isCI, isOptedOut, nodeMajor, osPlatform } from "./environment";
import { send } from "./transport";

export { isOptedOut } from "./environment";

/** The wire payload sent to `POST /v1/telemetry/ping`. */
export interface TelemetryPayload {
  /** npm package name - the telemetry key (scoped names allowed). */
  package: string;
  /** Package version that ran. */
  version: string;
  /** Subcommand or script name, e.g. "build". */
  command?: string;
  /** Execution duration in milliseconds. */
  duration_ms?: number;
  /** Node.js major version. */
  node_major: number;
  /** `process.platform` value. */
  os: string;
  /** True when a known CI provider was detected. */
  is_ci: boolean;
}

/** Options for a single {@link track} call. */
export interface TrackOptions {
  package: string;
  version: string;
  command?: string;
  durationMs?: number;
  /** Override the collector endpoint for this call only. */
  endpoint?: string;
}

/** Process-wide SDK configuration. */
export interface TelemetryConfig {
  /** Collector endpoint. Defaults to `STATLESS_TELEMETRY_URL` env var. */
  endpoint?: string;
  /** Set `false` to disable telemetry for the whole process. */
  enabled?: boolean;
}

/** Public hosted collector endpoint reference. */
export const DEFAULT_HOSTED_ENDPOINT = "https://telemetry.statless.dev/v1/telemetry/ping";

let endpoint = endpointOverride();
let enabled = true;

/** Adjust the endpoint or turn telemetry off for the whole process. */
export function configure(config: TelemetryConfig): void {
  if (typeof config.endpoint === "string") endpoint = config.endpoint;
  if (typeof config.enabled === "boolean") enabled = config.enabled;
}

/**
 * Returns true if telemetry is enabled, an endpoint is configured, and the
 * environment has not opted out (neither DO_NOT_TRACK=1 nor STATLESS_OPTOUT=1).
 */
export function isTelemetryActive(customEndpoint?: string): boolean {
  if (!enabled || isOptedOut()) return false;
  return Boolean(customEndpoint || endpoint || endpointOverride());
}

/**
 * Record one CLI / package execution.
 *
 * Returns a promise that always resolves (never rejects), so callers can
 * `await track(...)` before exit, or fire it and forget. No-ops without any
 * network activity when telemetry is disabled, unconfigured, or the developer opted out.
 */
export function track(options: TrackOptions): Promise<void> {
  const targetEndpoint = options.endpoint || endpoint || endpointOverride();
  if (!enabled || !targetEndpoint || isOptedOut()) return Promise.resolve();

  const payload: TelemetryPayload = {
    package: options.package,
    version: options.version,
    node_major: nodeMajor(),
    os: osPlatform(),
    is_ci: isCI(),
  };
  if (options.command) payload.command = options.command;
  if (typeof options.durationMs === "number") {
    payload.duration_ms = Math.max(0, Math.round(options.durationMs));
  }

  return send(targetEndpoint, payload, ingestToken());
}
