import type { TelemetryPayload } from "./index";

/** Hard upper bound on the network round trip, in milliseconds. */
const TIMEOUT_MS = 500;

/**
 * Fire-and-forget POST to the collector.
 *
 * Bounded by `AbortSignal.timeout(500)` so a slow or unreachable collector can
 * never delay or crash the host CLI. The returned promise always resolves and
 * never rejects; set `keepalive` so the request survives a prompt process exit
 * where the runtime honors it.
 */
export function send(endpoint: string, payload: TelemetryPayload, token: string): Promise<void> {
  const doFetch = (globalThis as { fetch?: typeof fetch }).fetch;
  if (typeof doFetch !== "function") return Promise.resolve();

  const signal =
    typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
      ? AbortSignal.timeout(TIMEOUT_MS)
      : undefined;

  const headers: Record<string, string> = { "content-type": "application/json" };
  if (token) headers["x-statless-token"] = token;

  try {
    return doFetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal,
      keepalive: true,
    }).then(
      () => undefined,
      () => undefined,
    );
  } catch {
    return Promise.resolve();
  }
}
