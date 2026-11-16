from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from itertools import chain
from pathlib import Path
import re
from typing import Annotated, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from cube_control.models import Account, ImportBatch, Observation
from cube_control.provider_access import Unavailable, fetch_report, read_private_key

Counter = Annotated[int, Field(strict=True, ge=0)]
ResourceName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
QuotaMetric = Literal["usage", "quota_limit", "quota_remaining"]
QuotaUnit = Literal["requests", "points", "quota_units"]
SNAPSHOT_SCOPE: Final = "github-rate-limits"


class ResourceQuota(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    used: Counter | None = None
    limit: Counter | None = None
    remaining: Counter | None = None
    reset: Annotated[int, Field(strict=True, ge=0, le=253402300799)] | None = None


class RateLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    resources: Annotated[dict[ResourceName, ResourceQuota], Field(min_length=1, max_length=64)]


RateResult = RateLimits | Unavailable


def fetch_limits(key: SecretStr, transport: httpx.BaseTransport | None = None) -> RateResult:
    return fetch_report(
        "https://api.github.com/rate_limit",
        {
            "Authorization": "Bearer " + key.get_secret_value(),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        RateLimits,
        "GitHub",
        "GitHub-Kontingentbericht nicht zugänglich",
        transport,
        authoritative_headers,
    )


def authoritative_headers(report: RateLimits, headers: Mapping[str, str]) -> RateLimits:
    names: Final = ("resource", "used", "limit", "remaining", "reset")
    values: Final = tuple(headers.get("x-ratelimit-" + name) for name in names)
    if all(value is None for value in values):
        return report
    if any(value is None for value in values):
        raise ValueError("Incomplete rate-limit headers")
    resource: Final = headers["x-ratelimit-resource"]
    counts: Final = {name: headers["x-ratelimit-" + name] for name in names[1:]}
    if any(re.fullmatch(r"[0-9]{1,20}", value) is None for value in counts.values()):
        raise ValueError("Invalid rate-limit headers")
    quota: Final = ResourceQuota.model_validate({name: int(value) for name, value in counts.items()})
    return RateLimits.model_validate({"resources": report.resources | {resource: quota}})


def quota_unit(resource: str) -> QuotaUnit:
    if resource == "graphql":
        return "points"
    return "requests" if resource in ("core", "search", "code_search") else "quota_units"


def resource_readings(account_id: str, resource: str, quota: ResourceQuota, now: datetime) -> tuple[Observation, ...]:
    reset: Final = datetime.fromtimestamp(quota.reset, UTC) if quota.reset is not None else None
    fields: Final[tuple[tuple[QuotaMetric, int | None], ...]] = (
        ("usage", quota.used),
        ("quota_limit", quota.limit),
        ("quota_remaining", quota.remaining),
    )
    return tuple(
        Observation(
            account_id=account_id,
            external_id=sha256(f"{now.isoformat()}:{resource}:{metric}:{value}:{reset}".encode()).hexdigest(),
            metric=metric,
            window="GitHub " + resource,
            snapshot_scope=SNAPSHOT_SCOPE,
            source="provider_api",
            observed_at=now,
            collected_at=now,
            value=Decimal(value) if value is not None else None,
            unit=quota_unit(resource) if value is not None else None,
            status="known" if value is not None else "unknown",
            resets_at=reset,
            expires_at=reset,
            message="Kontingentaufnahme; kann zwischen Zugängen geteilt sein. Keine Rechnung und kein Request-Protokoll.",
        )
        for metric, value in fields
    )


def quota_readings(account_id: str, result: RateResult, now: datetime) -> tuple[Observation, ...]:
    if isinstance(result, Unavailable):
        return (
            Observation(
                account_id=account_id,
                external_id=sha256(f"{now.isoformat()}:unavailable".encode()).hexdigest(),
                metric="usage",
                window="all",
                snapshot_scope=SNAPSHOT_SCOPE,
                source="provider_api",
                observed_at=now,
                collected_at=now,
                status=result.status,
                message=result.message,
            ),
        )
    return tuple(
        chain.from_iterable(
            resource_readings(account_id, resource, quota, now) for resource, quota in sorted(result.resources.items())
        )
    )


def collect_github(
    path: Path,
    now: datetime,
    fetch: Callable[[SecretStr], RateResult] = fetch_limits,
    *,
    account_id: str = "github-api",
    account_name: str = "GitHub API",
) -> ImportBatch:
    account: Final = Account(
        id=account_id,
        provider="github",
        name=account_name,
        kind="api",
        state="active",
        note="Kontingente können zwischen Zugängen geteilt sein; nicht schlüsselweise aufsummieren",
    )
    key: Final = read_private_key(path, "GITHUB_TOKEN", "GitHub")
    result: Final = key if isinstance(key, Unavailable) else fetch(key)
    return ImportBatch(
        accounts=() if isinstance(result, Unavailable) else (account,),
        observations=quota_readings(account.id, result, now),
    )
