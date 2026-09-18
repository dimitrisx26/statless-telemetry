# pyright: reportPrivateUsage=false
"""Smoke tests for the telemetry collector (SQLite per-test temp file)."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    # Isolate each test to a temp SQLite file (shared in-memory DBs don't survive pools).
    import os

    from app.config import get_settings

    get_settings.cache_clear()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    from app import storage

    await storage.close_db()
    await storage.init_db()

    from app.main import _limiter, app

    _limiter.reset()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await storage.close_db()
    get_settings.cache_clear()


def _ping(**overrides: Any) -> dict[str, Any]:
    payload = {
        "package": "my-cli",
        "version": "1.2.3",
        "command": "build",
        "duration_ms": 42,
        "node_major": 22,
        "os": "darwin",
        "is_ci": False,
    }
    payload.update(overrides)
    return payload


async def test_healthz(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


async def test_index_reports_usage(client: AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "statless-telemetry"
    assert body["usage"]["ping"].endswith("/v1/telemetry/ping")
    assert r.headers["Cache-Control"].startswith("no-store")


async def test_ping_returns_204_and_stores(client: AsyncClient) -> None:
    from app import storage

    r = await client.post("/v1/telemetry/ping", json=_ping())
    assert r.status_code == 204
    assert r.content == b""
    assert r.headers["Cache-Control"].startswith("no-store")
    assert await storage.count_pings("my-cli") == 1


async def test_ping_scoped_package_name(client: AsyncClient) -> None:
    from app import storage

    r = await client.post("/v1/telemetry/ping", json=_ping(package="@acme/tool"))
    assert r.status_code == 204
    assert await storage.count_pings("@acme/tool") == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"package": ""},
        {"package": "bad package name"},
        {"package": "x" * 200},
        {"version": ""},
        {"version": "1.0.0; rm -rf /"},
        {"duration_ms": -1},
        {"node_major": 10_000},
        {"os": "Darwin!!"},
    ],
)
async def test_ping_rejects_invalid_payloads(client: AsyncClient, bad: dict[str, Any]) -> None:
    r = await client.post("/v1/telemetry/ping", json=_ping(**bad))
    assert r.status_code == 422
    assert r.headers["Cache-Control"].startswith("no-store")


async def test_ping_ignores_unknown_fields(client: AsyncClient) -> None:
    r = await client.post("/v1/telemetry/ping", json=_ping(extra="nope", args=["--secret"]))
    assert r.status_code == 204  # pydantic ignores unknown fields; nothing retained


async def test_ping_rejects_oversize_body(client: AsyncClient) -> None:
    r = await client.post("/v1/telemetry/ping", content=b"x" * 100_000)
    assert r.status_code == 413
    assert r.headers["Cache-Control"].startswith("no-store")


async def test_ingest_token_gate(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("INGEST_TOKEN", "shared-secret")
    get_settings.cache_clear()
    try:
        assert (await client.post("/v1/telemetry/ping", json=_ping())).status_code == 401
        assert (
            await client.post(
                "/v1/telemetry/ping", json=_ping(), headers={"X-Statless-Token": "wrong"}
            )
        ).status_code == 401
        assert (
            await client.post(
                "/v1/telemetry/ping", json=_ping(), headers={"X-Statless-Token": "shared-secret"}
            )
        ).status_code == 204
        assert (
            await client.post(
                "/v1/telemetry/ping",
                json=_ping(),
                headers={"Authorization": "Bearer shared-secret"},
            )
        ).status_code == 204
    finally:
        get_settings.cache_clear()


async def test_telemetry_disabled_accepts_and_drops(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import storage
    from app.config import get_settings

    monkeypatch.setenv("TELEMETRY_ENABLED", "false")
    get_settings.cache_clear()
    try:
        r = await client.post("/v1/telemetry/ping", json=_ping())
        assert r.status_code == 204
        assert await storage.count_pings("my-cli") == 0
    finally:
        get_settings.cache_clear()


async def test_rate_limit_returns_429(client: AsyncClient) -> None:
    from app.main import _limiter

    old = _limiter.limit
    _limiter.limit = 2
    try:
        codes = [
            (await client.post("/v1/telemetry/ping", json=_ping())).status_code for _ in range(4)
        ]
    finally:
        _limiter.limit = old
    assert codes[:2] == [204, 204]
    assert codes[2:] == [429, 429]


async def test_stats_aggregates(client: AsyncClient) -> None:
    from datetime import UTC, datetime

    await client.post(
        "/v1/telemetry/ping", json=_ping(version="1.2.3", command="build", os="darwin")
    )
    await client.post(
        "/v1/telemetry/ping", json=_ping(version="1.3.0", command="test", os="linux", node_major=20)
    )
    await client.post(
        "/v1/telemetry/ping",
        json=_ping(version="1.3.0", command="build", os="linux", is_ci=True, duration_ms=120),
    )
    r = await client.get("/v1/stats/my-cli")
    assert r.status_code == 200
    data = r.json()
    assert data["pings"] == 3
    assert data["ci"] == 1
    assert data["uniques"] == 2  # two distinct os values -> two platform hashes
    assert data["avg_duration_ms"] == pytest.approx(68.0, abs=0.2)
    assert data["max_duration_ms"] == 120
    assert {v["version"]: v["count"] for v in data["versions"]} == {"1.3.0": 2, "1.2.3": 1}
    assert {c["name"]: c["count"] for c in data["commands"]} == {"build": 2, "test": 1}
    assert {o["name"]: o["count"] for o in data["os"]} == {"linux": 2, "darwin": 1}
    assert {n["name"]: n["count"] for n in data["node"]} == {"22": 2, "20": 1}
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert data["daily"][today]["pings"] == 3
    assert data["daily"][today]["uniques"] == 2


async def test_stats_scoped_package_via_path(client: AsyncClient) -> None:
    from app import storage

    await storage.log_ping(
        package="@acme/tool", version="1.0.0", command="run", platform_hash="a" * 32
    )
    r = await client.get("/v1/stats/@acme/tool")
    assert r.status_code == 200
    assert r.json()["package"] == "@acme/tool"
    assert r.json()["pings"] == 1


async def test_stats_rejects_bad_package(client: AsyncClient) -> None:
    assert (await client.get("/v1/stats/bad%20name")).status_code == 400


async def test_stats_token_gate(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("STATS_TOKEN", "shh")
    get_settings.cache_clear()
    try:
        assert (await client.get("/v1/stats/my-cli")).status_code == 403
        assert (await client.get("/v1/stats/my-cli?token=wrong")).status_code == 403
        assert (await client.get("/v1/stats/my-cli?token=shh")).status_code == 200
    finally:
        get_settings.cache_clear()


async def test_stats_date_range_filters(client: AsyncClient) -> None:
    from datetime import UTC, datetime, timedelta

    from app import storage

    await storage.log_ping(package="range-cli", version="1.0.0", platform_hash="a" * 32)
    async with storage.get_engine().begin() as conn:
        week_ago = datetime.now(UTC) - timedelta(days=7)
        await conn.execute(
            storage.Ping.__table__.update()
            .where(storage.Ping.package == "range-cli")
            .values(ts=week_ago)
        )
    await storage.log_ping(package="range-cli", version="1.0.1", platform_hash="b" * 32)

    data = (await client.get("/v1/stats/range-cli")).json()
    assert data["pings"] == 2
    week_ago_day = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert data["daily"][week_ago_day]["pings"] == 1
    assert data["daily"][today]["pings"] == 1

    since = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    recent = (await client.get(f"/v1/stats/range-cli?since={since}")).json()
    assert recent["pings"] == 1

    both = await client.get(f"/v1/stats/range-cli?since={today}&to={week_ago_day}")
    assert both.status_code == 400


async def test_stats_rejects_bad_dates(client: AsyncClient) -> None:
    assert (await client.get("/v1/stats/my-cli?since=not-a-date")).status_code == 400
    assert (await client.get("/v1/stats/my-cli?to=2026-13-40")).status_code == 400


async def test_stats_error_does_not_leak_exception(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    from app import storage

    async def boom(_package: str, since: str | None = None, to: str | None = None) -> None:
        raise SQLAlchemyError("host=db.internal user=secret")

    monkeypatch.setattr(storage, "get_package_stats", boom)
    r = await client.get("/v1/stats/my-cli")
    assert r.status_code == 500
    assert "secret" not in r.text


async def test_overview_lists_packages_and_scopes_by_prefix(client: AsyncClient) -> None:
    from app import storage

    await storage.log_ping(package="site-a", version="1.0.0", platform_hash="a" * 32)
    await storage.log_ping(package="site-a", version="1.0.0", platform_hash="b" * 32, is_ci=True)
    await storage.log_ping(package="site_a", version="1.0.0", platform_hash="c" * 32)
    await storage.log_ping(package="other-c", version="1.0.0", platform_hash="d" * 32)

    body = (await client.get("/v1/overview")).json()
    packages = {p["package"]: p for p in body["packages"]}
    assert body["count"] == 3
    assert packages["site-a"]["pings"] == 2
    assert packages["site-a"]["ci"] == 1
    assert packages["site-a"]["uniques"] == 2
    assert packages["site-a"]["last_ts"]

    # `_` is a LIKE wildcard: escaping must keep site_ from also matching site-a.
    scoped = (await client.get("/v1/overview?prefix=site_")).json()
    assert {p["package"] for p in scoped["packages"]} == {"site_a"}


async def test_overview_rejects_bad_prefix_and_gates_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    assert (await client.get("/v1/overview?prefix=bad prefix")).status_code == 400
    monkeypatch.setenv("STATS_TOKEN", "shh")
    get_settings.cache_clear()
    try:
        assert (await client.get("/v1/overview")).status_code == 403
        assert (await client.get("/v1/overview?token=shh")).status_code == 200
    finally:
        get_settings.cache_clear()


async def test_export_jsonl_gated_by_stats_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import storage
    from app.config import get_settings

    await storage.log_ping(
        package="exp-cli", version="1.0.0", command="build", os="linux", platform_hash="a" * 32
    )
    await storage.log_ping(package="other-cli", version="1.0.0", platform_hash="c" * 32)
    monkeypatch.setenv("STATS_TOKEN", "shh")
    get_settings.cache_clear()
    try:
        assert (await client.get("/v1/export/exp-cli")).status_code == 403
        r = await client.get("/v1/export/exp-cli?token=shh")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        rows = [json.loads(line) for line in r.text.strip().splitlines() if line]
        assert len(rows) == 1
        assert rows[0]["package"] == "exp-cli"
        assert "platform_hash" in rows[0]
        assert "127.0.0.1" not in r.text

        # Header auth must work too (keeps the token out of access logs).
        r = await client.get("/v1/export/exp-cli", headers={"X-Stats-Token": "shh"})
        assert r.status_code == 200
    finally:
        get_settings.cache_clear()


async def test_stats_accepts_token_header(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("STATS_TOKEN", "shh")
    get_settings.cache_clear()
    try:
        r = await client.get("/v1/stats/my-cli", headers={"X-Stats-Token": "shh"})
        assert r.status_code == 200
        r = await client.get("/v1/overview", headers={"X-Stats-Token": "shh"})
        assert r.status_code == 200
    finally:
        get_settings.cache_clear()


async def test_erasure_requires_stats_token(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import storage
    from app.config import get_settings

    await storage.log_ping(package="wipe-cli", version="1.0.0", platform_hash="a" * 32)

    # No STATS_TOKEN configured: nobody (not even with a token) may erase.
    assert (await client.delete("/v1/packages/wipe-cli")).status_code == 403
    assert await storage.count_pings("wipe-cli") == 1

    monkeypatch.setenv("STATS_TOKEN", "shh")
    get_settings.cache_clear()
    try:
        assert (await client.delete("/v1/packages/wipe-cli")).status_code == 403
        assert (await client.delete("/v1/packages/wipe-cli?token=wrong")).status_code == 403
        r = await client.delete("/v1/packages/wipe-cli", headers={"X-Stats-Token": "shh"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "deleted": 1}
        assert await storage.count_pings("wipe-cli") == 0
        # Erasing again is idempotent.
        r = await client.delete("/v1/packages/wipe-cli?token=shh")
        assert r.json() == {"ok": True, "deleted": 0}
    finally:
        get_settings.cache_clear()


async def test_erasure_rejects_bad_package(client: AsyncClient) -> None:
    assert (await client.delete("/v1/packages/bad%20name")).status_code == 400


async def test_public_export_omits_platform_hash(client: AsyncClient) -> None:
    from app import storage

    await storage.log_ping(package="pub-cli", version="1.0.0", platform_hash="a" * 32)
    r = await client.get("/v1/export/pub-cli")
    assert r.status_code == 200
    rows = [json.loads(line) for line in r.text.strip().splitlines() if line]
    assert len(rows) == 1
    assert "platform_hash" not in rows[0]


async def test_security_txt_is_configurable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("SECURITY_CONTACT", "mailto:sec@example.com")
    monkeypatch.setenv("SECURITY_POLICY", "https://example.com/security")
    get_settings.cache_clear()
    try:
        r = await client.get("/.well-known/security.txt")
        assert r.status_code == 200
        assert "Contact: mailto:sec@example.com" in r.text
        assert "Policy: https://example.com/security" in r.text
    finally:
        get_settings.cache_clear()


async def test_daily_buckets_stay_utc_when_tz_stored_naive(client: AsyncClient) -> None:
    # SQLite path: daily keys must remain plain UTC dates regardless of storage.
    from datetime import UTC, datetime

    from app import storage

    await storage.log_ping(package="tz-cli", version="1.0.0", platform_hash="a" * 32)
    data = (await client.get("/v1/stats/tz-cli")).json()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert list(data["daily"].keys()) == [today]
    assert data["daily"][today] == {"pings": 1, "uniques": 1}


async def test_retention_deletes_old_pings_only(client: AsyncClient) -> None:
    from datetime import UTC, datetime, timedelta

    from app import storage

    await storage.log_ping(package="old-cli", version="1.0.0", platform_hash="a" * 32)
    await storage.log_ping(package="new-cli", version="1.0.0", platform_hash="b" * 32)
    async with storage.get_engine().begin() as conn:
        old_cutoff = datetime.now(UTC) - timedelta(days=200)
        await conn.execute(
            storage.Ping.__table__.update()
            .where(storage.Ping.package == "old-cli")
            .values(ts=old_cutoff)
        )
    deleted = await storage.delete_old_events(180)
    assert deleted == 1
    assert await storage.count_pings("new-cli") == 1
    assert await storage.count_pings("old-cli") == 0


async def test_retention_zero_disables_and_purge_erases(client: AsyncClient) -> None:
    from app import storage

    await storage.log_ping(package="keep-cli", version="1.0.0", platform_hash="a" * 32)
    assert await storage.delete_old_events(0) == 0
    assert await storage.count_pings("keep-cli") == 1
    assert await storage.purge_package("keep-cli") == 1
    assert await storage.count_pings("keep-cli") == 0


def test_platform_hash_is_stable_within_epoch() -> None:
    from app.main import platform_hash

    assert platform_hash("1.2.3.4", "linux") == platform_hash("1.2.3.4", "linux")
    assert platform_hash("1.2.3.4", "linux") != platform_hash("1.2.3.4", "darwin")
    assert platform_hash("1.2.3.4", "linux") != platform_hash("5.6.7.8", "linux")
    assert len(platform_hash("1.2.3.4", "linux")) == 32


def test_secret_salt_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main

    monkeypatch.setenv("SERVER_SECRET", "topsecret")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert main._derive_salt() == main._derive_salt()
    finally:
        get_settings.cache_clear()


async def test_privacy_page_carries_locked_csp(client: AsyncClient) -> None:
    r = await client.get("/privacy")
    assert r.status_code == 200
    assert "DO_NOT_TRACK" in r.text
    assert (
        r.headers["Content-Security-Policy"]
        == "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'"
    )
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


async def test_robots_and_security_txt(client: AsyncClient) -> None:
    r = await client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /" in r.text
    assert r.headers["Cache-Control"].startswith("no-store")
    s = await client.get("/.well-known/security.txt")
    assert s.status_code == 200
    assert "Contact:" in s.text


async def test_rotation_loop_start_stop() -> None:
    from app import main

    main._start_rotation_loop()
    assert main.current_salt()
    await asyncio.sleep(0)
    await main._stop_rotation_loop()
    assert main._rotation_task is None
