"""Pydantic v2 schemas for the telemetry ingest API.

The wire contract is intentionally tiny and stable - installed clients keep
sending long after the collector is upgraded, so validation is strict enough
to reject junk but loose enough never to break an older SDK:

    package      npm package name (the telemetry key), scoped names allowed
    version      package version that ran
    command      subcommand / script name, "" when there is none
    duration_ms  execution duration in milliseconds
    node_major   Node.js major version (e.g. 22)
    os           process.platform value (e.g. darwin, linux, win32)
    is_ci        true when a known CI provider was detected
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# npm package name: optional @scope/ followed by the name. Bounded so a hostile
# client cannot store an unbounded string. Single source of truth: the collector
# route validation in main.py reuses this pattern.
PACKAGE_NAME_PATTERN = r"^[A-Za-z0-9@][A-Za-z0-9@._/-]{0,127}$"

PackageName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=PACKAGE_NAME_PATTERN),
]

VersionStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"),
]
CommandStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9 .:_-]{0,63}$"),
]
OsStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9][a-z0-9._-]{0,31}$"),
]


class TelemetryPing(BaseModel):
    """A single CLI / package execution ping."""

    model_config = ConfigDict(extra="ignore")

    package: PackageName = Field(description="npm package name (the telemetry key)")
    version: VersionStr = Field(description="package version that ran")
    command: CommandStr = Field(default="", description="subcommand or script name, if any")
    duration_ms: int = Field(
        default=0, ge=0, le=86_400_000, description="execution duration in milliseconds"
    )
    node_major: int = Field(default=0, ge=0, le=999, description="Node.js major version")
    os: OsStr = Field(default="unknown", description="process.platform value")
    is_ci: bool = Field(default=False, description="true when a known CI provider was detected")


class VersionCount(BaseModel):
    version: str
    count: int


class NameCount(BaseModel):
    name: str
    count: int


class DailyCount(BaseModel):
    pings: int
    uniques: int


class PackageOverview(BaseModel):
    package: str
    pings: int
    ci: int
    uniques: int
    last_ts: str


class PackageStats(BaseModel):
    package: str
    pings: int
    uniques: int
    ci: int
    avg_duration_ms: float
    max_duration_ms: int
    versions: list[VersionCount]
    commands: list[NameCount]
    os: list[NameCount]
    node: list[NameCount]
    daily: dict[str, DailyCount]


class OverviewResponse(BaseModel):
    packages: list[PackageOverview]
    count: int


class ErasureResult(BaseModel):
    ok: bool
    deleted: int
