"""FastAPI routes, rate limiting, and in-memory platform hashing.

Endpoints:
    POST /v1/telemetry/ping     Ingest one CLI / package execution ping (204 No Content)
    GET  /v1/stats/{package}    JSON aggregates for one package
    GET  /v1/overview           Per-package totals across the collector
    GET  /v1/export/{package}   NDJSON dump of raw pings (Art. 15/20 data access)
    DELETE /v1/packages/{package}  Erase all pings for one package (Art. 17, STATS_TOKEN gated)
    GET  /privacy               Human-readable privacy notice
    GET  /healthz               Liveness probe
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from . import __version__
from . import storage as db
from .config import get_settings
from .models import (
    PACKAGE_NAME_PATTERN,
    ErasureResult,
    OverviewResponse,
    PackageOverview,
    PackageStats,
    TelemetryPing,
)

log = logging.getLogger(__name__)

PACKAGE_RE = re.compile(PACKAGE_NAME_PATTERN)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


def _valid_package(package: str) -> bool:
    return bool(PACKAGE_RE.match(package))


# --------------------------------------------------------------------------- #
# In-memory platform hashing                                                  #
# --------------------------------------------------------------------------- #
# The client's IP is never stored. It is combined with the reported platform
# and HMAC-SHA256 hashed under an ephemeral in-memory salt that rotates every
# SALT_ROTATE_HOURS (default 24h). After rotation, yesterday's hashes cannot be
# re-correlated - uniques are approximate per-salt-window, the intended
# trade-off for a cookie-free collector. The salt never touches disk or logs.

_salt: str | None = None
_rotation_task: asyncio.Task[None] | None = None


def _derive_salt() -> str:
    """Random salt, or a deterministic one when SERVER_SECRET is configured."""
    secret = get_settings().server_secret
    if not secret:
        return secrets.token_hex(32)
    hours = get_settings().salt_rotate_hours  # config validation keeps this > 0
    now = datetime.now(UTC)
    window_index = int(now.timestamp() // (hours * 3600.0))
    return hmac.new(
        secret.encode(), f"{now:%Y-%m-%d}:{window_index}".encode(), hashlib.sha256
    ).hexdigest()


def current_salt() -> str:
    """Return the active salt, generating one lazily on first use."""
    global _salt
    if _salt is None:
        _salt = _derive_salt()
    return _salt


def rotate_salt() -> str:
    """Force-rotate the salt immediately. Returns the new salt."""
    global _salt
    _salt = _derive_salt()
    return _salt


def platform_hash(ip: str, platform: str = "") -> str:
    """Pseudonymous platform hash: HMAC-SHA256(salt, ip|platform), truncated to 128 bits.

    Truncation keeps the SQLite/Postgres index small while remaining
    collision-resistant for approximate unique-install counting.
    """
    message = f"{ip.strip().lower()}|{platform}".encode()
    digest = hmac.new(current_salt().encode(), message, hashlib.sha256).hexdigest()
    return digest[:32]


async def _rotation_loop() -> None:
    interval = get_settings().salt_rotate_hours * 3600.0
    while True:
        await asyncio.sleep(interval)
        rotate_salt()


def _start_rotation_loop() -> None:
    global _rotation_task
    rotate_salt()  # ensure a salt exists before serving traffic
    if _rotation_task is None or _rotation_task.done():
        _rotation_task = asyncio.create_task(_rotation_loop())


async def _stop_rotation_loop() -> None:
    global _rotation_task
    task, _rotation_task = _rotation_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------- #
# Request helpers                                                             #
# --------------------------------------------------------------------------- #
def _socket_ip(request: Request) -> str:
    """Direct TCP peer - spoof-proof, so the rate limiter keys on this."""
    return request.client.host if request.client else "unknown"


def _client_ip(request: Request) -> str:
    """IP for hashing. Honors proxy headers ONLY when TRUST_PROXY=true."""
    settings = get_settings()
    if settings.trust_proxy:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    return request.client.host if request.client else ""


class RateLimiter:
    """Fixed-window counter per client, per minute. Single event loop, no locks."""

    def __init__(self, limit: int, max_ips: int = 10_000) -> None:
        if max_ips < 1:
            raise ValueError("max_ips must be positive")
        self.limit = limit
        self.max_ips = max_ips
        self._hits: dict[str, tuple[int, float]] = {}
        self._last_sweep = float("-inf")

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        if key not in self._hits and len(self._hits) >= self.max_ips:
            if now - self._last_sweep >= 60.0:
                self._last_sweep = now
                cutoff = now - 60.0
                self._hits = {k: v for k, v in self._hits.items() if v[1] > cutoff}
            if len(self._hits) >= self.max_ips:
                return False
        count, window_start = self._hits.get(key, (0, now))
        if now - window_start >= 60.0:
            count, window_start = 0, now
        if count >= self.limit:
            return False
        self._hits[key] = (count + 1, window_start)
        return True

    def reset(self) -> None:
        self._hits.clear()
        self._last_sweep = float("-inf")


_limiter = RateLimiter(get_settings().rate_limit)


class BodyLimitMiddleware:
    """Reject POST/PUT/PATCH bodies over `max_bytes` (413) before parsing; chunked-safe."""

    def __init__(self, app, max_bytes: int = 4096) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        declared: int | None = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:  # malformed header: treat as too large
                    declared = self.max_bytes + 1
                break
        if declared is not None and declared > self.max_bytes:
            await self._reject(scope, receive, send)
            return
        if declared is not None:
            await self.app(scope, receive, send)
            return
        # Chunked (no Content-Length): buffer bounded, then replay downstream.
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, scope, receive, send) -> None:
        resp = _no_store(JSONResponse({"ok": False, "error": "payload too large"}, status_code=413))
        await resp(scope, receive, send)


def _ingest_allowed(request: Request) -> bool:
    """True unless INGEST_TOKEN is set and the request presents a different token."""
    expected = get_settings().ingest_token
    if not expected:
        return True
    supplied = request.headers.get("x-statless-token", "").strip()
    if not supplied:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _stats_authorized(token: str) -> bool:
    """True unless STATS_TOKEN is set and the token does not match (public by default)."""
    expected = get_settings().stats_token
    return not expected or hmac.compare_digest(token, expected)


def _parse_date(value: str) -> str | None:
    if not _DATE_RE.match(value):
        return None
    try:
        return value if datetime.fromisoformat(value) else None
    except ValueError:
        return None


def _validated_dates(since: str, to: str) -> tuple[str, str] | JSONResponse:
    """Shared since/to validation. Returns (since, to) or a 400 JSONResponse."""
    parsed_since, parsed_to = _parse_date(since) or "", _parse_date(to) or ""
    if since and not parsed_since:
        return JSONResponse({"ok": False, "error": "bad since date"}, status_code=400)
    if to and not parsed_to:
        return JSONResponse({"ok": False, "error": "bad to date"}, status_code=400)
    if parsed_since and parsed_to and parsed_since > parsed_to:
        return JSONResponse({"ok": False, "error": "since after to"}, status_code=400)
    return parsed_since, parsed_to


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    _start_rotation_loop()
    retention = get_settings().retention_days

    async def retention_loop() -> None:
        if retention <= 0:
            return
        while True:
            deleted = await db.delete_old_events(retention)
            if deleted:
                log.info("retention: deleted %d pings older than %d days", deleted, retention)
            await asyncio.sleep(86_400)

    task = asyncio.create_task(retention_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await _stop_rotation_loop()
    await db.close_db()


app = FastAPI(title="statless-telemetry", version=__version__, lifespan=lifespan)
app.add_middleware(BodyLimitMiddleware)


@app.exception_handler(RequestValidationError)
async def _validation_no_store(request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default 422 shape plus no-store (echoed input is capped at 4 KB).
    return _no_store(JSONResponse(status_code=422, content=jsonable_encoder(exc.errors())))


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/", include_in_schema=False)
async def index() -> JSONResponse:
    s = get_settings()
    base = s.base_url.rstrip("/")
    return _no_store(
        JSONResponse(
            {
                "service": "statless-telemetry",
                "version": __version__,
                "privacy": "IPs never stored; HMAC-hashed with a rotating in-memory salt",
                "usage": {
                    "ping": f"POST {base}/v1/telemetry/ping",
                    "stats": f"{base}/v1/stats/YOUR-PACKAGE",
                    "overview": f"{base}/v1/overview",
                    "export": f"{base}/v1/export/YOUR-PACKAGE",
                },
            }
        )
    )


@app.post("/v1/telemetry/ping", status_code=204)
async def ping(ping: TelemetryPing, request: Request) -> Response:
    """Ingest one execution ping. Always answers with 204 and no body on success."""
    if not _ingest_allowed(request):
        return _no_store(Response(status_code=401))
    if not _limiter.allow(_socket_ip(request)):
        return _no_store(
            Response(status_code=429, headers={"Retry-After": "60"})
        )
    # Ingest can be turned off without breaking installed clients: accept and drop.
    if not get_settings().telemetry_enabled:
        return _no_store(Response(status_code=204))
    hashed = platform_hash(_client_ip(request), ping.os)
    try:
        await db.log_ping(
            package=ping.package,
            version=ping.version,
            command=ping.command,
            duration_ms=ping.duration_ms,
            node_major=ping.node_major,
            os=ping.os,
            is_ci=ping.is_ci,
            platform_hash=hashed,
        )
    except Exception:  # telemetry ingest must never surface an error to the client
        log.debug("ping ingest failed for %s", ping.package, exc_info=True)
    return _no_store(Response(status_code=204))


def _stats_token(request: Request, query_token: str) -> str:
    """Token for stats endpoints. Prefers the header (keeps secrets out of access logs).

    The `?token=` query parameter is kept for backwards compatibility.
    """
    return request.headers.get("x-stats-token", "").strip() or query_token


@app.get("/v1/stats/{package:path}", response_model=PackageStats)
async def stats(
    package: str, request: Request, response: Response, token: str = "", since: str = "", to: str = ""
) -> PackageStats:
    if not _valid_package(package):
        return _no_store(JSONResponse({"ok": False, "error": "invalid package"}, status_code=400))
    if not _stats_authorized(_stats_token(request, token)):
        return _no_store(JSONResponse({"ok": False, "error": "forbidden"}, status_code=403))
    dates = _validated_dates(since, to)
    if isinstance(dates, JSONResponse):
        return _no_store(dates)
    parsed_since, parsed_to = dates
    try:
        data = await db.get_package_stats(package, since=parsed_since or None, to=parsed_to or None)
    except SQLAlchemyError:
        log.exception("stats failed for %s", package)
        return _no_store(JSONResponse({"ok": False, "error": "stats unavailable"}, status_code=500))
    _no_store(response)
    return PackageStats(**data)


@app.get("/v1/overview", response_model=OverviewResponse)
async def overview(
    request: Request, response: Response, token: str = "", prefix: str = ""
) -> OverviewResponse:
    """Per-package totals, busiest first. Optional `prefix` scopes to a package prefix."""
    if prefix and not _valid_package(prefix):
        return _no_store(JSONResponse({"ok": False, "error": "invalid prefix"}, status_code=400))
    if not _stats_authorized(_stats_token(request, token)):
        return _no_store(JSONResponse({"ok": False, "error": "forbidden"}, status_code=403))
    try:
        packages = await db.get_overview(prefix or None)
    except SQLAlchemyError:
        log.exception("overview failed")
        return _no_store(
            JSONResponse({"ok": False, "error": "overview unavailable"}, status_code=500)
        )
    _no_store(response)
    return OverviewResponse(
        packages=[PackageOverview(**p) for p in packages], count=len(packages)
    )


@app.get("/v1/export/{package:path}", include_in_schema=False)
async def export(package: str, request: Request, token: str = "") -> Response:
    if not _valid_package(package):
        return _no_store(JSONResponse({"ok": False, "error": "invalid package"}, status_code=400))
    if not _stats_authorized(_stats_token(request, token)):
        return _no_store(JSONResponse({"ok": False, "error": "forbidden"}, status_code=403))
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", package)

    async def ndjson() -> AsyncIterator[bytes]:
        # Streams row-by-row so a huge package cannot buffer the whole table in memory.
        # When stats are public the pseudonymous platform_hash is omitted: it is
        # pseudonymous, but still personal data under GDPR - no need to publish it.
        include_hash = bool(get_settings().stats_token)
        try:
            async for row in db.iter_export(package, include_platform_hash=include_hash):
                yield (json.dumps(row, separators=(",", ":")) + "\n").encode()
        except SQLAlchemyError:
            log.exception("export failed for %s", package)

    resp = _no_store(StreamingResponse(ndjson(), media_type="application/x-ndjson"))
    resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}.ndjson"'
    return resp


@app.delete("/v1/packages/{package:path}", response_model=ErasureResult)
async def delete_package(
    package: str, request: Request, response: Response, token: str = ""
) -> ErasureResult:
    """GDPR Art. 17 erasure: hard-delete every stored ping for one package.

    Requires STATS_TOKEN to be configured AND supplied, so a public collector
    never lets strangers wipe other operators' data.
    """
    if not _valid_package(package):
        return _no_store(JSONResponse({"ok": False, "error": "invalid package"}, status_code=400))
    if not get_settings().stats_token:
        return _no_store(
            JSONResponse(
                {"ok": False, "error": "erasure requires STATS_TOKEN to be configured"},
                status_code=403,
            )
        )
    if not _stats_authorized(_stats_token(request, token)):
        return _no_store(JSONResponse({"ok": False, "error": "forbidden"}, status_code=403))
    try:
        deleted = await db.purge_package(package)
    except SQLAlchemyError:
        log.exception("erasure failed for %s", package)
        return _no_store(
            JSONResponse({"ok": False, "error": "erasure unavailable"}, status_code=500)
        )
    _no_store(response)
    return ErasureResult(ok=True, deleted=deleted)


_PRIVACY_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="robots" content="noindex, nofollow" />
<title>statless-telemetry - privacy notice</title>
<style>
  :root { color-scheme: light dark; }
  body { max-width: 42rem; margin: 3rem auto; padding: 0 1.25rem; line-height: 1.6;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  h1 { font-size: 1.5rem; } h2 { font-size: 1.05rem; margin-top: 2rem; }
  code { background: rgba(127,127,127,.18); padding: .1rem .3rem; border-radius: .25rem; }
</style>
</head>
<body>
<h1>statless-telemetry privacy notice</h1>
<p>This collector exists to tell maintainers which subcommands people run, which
package versions are in the wild, and when usage happens. It is deliberately
tiny and privacy-first.</p>
<h2>What is stored</h2>
<ul>
  <li>Package name, reported version, subcommand, and execution duration.</li>
  <li>Node.js major version, operating-system platform, and a CI yes/no flag.</li>
  <li>Timestamp (UTC) and a rotating <code>platform_hash</code>.</li>
</ul>
<h2>What is never stored</h2>
<ul>
  <li>No cookies, no device identifiers, no fingerprinting, no persistent IDs.</li>
  <li>No raw IP addresses. The IP is HMAC-SHA256 hashed with an ephemeral
      in-memory salt that rotates every 24 hours, then combined with the
      reported platform; the hash cannot be reversed.</li>
  <li>No filesystem paths, repository names, environment values, or arguments.</li>
  <li>No command output.</li>
</ul>
<h2>Opting out</h2>
<p>Set <code>DO_NOT_TRACK=1</code> or <code>STATLESS_OPTOUT=1</code> in your
environment. The SDK then returns before sending anything, so no request ever
leaves the machine.</p>
<h2>Retention</h2>
<p>Pings older than 180 days are deleted automatically. Operators may change the
window in either direction, including disabling automatic deletion entirely.</p>
<h2>Access and erasure</h2>
<p>The collector offers <code>/v1/export/&lt;package&gt;</code> (portable NDJSON dump,
GDPR Art. 15/20) and <code>DELETE /v1/packages/&lt;package&gt;</code> (hard deletion,
GDPR Art. 17) for package maintainers, gated by the operator's stats token.</p>
</body>
</html>
"""


@app.get("/privacy", include_in_schema=False)
async def privacy() -> Response:
    resp = _no_store(HTMLResponse(_PRIVACY_HTML))
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'"
    )
    return resp


@app.get("/robots.txt", include_in_schema=False)
async def robots() -> Response:
    return _no_store(Response("User-agent: *\nDisallow: /\n", media_type="text/plain"))


@app.get("/.well-known/security.txt", include_in_schema=False)
async def security_txt() -> Response:
    s = get_settings()
    lines = [f"Contact: {s.security_contact}", "Preferred-Languages: en"]
    if s.security_policy:
        lines.append(f"Policy: {s.security_policy}")
    return _no_store(Response("\n".join(lines) + "\n", media_type="text/plain; charset=utf-8"))


def run() -> None:  # `statless-telemetry` entrypoint
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port)
