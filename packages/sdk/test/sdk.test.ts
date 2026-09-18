import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configure, track } from "../src/index";

type FetchCall = { url: string; init: RequestInit };

function mockFetch() {
  const calls: FetchCall[] = [];
  const fn = vi.fn((url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    return Promise.resolve(new Response(null, { status: 204 }));
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

function payloadOf(call: FetchCall) {
  return JSON.parse(String(call.init.body));
}

const originalEnv = { ...process.env };

beforeEach(() => {
  delete process.env.DO_NOT_TRACK;
  delete process.env.STATLESS_OPTOUT;
  delete process.env.STATLESS_TELEMETRY_URL;
  delete process.env.STATLESS_TELEMETRY_TOKEN;
  delete process.env.CI;
  delete process.env.GITHUB_ACTIONS;
  configure({ enabled: true });
});

afterEach(() => {
  vi.unstubAllGlobals();
  for (const key of Object.keys(process.env)) {
    if (!(key in originalEnv)) delete process.env[key];
  }
  Object.assign(process.env, originalEnv);
});

describe("track", () => {
  it("sends the documented payload", async () => {
    const { calls } = mockFetch();
    await track({ package: "my-cli", version: "1.2.3", command: "build", durationMs: 41.6 });

    expect(calls).toHaveLength(1);
    const payload = payloadOf(calls[0]!);
    expect(payload.package).toBe("my-cli");
    expect(payload.version).toBe("1.2.3");
    expect(payload.command).toBe("build");
    expect(payload.duration_ms).toBe(42);
    expect(typeof payload.node_major).toBe("number");
    expect(typeof payload.os).toBe("string");
    expect(typeof payload.is_ci).toBe("boolean");
  });

  it("posts to the configured endpoint with a timeout signal", async () => {
    const { calls } = mockFetch();
    await track({ package: "my-cli", version: "1.0.0", endpoint: "http://localhost:8000/v1/telemetry/ping" });

    expect(calls[0]!.url).toBe("http://localhost:8000/v1/telemetry/ping");
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.init.signal).toBeInstanceOf(AbortSignal);
  });

  it("sends the optional ingest token header", async () => {
    process.env.STATLESS_TELEMETRY_TOKEN = "shared-secret";
    const { calls } = mockFetch();
    await track({ package: "my-cli", version: "1.0.0" });
    expect((calls[0]!.init.headers as Record<string, string>)["x-statless-token"]).toBe(
      "shared-secret",
    );
  });

  it("omits optional fields when not provided", async () => {
    const { calls } = mockFetch();
    await track({ package: "my-cli", version: "1.0.0" });
    const payload = payloadOf(calls[0]!);
    expect("command" in payload).toBe(false);
    expect("duration_ms" in payload).toBe(false);
  });

  it("detects CI from the environment", async () => {
    process.env.GITHUB_ACTIONS = "true";
    const { calls } = mockFetch();
    await track({ package: "my-cli", version: "1.0.0" });
    expect(payloadOf(calls[0]!).is_ci).toBe(true);
  });

  it("does not send when DO_NOT_TRACK=1", async () => {
    process.env.DO_NOT_TRACK = "1";
    const { fn } = mockFetch();
    await track({ package: "my-cli", version: "1.0.0" });
    expect(fn).not.toHaveBeenCalled();
  });

  it("does not send when STATLESS_OPTOUT=1", async () => {
    process.env.STATLESS_OPTOUT = "1";
    const { fn } = mockFetch();
    await track({ package: "my-cli", version: "1.0.0" });
    expect(fn).not.toHaveBeenCalled();
  });

  it("does not send when disabled via configure", async () => {
    configure({ enabled: false });
    const { fn } = mockFetch();
    await track({ package: "my-cli", version: "1.0.0" });
    expect(fn).not.toHaveBeenCalled();
  });

  it("never rejects on network errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("ECONNREFUSED"))),
    );
    await expect(track({ package: "my-cli", version: "1.0.0" })).resolves.toBeUndefined();
  });

  it("never rejects when fetch throws synchronously", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => {
        throw new Error("boom");
      }),
    );
    await expect(track({ package: "my-cli", version: "1.0.0" })).resolves.toBeUndefined();
  });
});
