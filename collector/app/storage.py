"""Async SQLite/PostgreSQL persistence layer for telemetry pings.

Schema - a single ``pings`` table keeps the standalone story simple:

    id             INTEGER PK
    ts             TIMESTAMPTZ       (UTC)
    package        TEXT indexed      (npm package name / telemetry key)
    version        TEXT              (version that ran)
    command        TEXT              (subcommand / script, "" when none)
    duration_ms    INTEGER           (execution duration)
    node_major     INTEGER           (Node.js major version)
    os             TEXT              (process.platform)
    is_ci          BOOLEAN           (known CI provider detected)
    platform_hash  TEXT              (HMAC-SHA256 w/ rotating salt - never a raw IP)

SQLite is the default (zero-config single file). Point ``DATABASE_URL`` at
``postgresql+asyncpg://...`` and the same code runs against Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    ColumnElement,
    CursorResult,
    DateTime,
    Index,
    Integer,
    String,
    and_,
    case,
    delete,
    distinct,
    func,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Ping(Base):
    __tablename__ = "pings"
    __table_args__ = (
        Index("ix_pings_pkg_ts", "package", "ts"),
        Index("ix_pings_pkg_version", "package", "version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    package: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    command: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    node_major: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    os: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    is_ci: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    platform_hash: Mapped[str] = mapped_column(String(32), nullable=False, default="")


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


@asynccontextmanager
async def _session() -> AsyncGenerator[AsyncSession]:
    """Yield a session, or raise if init_db() has not run (no assert - survives -O)."""
    if _session_factory is None:
        raise RuntimeError("call init_db() first")
    async with _session_factory() as session:
        yield session


def get_engine() -> AsyncEngine:
    """Process-wide engine, initialized once from settings. Call init_db() first."""
    global _engine, _session_factory
    if _engine is None:
        from .config import get_settings

        url = get_settings().database_url
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
        _engine = create_async_engine(url, pool_pre_ping=True, **kwargs)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def init_db() -> None:
    """Create tables (and the SQLite directory) from the configured DATABASE_URL."""
    from pathlib import Path

    from .config import get_settings

    url = get_settings().database_url
    if url.startswith("sqlite"):
        path = url.split("sqlite+aiosqlite:///", 1)[-1].split("?")[0]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def _truncate(value: str, limit: int) -> str:
    return value[:limit] if len(value) > limit else value


async def log_ping(
    *,
    package: str,
    version: str,
    command: str = "",
    duration_ms: int = 0,
    node_major: int = 0,
    os: str = "unknown",
    is_ci: bool = False,
    platform_hash: str = "",
) -> None:
    """Insert one telemetry ping."""
    async with _session() as session:
        session.add(
            Ping(
                ts=datetime.now(UTC),
                package=_truncate(package, 128),
                version=_truncate(version or "", 64),
                command=_truncate(command or "", 64),
                duration_ms=int(duration_ms or 0),
                node_major=int(node_major or 0),
                os=_truncate(os or "unknown", 32),
                is_ci=bool(is_ci),
                platform_hash=_truncate(platform_hash or "", 32),
            )
        )
        await session.commit()


async def count_pings(package: str) -> int:
    """Total stored pings for one package (test/debug helper)."""
    async with _session() as session:
        return (
            await session.execute(
                select(func.count()).select_from(Ping).where(Ping.package == package)
            )
        ).scalar() or 0


async def delete_old_events(retention_days: int) -> int:
    """Delete pings older than `retention_days`; return the number deleted.

    GDPR storage-limitation (Art. 5(1)(e)): with RETENTION_DAYS > 0 (default
    180) the lifespan loop prunes daily. 0 disables automatic deletion -
    you then own the storage-limitation duty yourself.
    """
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    async with _session() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(delete(Ping).where(Ping.ts < cutoff)),
        )
        await session.commit()
        return int(result.rowcount or 0)


async def purge_package(package: str) -> int:
    """Hard-delete all pings for one package (maintainer bulk purge)."""
    async with _session() as session:
        result = cast(
            "CursorResult[Any]", await session.execute(delete(Ping).where(Ping.package == package))
        )
        await session.commit()
        return int(result.rowcount or 0)


async def iter_export(
    package: str, include_platform_hash: bool = True
) -> AsyncIterator[dict[str, Any]]:
    """Raw ping rows for one package, oldest first, streamed (NDJSON portable export).

    Streaming keeps memory flat no matter how many pings a package has.
    ``include_platform_hash=False`` omits the pseudonymous identifier column -
    used when stats are public, so per-row pseudonyms are not published openly.
    """
    fields = ["ts", "package", "version", "command", "duration_ms", "node_major", "os", "is_ci"]
    columns = [
        Ping.ts,
        Ping.package,
        Ping.version,
        Ping.command,
        Ping.duration_ms,
        Ping.node_major,
        Ping.os,
        Ping.is_ci,
    ]
    if include_platform_hash:
        fields.append("platform_hash")
        columns.append(Ping.platform_hash)
    async with _session() as session:
        result = await session.stream(
            select(*columns).where(Ping.package == package).order_by(Ping.ts)
        )
        async for row in result:
            out: dict[str, Any] = {}
            for name, value in zip(fields, row, strict=True):
                if name == "ts":
                    out["ts"] = value.isoformat() if hasattr(value, "isoformat") else str(value)
                elif name == "is_ci":
                    out["is_ci"] = bool(value)
                else:
                    out[name] = value
            yield out


def _like_prefix(prefix: str) -> str:
    """Escape LIKE metacharacters so a package prefix matches literally."""
    return prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _utc_day_expr() -> ColumnElement[Any]:
    """SQL expression for the UTC calendar day of ``Ping.ts``.

    SQLite stores UTC ISO strings, so ``date(ts)`` is already UTC. On Postgres,
    ``date(timestamptz)`` converts in the session timezone - force UTC with
    ``AT TIME ZONE 'UTC'`` so daily buckets never drift with the server TZ.
    """
    engine = get_engine()
    if engine.dialect.name == "postgresql":
        return func.date(func.timezone("UTC", Ping.ts))
    return func.date(Ping.ts)


async def get_overview(prefix: str | None = None) -> list[dict[str, Any]]:
    """Per-package totals across the whole collector, busiest first."""
    ci_runs = func.sum(case((Ping.is_ci.is_(True), 1), else_=0)).label("ci")
    stmt = (
        select(
            Ping.package,
            func.count().label("pings"),
            ci_runs,
            func.count(distinct(Ping.platform_hash)).label("uniques"),
            func.max(Ping.ts).label("last_ts"),
        )
        .group_by(Ping.package)
        .order_by(func.count().desc(), Ping.package)
    )
    if prefix:
        stmt = stmt.where(Ping.package.like(_like_prefix(prefix) + "%", escape="\\"))
    async with _session() as session:
        rows = (await session.execute(stmt)).all()
    return [
        {
            "package": pkg,
            "pings": int(pings or 0),
            "ci": int(ci or 0),
            "uniques": int(uniques or 0),
            "last_ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        }
        for pkg, pings, ci, uniques, ts in rows
    ]


async def get_package_stats(
    package: str,
    since: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    """Aggregate counts for one package: totals, versions, commands, OS, Node, daily.

    since/to are inclusive UTC date bounds (YYYY-MM-DD), validated by the route.
    """
    conditions = [Ping.package == package]
    if since:
        conditions.append(Ping.ts >= datetime.fromisoformat(since).replace(tzinfo=UTC))
    if to:
        upper = datetime.fromisoformat(to).replace(tzinfo=UTC) + timedelta(days=1)
        conditions.append(Ping.ts < upper)
    where = and_(*conditions)
    async with _session() as session:
        total, ci_count, uniques, avg_duration, max_duration = (
            await session.execute(
                select(
                    func.count(),
                    func.sum(case((Ping.is_ci.is_(True), 1), else_=0)),
                    func.count(distinct(Ping.platform_hash)),
                    func.avg(Ping.duration_ms),
                    func.max(Ping.duration_ms),
                ).where(where)
            )
        ).one()
        version_rows = (
            await session.execute(
                select(Ping.version, func.count())
                .where(where)
                .group_by(Ping.version)
                .order_by(func.count().desc(), Ping.version)
                .limit(50)
            )
        ).all()
        command_rows = (
            await session.execute(
                select(Ping.command, func.count())
                .where(where, Ping.command != "")
                .group_by(Ping.command)
                .order_by(func.count().desc(), Ping.command)
                .limit(50)
            )
        ).all()
        os_rows = (
            await session.execute(
                select(Ping.os, func.count())
                .where(where)
                .group_by(Ping.os)
                .order_by(func.count().desc(), Ping.os)
                .limit(20)
            )
        ).all()
        node_rows = (
            await session.execute(
                select(Ping.node_major, func.count())
                .where(where)
                .group_by(Ping.node_major)
                .order_by(Ping.node_major)
            )
        ).all()
        day = _utc_day_expr()
        daily_rows = (
            await session.execute(
                select(
                    day.label("day"),
                    func.count().label("pings"),
                    func.count(distinct(Ping.platform_hash)).label("uniques"),
                )
                .where(where)
                .group_by(day)
                .order_by(day)
            )
        ).all()

    return {
        "package": package,
        "pings": int(total or 0),
        "uniques": int(uniques or 0),
        "ci": int(ci_count or 0),
        "avg_duration_ms": round(float(avg_duration or 0), 1),
        "max_duration_ms": int(max_duration or 0),
        "versions": [{"version": v, "count": int(n)} for v, n in version_rows],
        "commands": [{"name": c, "count": int(n)} for c, n in command_rows],
        "os": [{"name": o, "count": int(n)} for o, n in os_rows],
        "node": [{"name": str(n), "count": int(c)} for n, c in node_rows],
        "daily": {
            str(day): {"pings": int(n or 0), "uniques": int(u or 0)} for day, n, u in daily_rows
        },
    }
