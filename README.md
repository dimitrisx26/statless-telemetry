<div align="center">

# statless-telemetry

**Know which subcommands people run, which versions are in the wild, and when - from a sub-2KB SDK with zero runtime dependencies.**

<p>
  <a href="https://github.com/statless/statless-telemetry/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/statless/statless-telemetry/ci.yml?branch=main&amp;style=flat-square&amp;logo=github&amp;label=CI" alt="CI status on main" />
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/collector-AGPLv3-2563EB?style=flat-square" alt="Collector license: AGPLv3" />
  </a>
  <a href="packages/sdk/LICENSE">
    <img src="https://img.shields.io/badge/SDK-MIT-16A34A?style=flat-square" alt="SDK license: MIT" />
  </a>
</p>

<p>
  <a href="https://www.npmjs.com/package/@statless/telemetry">
    <img src="https://img.shields.io/npm/v/@statless/telemetry?style=flat-square&amp;logo=npm&amp;logoColor=white" alt="npm version" />
  </a>
  <a href="packages/sdk">
    <img src="https://img.shields.io/badge/bundle-%3C2KB%20gzipped-8B5CF6?style=flat-square" alt="Bundle size under 2KB gzipped" />
  </a>
  <a href="collector/pyproject.toml">
    <img src="https://img.shields.io/badge/Python-3.13%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.13 or newer" />
  </a>
  <a href="packages/sdk/package.json">
    <img src="https://img.shields.io/badge/Node-18.17%2B-339933?style=flat-square&amp;logo=node.js&amp;logoColor=white" alt="Node.js 18.17 or newer" />
  </a>
</p>

[Report a bug](https://github.com/statless/statless-telemetry/issues) · [Request a feature](https://github.com/statless/statless-telemetry/issues) · [Privacy notice](#privacy--opt-out)

</div>

---

statless-telemetry is a self-hostable collector plus a tiny client SDK for **developer CLI usage**, **`npx` script executions**, and **npm package version adoption**. The SDK is one fire-and-forget `POST`; the collector is a single FastAPI service that stores pings in SQLite (PostgreSQL optional) and exposes JSON aggregates.

**Stack:** SDK MIT · collector AGPLv3 · TypeScript (ESM + CJS via tsup) · Python 3.13+ · FastAPI 0.115+ · SQLite (default) / PostgreSQL

## Why statless-telemetry

- **Tiny and dependency-free** - the SDK ships under 2KB gzipped with no runtime dependencies and no `postinstall` hooks
- **Never breaks the host CLI** - native `fetch` bounded by `AbortSignal.timeout(500)`, `keepalive`, and fail-silent error handling
- **Privacy flags honored automatically** - no request leaves the machine when `DO_NOT_TRACK=1` or `STATLESS_OPTOUT=1`
- **Safe by default** - SDK remains dormant until an endpoint is configured; no surprise network egress
- **Command-level insight** - not just download counts: package, version, subcommand, duration, Node major, OS, and a CI yes/no
- **Pseudonymous uniques** - the client IP is HMAC-hashed with a rotating in-memory salt and never written to disk
- **One-command self-hosting** - `docker compose up -d --build`; SQLite by default, PostgreSQL when you outgrow it

> Telemetry counts *recorded pings*, not guaranteed human usage. Offline machines, blocked egress, air-gapped CI, and opted-out developers are invisible by design. See [privacy & opt-out](#privacy--opt-out) and [legal & regulatory compliance](#legal--regulatory-compliance).

---

## Table of contents

- [Repository layout](#repository-layout)
- [How it compares](#how-it-compares)
- [Quick start](#quick-start)
- [Commander.js integration](#commanderjs-integration)
- [Yargs integration](#yargs-integration)
- [Payload reference](#payload-reference)
- [Expressing duration](#expressing-duration)
- [Privacy & opt-out](#privacy--opt-out)
- [Legal & regulatory compliance](#legal--regulatory-compliance)
- [Endpoints](#endpoints)
- [Configuration](#configuration)
- [Scaling notes](#scaling-notes)
- [Local development](#local-development)
- [Publishing](#publishing)
- [License](#license)

---

## Repository layout

```text
statless-telemetry/
├── .github/workflows/       # ci.yml (collector + SDK) and publish.yml (npm)
├── collector/               # FastAPI ingest + storage (AGPLv3)
│   ├── app/
│   │   ├── config.py        # pydantic-settings, env-driven
│   │   ├── main.py          # POST /v1/telemetry/ping + stats API
│   │   ├── models.py        # Pydantic v2 schemas
│   │   └── storage.py       # async SQLAlchemy persistence
│   ├── tests/
│   ├── Dockerfile
│   └── docker-compose.yml
├── packages/
│   └── sdk/                 # zero-dependency client SDK (MIT)
│       ├── src/index.ts     # public interface
│       ├── src/environment.ts # OS, Node runtime, CI, opt-out detection
│       └── src/transport.ts # native fetch with AbortSignal.timeout
├── LICENSE                  # AGPLv3 (collector)
└── README.md
```

---

## How it compares

statless-telemetry answers a narrower question than a product-analytics suite: **which commands and versions are actually running out there?** It is built for maintainers who want that signal without shipping a large SDK or a third-party tracker.

| | statless-telemetry | Scarf | npm download counts | Plausible / Umami |
|---|---|---|---|---|
| Granularity | **Command + version + duration + env** | Install / docs events | Version downloads only | Pageviews / events |
| Client footprint | **<2KB, zero deps, no install script** | Gateway/redirect or snippet | None | JS snippet |
| Self-hosted | **One container + SQLite** (Postgres optional) | SaaS-leaning | N/A (npm stats) | Postgres/ClickHouse required |
| Persistent identifiers | **None across 24h windows** (rotating IP hash only) | Account/company graph | npm account | Cookie/ID optional |
| Command-level data | **Yes** | No | No | No |
| License | **SDK MIT / collector AGPLv3** | Proprietary | N/A | AGPLv3 / MIT |

> Competitor capabilities change; verify against each project's docs. Last reviewed 2026-09.

**Not a fit if** you need funnels, session replay, crash reporting, or a hosted dashboard - pair this with your existing analytics for that. statless-telemetry deliberately trades features for a tiny footprint and a clear privacy story.

---

## Quick start

### 1 · Run the collector

```bash
git clone https://github.com/statless/statless-telemetry.git
cd statless-telemetry/collector
docker compose up -d --build
curl http://localhost:8000/healthz   # {"ok":true}
```

On Linux, run as your host UID/GID so the non-root container can write to `./data`:

```bash
STATLESS_UID=$(id -u) STATLESS_GID=$(id -g) docker compose up -d --build
```

### 2 · Install the SDK

```bash
npm install @statless/telemetry
```

### 3 · Send a ping

```ts
import { configure, track } from "@statless/telemetry";

// Point to your collector (or set STATLESS_TELEMETRY_URL in your environment)
configure({ endpoint: "http://localhost:8000/v1/telemetry/ping" });

const started = Date.now();
// ... run the command ...
await track({
  package: "mytool",
  version: "1.2.3", // read from your package.json
  command: "build",
  durationMs: Date.now() - started,
});
```

### 4 · Read the numbers

```bash
curl http://localhost:8000/v1/stats/mytool
```

```json
{
  "package": "mytool",
  "pings": 412,
  "uniques": 188,
  "ci": 274,
  "avg_duration_ms": 341.7,
  "max_duration_ms": 9210,
  "versions": [
    {"version": "1.3.0", "count": 221},
    {"version": "1.2.3", "count": 191}
  ],
  "commands": [
    {"name": "build", "count": 260},
    {"name": "test", "count": 152}
  ],
  "os": [{"name": "darwin", "count": 190}, {"name": "linux", "count": 222}],
  "node": [{"name": "22", "count": 300}, {"name": "20", "count": 112}],
  "daily": {
    "2026-09-17": {"pings": 96, "uniques": 51},
    "2026-09-18": {"pings": 118, "uniques": 63}
  }
}
```

Point the SDK at your collector with an env var:

```bash
export STATLESS_TELEMETRY_URL="https://telemetry.example.com/v1/telemetry/ping"
```

---

## Commander.js integration

```ts
#!/usr/bin/env node
import { Command } from "commander";
import { track } from "@statless/telemetry";
import pkg from "./package.json" with { type: "json" };

const started = Date.now();
const program = new Command();

program.name(pkg.name).version(pkg.version).command("build").action(async () => {
  // ... your build logic ...
});

program.hook("postAction", async (_thisCommand, actionCommand) => {
  await track({
    package: pkg.name,
    version: pkg.version,
    command: actionCommand.name(),
    durationMs: Date.now() - started,
  });
});

await program.parseAsync();
```

`postAction` runs once after the selected command completes, so a single hook covers every subcommand. `track` never rejects, so awaiting it is safe even if the collector is down.

## Yargs integration

```ts
#!/usr/bin/env node
import yargs from "yargs";
import { hideBin } from "yargs/helpers";
import { track } from "@statless/telemetry";
import pkg from "./package.json" with { type: "json" };

const started = Date.now();

await yargs(hideBin(process.argv))
  .command(
    "build",
    "build the project",
    () => {},
    async (argv) => {
      // ... your build logic ...
      await track({
        package: pkg.name,
        version: pkg.version,
        command: String(argv._[0]),
        durationMs: Date.now() - started,
      });
    },
  )
  .demandCommand(1)
  .parse();
```

For parsers with no post-command hook, wrap the whole invocation:

```ts
const started = Date.now();
let command = "root";
try {
  await main(); // your existing entrypoint
} finally {
  await track({ package: pkg.name, version: pkg.version, command, durationMs: Date.now() - started });
}
```

---

## Payload reference

`POST /v1/telemetry/ping` accepts a JSON body. The collector adds the UTC timestamp and a rotating `platform_hash`; everything else comes from the SDK.

| Field | Type | Required | Allowed | Source |
|---|---|---|---|---|
| `package` | string | yes | npm package name, scoped names allowed (`mytool` or `@scope/mytool`). Starts with a letter, digit, or `@`; then letters, digits, and `. _ - / @`, up to 128 characters | `TrackOptions.package` |
| `version` | string | yes | version string (`1.2.3`, `2.0.0-beta.1`). Starts with a letter or digit; then letters, digits, and `. _ + -`, up to 64 characters | `TrackOptions.version` |
| `command` | string | no | empty, or a short name like `build` or `test:watch`. Starts with a letter or digit; then letters, digits, spaces, and `. _ : -`, up to 64 characters | `TrackOptions.command` |
| `duration_ms` | integer | no | `0` to `86 400 000` (24 hours) | `TrackOptions.durationMs` |
| `node_major` | integer | no | `0` to `999` | detected from `process.versions.node` |
| `os` | string | no | lowercase platform id (`darwin`, `linux`, `win32`), up to 32 characters | detected from `process.platform` |
| `is_ci` | boolean | no | - | detected from common CI env vars |

Unknown fields are ignored, so a newer SDK can add fields without breaking an older collector. The request body is capped at 4 KB. Values that break the rules above are rejected with `422`; the exact patterns are defined in [`collector/app/models.py`](collector/app/models.py).

> **Do not send** secrets, tokens, repository names, filesystem paths, environment values, or command arguments. The SDK never collects these; keep it that way.

---

## Expressing duration

`durationMs` is wall-clock time for the whole invocation. For a single subcommand, measure around that command:

```ts
const start = performance.now();
await runBuild();
await track({ package: pkg.name, version: pkg.version, command: "build", durationMs: performance.now() - start });
```

`performance.now()` and `Date.now()` both work; the SDK rounds to the nearest millisecond and clamps negatives to `0`.

---

## Privacy & opt-out

**The short version:** no cookies, no persistent cross-day identifiers, no filesystem paths, no arguments, no command output. The client IP is never written to disk or logs—it is HMAC-SHA256 hashed in memory with an ephemeral salt that rotates every 24 hours, then combined with the reported platform. The SDK remains dormant until an endpoint is explicitly configured; requests are capped at 500ms and fail silently.

**Automatic opt-out.** The SDK returns immediately without network activity when either flag is set:

```bash
DO_NOT_TRACK=1        # cross-tool convention
STATLESS_OPTOUT=1     # explicit, statless-specific
```

A host application can also disable telemetry in code or query status:

```ts
import { configure, isOptedOut, isTelemetryActive } from "statless-telemetry";

configure({ enabled: false });

console.log(isOptedOut());        // true if DO_NOT_TRACK=1 or STATLESS_OPTOUT=1
console.log(isTelemetryActive()); // true only if enabled, endpoint set, and not opted out
```

The collector's full notice is served at `/privacy`. Operators who need to turn ingest off without breaking installed clients can set `TELEMETRY_ENABLED=false`: the endpoint still answers `204` but stores nothing.

---

## Legal & regulatory compliance

While statless-telemetry is engineered around data minimization and storage limitation, shipping telemetry in client-side developer tools involves legal obligations across international privacy jurisdictions.

### 1 · ePrivacy Directive (Art. 5(3)) & Member State Laws (TDDDG § 25, PECR)
- **Terminal equipment rule:** Article 5(3) of the ePrivacy Directive (Directive 2002/58/EC) restricts accessing information stored on a user's terminal equipment without prior consent, unless strictly necessary to deliver a requested service.
- **Scope:** Under European Data Protection Board (EDPB) Guidelines 2/2023, querying local operating system properties (`process.platform`) and runtime versions (`process.versions.node`) falls under the technical scope of Article 5(3). Because usage telemetry is for the tool maintainer's insight rather than strictly necessary for the command itself, European privacy laws generally require prior user notice or consent.
- **Recommended CLI pattern:** Provide a first-run notice or prompt in your CLI before enabling telemetry, or provide an interactive opt-in prompt (`Would you like to share anonymous usage stats? [y/N]`).

### 2 · GDPR (Regulation (EU) 2016/679)
- **Personal data:** Dynamic IP addresses received in transit constitute personal data (CJEU C-582/14 *Breyer*). Furthermore, while the daily rotating salt prevents multi-day profiling, the resulting `platform_hash` is a **pseudonymous identifier** within any 24-hour window (GDPR Recital 26).
- **Lawful basis (Art. 6):** Tool operators typically rely on either **affirmative consent** (Art. 6(1)(a)) or **legitimate interest** (Art. 6(1)(f)) backed by a documented Legitimate Interest Assessment (LIA) showing that minimal impact is balanced against product maintenance needs.
- **Transparency (Art. 13):** Maintainers must inform users at or before collection time (e.g. in installation docs, README, and CLI runtime banners) about what data is gathered, who operates the collector, and how to opt out.
- **Data subject rights (Art. 11, 15–21):** Because stored telemetry records are pseudonymous and lack account usernames, email addresses, or names, individual records cannot be identified without auxiliary data (GDPR Article 11). Maintainers should not claim bulk data exports or package deletions are individual DSAR endpoints.

### 3 · US Privacy Laws (CCPA / CPRA)
- Telemetry does not involve the "sale" or "sharing" of personal data for cross-context behavioral advertising. However, the California Consumer Privacy Act (CCPA) requires a **Notice at Collection** (Cal. Civ. Code § 1798.100) detailing the categories of collected metrics and their commercial/business purposes. Disclose this in your CLI documentation.

### 4 · Data Controller vs. Processor (Art. 28)
- **Self-hosted:** When you self-host the collector, you act as the data controller and your data remains entirely within your infrastructure.
- **Third-party collectors:** If you configure the SDK to point to a third-party hosted collector, that third party acts as a data processor. Ensure a valid Data Processing Agreement (DPA) and appropriate cross-border transfer mechanisms (Chapter V) are in place.

---

## Endpoints

| Method | Route | Notes |
|---|---|---|
| `POST` | `/v1/telemetry/ping` | Ingest one ping. **`204 No Content`** on success. `401` when `INGEST_TOKEN` is set and the token is wrong; `413` for bodies over 4 KB; `422` for invalid fields; `429` past the rate limit |
| `GET` | `/v1/stats/{package}` | JSON aggregates: `pings`, `uniques`, `ci`, `avg_duration_ms`, `max_duration_ms`, `versions`, `commands`, `os`, `node`, `daily`. Scoped package names work (`/v1/stats/@scope/name`). Filter with `?since=YYYY-MM-DD&to=YYYY-MM-DD` (inclusive UTC dates). Public by default - set `STATS_TOKEN` to require `?token=...` or the `X-Stats-Token` header |
| `GET` | `/v1/overview` | Per-package totals, busiest first (`package`, `pings`, `ci`, `uniques`, `last_ts`). `?prefix=` scopes to a package prefix. Same `STATS_TOKEN` gate |
| `GET` | `/v1/export/{package}` | Streamed NDJSON dump of raw pings for one package (portable maintainer export / backup). When stats are public (no `STATS_TOKEN`), the pseudonymous `platform_hash` column is omitted |
| `DELETE` | `/v1/packages/{package}` | Maintainer package data purge: hard-delete every stored ping for one package. Requires `STATS_TOKEN` to be configured AND supplied; with no token configured the endpoint always refuses (`403`) |
| `GET` | `/privacy` | Configurable GDPR Art. 13/14 privacy notice: controller details, legal basis, retention, opt-out, and supervisory authority |
| `GET` | `/robots.txt`, `/.well-known/security.txt` | Crawler off-switch (`Disallow: /`) and a disclosure template |
| `GET` | `/healthz` | Liveness probe |

`{package}` is an npm package name, e.g. `mytool` or `@scope/mytool` - the same rules as the `package` field in the payload reference above.

---

## Configuration

Collector settings are environment variables. With Docker Compose, add them to the service's `environment` block in [`collector/docker-compose.yml`](collector/docker-compose.yml), then recreate the container.

> **Before going public:** use HTTPS, persist and back up `./data`, set `STATS_TOKEN` and/or `INGEST_TOKEN` if the collector is reachable from the internet, set your `CONTROLLER_*` identity variables, and review retention.

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/telemetry.db` | Use `postgresql+asyncpg://user:pass@db:5432/telemetry` for Postgres |
| `SALT_ROTATE_HOURS` | `24` | Platform-hash salt rotation window (must be > 0) |
| `SERVER_SECRET` | *(empty = random)* | Derive the salt from `HMAC(secret, date+window)` so uniques survive restarts and match across replicas. Keep it in your secret manager |
| `TRUST_PROXY` | `false` | Honor `X-Forwarded-For` / `X-Real-IP` for platform hashing. Leave off unless behind a proxy that overwrites these headers. The rate limiter always uses the real socket IP |
| `RATE_LIMIT` | `120` | Pings per minute per client (0 disables). Over-limit requests get `429`, keyed on the socket IP |
| `RETENTION_DAYS` | `180` | Auto-delete pings older than this (GDPR Art. 5(1)(e) storage limitation). `0` disables automatic deletion |
| `STATS_TOKEN` | *(empty = public)* | When set, `/v1/stats`, `/v1/overview`, and `/v1/export` require `?token=<value>` or the `X-Stats-Token` header (header preferred - query strings end up in access logs). Also gates `DELETE /v1/packages/{package}` |
| `INGEST_TOKEN` | *(empty = open)* | When set, `/v1/telemetry/ping` requires `X-Statless-Token: <value>` or `Authorization: Bearer <value>`. The SDK sends it from `STATLESS_TELEMETRY_TOKEN` |
| `TELEMETRY_ENABLED` | `true` | When `false`, the collector accepts and silently drops pings (`204`) |
| `CONTROLLER_NAME` | `statless-telemetry operator` | Legal entity or maintainer name displayed in `/privacy` notice |
| `CONTROLLER_CONTACT` | *(empty)* | Contact email or URL for privacy inquiries in `/privacy` notice |
| `DATA_PROTECTION_OFFICER` | *(empty)* | Optional DPO email rendered in `/privacy` notice |
| `LEGAL_BASIS` | `Legitimate interest...` | Documented GDPR Art. 6 lawful basis rendered in `/privacy` notice |
| `SUPERVISORY_AUTHORITY` | *(empty)* | Name/URL of competent supervisory authority rendered in `/privacy` notice |
| `BASE_URL` | `http://localhost:8000` | Rendered into the index metadata |
| `SECURITY_CONTACT` | `mailto:security@YOUR-DOMAIN.example` | Contact line for `/.well-known/security.txt` - **replace before going public** |
| `SECURITY_POLICY` | *(empty = omitted)* | Optional `Policy:` URL for `/.well-known/security.txt` (e.g. your vulnerability disclosure policy or ToS) |
| `TELEMETRY_HOST` / `TELEMETRY_PORT` | `0.0.0.0` / `8000` | Bind for the `statless-telemetry` entrypoint (namespaced so stray `HOST`/`PORT` cannot hijack them) |
| `STATLESS_UID` / `STATLESS_GID` | `10001` | docker-compose only: run the container as your host user so the SQLite bind-mount is writable |

**SDK environment variables**

| Var | Default | Purpose |
|---|---|---|
| `STATLESS_TELEMETRY_URL` | *(empty = dormant)* | Ingest endpoint URL (e.g. your own `https://telemetry.example.com/v1/telemetry/ping`) |
| `STATLESS_TELEMETRY_TOKEN` | *(empty)* | Sent as `X-Statless-Token` when the collector sets `INGEST_TOKEN` |
| `DO_NOT_TRACK` | *(empty)* | `1` disables the SDK entirely |
| `STATLESS_OPTOUT` | *(empty)* | `1` disables the SDK entirely |

---

## Scaling notes

**Run a single worker.** The shipped `statless-telemetry` entrypoint uses one process, and that is deliberate: the rate limiter lives in process memory, so multiple workers would multiply rate limits independently, and SQLite is single-writer.

**If you outgrow it:** move to PostgreSQL, run N replicas, delegate rate limiting to your proxy, and set `SERVER_SECRET` so platform hashes derive deterministically from `HMAC(secret, date + window)` and stay consistent across replicas. Keep the secret in your secret manager, never in the repo.

---

## Local development

**Collector** (requires [uv](https://docs.astral.sh/uv/)):

```bash
cd collector
uv sync --extra dev          # creates .venv from uv.lock
uv run pytest -q             # tests
uv run ruff check app tests
uv run uvicorn app.main:app --reload
```

**SDK** (requires Node 18.17+):

```bash
cd packages/sdk
npm install
npm run lint                 # tsc --noEmit
npm run build                # dual ESM/CJS via tsup
npm run size                 # asserts the 2KB gzipped budget
npm test                     # vitest
```

---

## Publishing

The SDK publishes to npm from [`packages/sdk`](packages/sdk) via [`.github/workflows/publish.yml`](.github/workflows/publish.yml) when a `v*` tag is pushed:

```bash
# bump packages/sdk/package.json version, then
git tag v0.1.0
git push origin v0.1.0
```

The workflow runs typecheck, build, the bundle-size gate, and tests before `npm publish --provenance`. Add an `NPM_TOKEN` repository secret (automation token with publish rights) to enable it.

---

## License

- **SDK** ([`packages/sdk`](packages/sdk)): [MIT](packages/sdk/LICENSE) - drop it into any CLI, including closed-source ones.
- **Collector** (everything else): [AGPLv3](LICENSE) - run it as a service; if you modify and network-host it, share your changes.

See [`LICENSE`](LICENSE) for the full collector license text.
