# statless-telemetry collector

Self-hostable, cookie-free usage telemetry for developer CLIs and npm packages.
A FastAPI service that stores one small row per execution ping - package,
version, subcommand, duration, Node major, OS, a CI flag, a timestamp, and a
rotating `platform_hash`. No raw IPs on disk, no cookies, no persistent cross-day identifiers.

## Run it

```bash
# Zero-config (SQLite file in ./data)
uv run statless-telemetry          # http://localhost:8000

# or with Docker Compose
docker compose up -d --build
```

Point `DATABASE_URL` at `postgresql+asyncpg://...` to use Postgres instead of
SQLite. The SDK sends to `POST /v1/telemetry/ping`.

## Endpoints

| Method | Route | Notes |
|---|---|---|
| `POST` | `/v1/telemetry/ping` | Ingest one ping (`204`); `401`/`413`/`422`/`429` on rejection |
| `GET` | `/v1/stats/{package}` | JSON aggregates, `?since=&to=` (inclusive UTC dates) |
| `GET` | `/v1/overview` | Per-package totals, busiest first; `?prefix=` |
| `GET` | `/v1/export/{package}` | Streamed NDJSON of raw pings (portable maintainer export) |
| `DELETE` | `/v1/packages/{package}` | Erase all pings for a package (maintainer purge) |
| `GET` | `/privacy` | Configurable GDPR Art. 13/14 privacy notice |
| `GET` | `/healthz` | Liveness probe |

`/v1/stats`, `/v1/overview`, `/v1/export`, and `DELETE` accept the stats token
via the `X-Stats-Token` header (preferred) or `?token=`.

## Privacy

- The client IP is HMAC-SHA256 hashed in memory with an ephemeral salt that
  rotates every `SALT_ROTATE_HOURS` (default 24h) and is never written to disk or logs.
- `platform_hash` is omitted from exports when stats are public (no `STATS_TOKEN`).
- Pings older than `RETENTION_DAYS` (default 180) are pruned daily (storage limitation).
- Opt-out is client-side: `DO_NOT_TRACK=1` or `STATLESS_OPTOUT=1` sends nothing.
- The `/privacy` route renders an operator-configurable notice detailing the controller,
  documented legal basis, and supervisory authority details.

## Configuration

See the main [README](https://github.com/statless/statless-telemetry#configuration)
for every environment variable (`DATABASE_URL`, `SERVER_SECRET`, `TRUST_PROXY`,
`RATE_LIMIT`, `RETENTION_DAYS`, `STATS_TOKEN`, `INGEST_TOKEN`,
`CONTROLLER_NAME`, `CONTROLLER_CONTACT`, `LEGAL_BASIS`, `TELEMETRY_ENABLED`, ...).

## Development

```bash
uv sync --extra dev
uv run ruff check app tests && uv run ruff format --check app tests
uv run pyright          # strict, app + tests
uv run pytest -q
```

## License

AGPL-3.0-or-later. The SDK (`packages/sdk`) is MIT.
