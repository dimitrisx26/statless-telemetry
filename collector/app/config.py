"""Environment configuration for the statless-telemetry collector.

All settings are overridable via environment variables:

    DATABASE_URL    Async SQLAlchemy URL. Defaults to SQLite in ./data/.
                    SQLite:      sqlite+aiosqlite:///./data/telemetry.db
                    PostgreSQL:  postgresql+asyncpg://user:pass@db:5432/telemetry
    SALT_ROTATE_HOURS  Hours between platform-hash salt rotations (default: 24, must be > 0)
    SERVER_SECRET   Optional. When set, the platform-hash salt is derived deterministically
                    (HMAC of the UTC date + window), so uniques survive restarts and can be
                    reproduced across replicas sharing the secret.
    TRUST_PROXY     Trust X-Forwarded-For / X-Real-IP for platform hashing. Enable ONLY behind
                    a reverse proxy that overwrites those headers, otherwise clients can spoof
                    identities (default: false).
    RATE_LIMIT      Max pings per minute per client (0 disables, default: 120)
    RETENTION_DAYS  Delete pings older than this many days (default: 180).
                    0 disables automatic deletion (you then own the storage-limitation duty).
    STATS_TOKEN     When set (non-empty), GET /v1/stats, /v1/overview, and /v1/export require
                    ?token=<value>. Empty (default) keeps stats public.
    INGEST_TOKEN    When set, POST /v1/telemetry/ping requires either the header
                    ``X-Statless-Token: <value>`` or ``Authorization: Bearer <value>``.
    TELEMETRY_ENABLED  When false, the collector accepts and silently drops pings (204).
                    Useful to turn ingest off without breaking installed clients.
    BASE_URL        Public base URL rendered into the index metadata (default: http://localhost:8000)
    SECURITY_CONTACT  Contact line for /.well-known/security.txt. Replace the mailto
                    placeholder before going public (default: mailto:security@YOUR-DOMAIN.example)
    SECURITY_POLICY Optional Policy URL for /.well-known/security.txt (e.g. your VDP or ToS)
    TELEMETRY_HOST / TELEMETRY_PORT  Bind address for the `statless-telemetry` entrypoint
                    (namespaced to avoid colliding with shell/CI HOST & PORT; defaults
                    0.0.0.0 / 8000)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, NonNegativeInt
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False)

    database_url: str = f"sqlite+aiosqlite:///{ROOT / 'data' / 'telemetry.db'}"
    salt_rotate_hours: float = Field(default=24.0, gt=0)
    server_secret: str = ""
    trust_proxy: bool = False
    rate_limit: int = Field(default=120, ge=0)
    retention_days: NonNegativeInt = 180
    stats_token: str = ""
    ingest_token: str = ""
    telemetry_enabled: bool = True
    base_url: str = "http://localhost:8000"
    security_contact: str = "mailto:security@YOUR-DOMAIN.example"
    security_policy: str = ""
    host: str = Field(default="0.0.0.0", validation_alias="TELEMETRY_HOST")
    port: int = Field(default=8000, ge=1, le=65535, validation_alias="TELEMETRY_PORT")


@lru_cache
def get_settings() -> Settings:
    return Settings()
