"""FastAPI routes, rate limiting, and in-memory platform hashing.

Endpoints:
    POST /v1/telemetry/ping     Ingest one CLI / package execution ping (204 No Content)
    GET  /v1/stats/{package}    JSON aggregates for one package
    GET  /v1/overview           Per-package totals across the collector
    GET  /v1/export/{package}   NDJSON dump of raw pings (maintainer data export)
    DELETE /v1/packages/{package}  Erase pings for one package (maintainer purge, token-gated)
    GET  /privacy               Human-readable privacy notice
    GET  /healthz               Liveness probe
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Iterable, MutableMapping
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Receive, Scope, Send

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


def _no_store[T: Response](resp: T) -> T:
    """Attach the collector's hardening headers, preserving the concrete response type."""
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
        with suppress(asyncio.CancelledError):
            await task


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

    def __init__(self, app: ASGIApp, max_bytes: int = 4096) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        declared: int | None = None
        headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", [])
        for name, value in headers:
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
            chunk: bytes = message.get("body", b"")
            total += len(chunk)
            if total > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        sent = False

        async def replay() -> MutableMapping[str, Any]:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
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
    """Return the value when it is a sane YYYY-MM-DD, else None."""
    if not _DATE_RE.match(value):
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


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
    with suppress(asyncio.CancelledError):
        await task
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
async def healthz() -> dict[str, bool]:
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
        return _no_store(Response(status_code=429, headers={"Retry-After": "60"}))
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
    package: str,
    request: Request,
    response: Response,
    token: str = "",
    since: str = "",
    to: str = "",
) -> PackageStats | JSONResponse:
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
) -> OverviewResponse | JSONResponse:
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
    return OverviewResponse(packages=[PackageOverview(**p) for p in packages], count=len(packages))


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
) -> ErasureResult | JSONResponse:
    """Hard-delete every stored ping for one package (maintainer bulk purge).

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


def _render_privacy_html() -> str:
    s = get_settings()
    controller_name = html.escape(s.controller_name)
    controller_contact = (
        html.escape(s.controller_contact) if s.controller_contact else "Not configured by operator"
    )
    dpo = (
        html.escape(s.data_protection_officer)
        if s.data_protection_officer
        else "None designated"
    )
    legal_basis = html.escape(s.legal_basis)
    authority = (
        html.escape(s.supervisory_authority)
        if s.supervisory_authority
        else "Competent local Data Protection Authority"
    )
    retention = (
        f"{s.retention_days} days"
        if s.retention_days > 0
        else "Indefinite (operator-managed storage limitation)"
    )

    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '<meta name="robots" content="noindex, nofollow" />\n'
        "<title>statless-telemetry - privacy notice</title>\n"
        "<style>\n"
        "  :root { color-scheme: light dark; }\n"
        "  body { max-width: 44rem; margin: 3rem auto; padding: 0 1.25rem; line-height: 1.6;\n"
        '         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,\n'
        "         Helvetica, Arial, sans-serif; }\n"
        "  h1 { font-size: 1.5rem; } h2 { font-size: 1.1rem; margin-top: 1.75rem;\n"
        "         border-bottom: 1px solid rgba(127,127,127,.25); padding-bottom: .25rem; }\n"
        "  code { background: rgba(127,127,127,.18); padding: .1rem .3rem;\n"
        "         border-radius: .25rem; font-size: .9em; }\n"
        "  ul { padding-left: 1.25rem; }\n"
        "  .meta { background: rgba(127,127,127,.08); border-left: 3px solid #2563eb;\n"
        "         padding: .75rem 1rem; margin: 1rem 0; border-radius: 0 .25rem .25rem 0; }\n"
        "  .meta p { margin: .25rem 0; }\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>statless-telemetry privacy notice</h1>\n"
        "<p>This collector provides usage analytics for developer CLIs and npm packages\n"
        "(subcommand usage, version adoption, and platform distribution). It is engineered\n"
        "around strict data minimization and storage limitation.</p>\n"
        '<div class="meta">\n'
        f"  <p><strong>Data Controller:</strong> {controller_name}</p>\n"
        f"  <p><strong>Privacy Contact:</strong> {controller_contact}</p>\n"
        f"  <p><strong>Data Protection Officer:</strong> {dpo}</p>\n"
        f"  <p><strong>Documented Legal Basis:</strong> {legal_basis}</p>\n"
        f"  <p><strong>Retention Period:</strong> {retention}</p>\n"
        "</div>\n"
        "<h2>What is processed and stored</h2>\n"
        "<ul>\n"
        "  <li><strong>Package metadata:</strong> Package name, reported version,\n"
        "      subcommand name, and execution duration in milliseconds.</li>\n"
        "  <li><strong>Environment parameters:</strong> Node.js major version, operating system\n"
        "      identifier (e.g. <code>darwin</code>, <code>linux</code>), and a CI provider boolean"
        " flag.</li>\n"
        "  <li><strong>Timestamp &amp; Pseudonymous Hash:</strong> UTC timestamp and a"
        " pseudonymous <code>platform_hash</code>.</li>\n"
        "</ul>\n"
        "<h2>Pseudonymization and network IP handling</h2>\n"
        "<ul>\n"
        "  <li><strong>Raw IP addresses are never saved to disk or logs:</strong> When an HTTP ping"
        " is received,\n"
        "      the client IP address is processed in memory to compute an HMAC-SHA256 hash using an"
        " ephemeral salt.</li>\n"
        f"  <li><strong>Ephemeral 24-hour salt rotation:</strong> The hashing salt rotates every"
        f" {s.salt_rotate_hours:g} hours\n"
        "      and is never written to disk. Once rotated, past hashes cannot be correlated with"
        " new requests.\n"
        "      Within any rotation window, the hash serves strictly as a pseudonymous counter"
        " for unique installations.</li>\n"
        "  <li><strong>No local persistence or cookies:</strong> No tracking cookies, local storage"
        " entries, or device UUIDs are stored on the client machine.</li>\n"
        "  <li><strong>No sensitive context:</strong> No filesystem paths, repository URLs,"
        " environment variables,\n"
        "      command-line arguments, or execution outputs are ever collected or stored.</li>\n"
        "</ul>\n"
        "<h2>Retention and storage limitation (GDPR Art. 5(1)(e))</h2>\n"
        f"<p>Telemetry pings are retained for {retention}. An automated background pruning loop\n"
        "runs daily to permanently delete records exceeding this window.</p>\n"
        "<h2>Data subject rights and limitations (GDPR Art. 11, 15-21)</h2>\n"
        "<p>Because telemetry records are pseudonymous and intentionally disconnected from names,\n"
        "email addresses, user accounts, or persistent device IDs, the controller cannot directly\n"
        "identify which records belong to a specific person (pursuant to GDPR Art. 11).\n"
        "Consequently, individual access (Art. 15) and erasure (Art. 17) requests cannot be\n"
        "fulfilled without additional identifying telemetry information (e.g. exact timestamp,\n"
        "IP, and platform for that day).</p>\n"
        "<p>Package maintainers may export aggregate datasets using"
        " <code>/v1/export/&lt;package&gt;</code>\n"
        "or perform bulk package purges using <code>DELETE /v1/packages/&lt;package&gt;</code>\n"
        "(gated by the operator's stats token).</p>\n"
        "<h2>Right to object and opt out</h2>\n"
        "<p>Developers can opt out at any time on their machine. When either environment variable\n"
        "is present, the client SDK immediately terminates before initiating any network"
        " connection:</p>\n"
        "<pre><code>DO_NOT_TRACK=1\nSTATLESS_OPTOUT=1</code></pre>\n"
        "<p>Applications can also disable telemetry programmatically via\n"
        "<code>configure({ enabled: false })</code>.</p>\n"
        "<h2>Right to lodge a complaint (GDPR Art. 77)</h2>\n"
        f"<p>You have the right to lodge a complaint regarding data processing with your\n"
        f"competent supervisory authority ({authority}).</p>\n"
        "</body>\n"
        "</html>\n"
    )


@app.get("/privacy", include_in_schema=False)
async def privacy() -> Response:
    resp = _no_store(HTMLResponse(_render_privacy_html()))
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
