"""Read-only comparison of submitted media jobs with the status HeyGen reports for them.

The job ID of a HeyGen media job is the HeyGen video ID. HeyGen's video endpoint reports a lifecycle status but no
cost, so this check never changes a job and never infers usage: finalizing still needs the actual usage, and a
failure still needs a reviewed command. Contract checked on 2026-11-14 against
https://developers.heygen.com/reference/get-video and the `videos:read` scope in
https://developers.heygen.com/docs/api-key-permissions.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr

from cube_control.media import JobList
from cube_control.models import FrozenModel, Identifier
from cube_control.provider_access import Unavailable, fetch_report, read_private_key

CHECK_LIMIT: Final = 100
ProviderStatus = Literal["pending", "processing", "completed", "failed"]
Suggestion = Literal[
    "wait",
    "finalize_after_confirming_usage",
    "review_failure",
    "provider_status_unavailable",
]


class _ProviderModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class VideoDetail(_ProviderModel):
    id: Annotated[str, Field(min_length=1, max_length=256)]
    status: ProviderStatus
    completed_at: Annotated[int, Field(ge=0, le=10**11)] | None = None
    duration: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = None
    failure_code: Annotated[str, Field(max_length=200)] | None = None


class VideoResponse(_ProviderModel):
    data: VideoDetail


VideoResult = VideoDetail | Unavailable


class RecentJobs(Protocol):
    def recent(self, now: datetime, account_id: str | None = None, limit: int = 50) -> JobList: ...


class JobCheck(FrozenModel):
    job_id: Identifier
    version: int
    provider_status: ProviderStatus | None = None
    completed_at: AwareDatetime | None = None
    duration_seconds: Decimal | None = None
    failure_code: str | None = None
    suggestion: Suggestion
    message: Annotated[str, Field(max_length=200)] = ""


class JobCheckReport(FrozenModel):
    account_id: Identifier
    checked_at: AwareDatetime
    checks: tuple[JobCheck, ...]
    truncated: bool
    message: Annotated[str, Field(max_length=200)] = ""


def fetch_video(key: SecretStr, video_id: str, transport: httpx.BaseTransport | None = None) -> VideoResult:
    result: Final = fetch_report(
        f"https://api.heygen.com/v3/videos/{quote(video_id, safe='')}",
        {"X-Api-Key": key.get_secret_value()},
        VideoResponse,
        "HeyGen",
        "HeyGen-Videostatus benötigt videos:read",
        transport,
    )
    return result.data if isinstance(result, VideoResponse) else result


def _check(job_id: str, version: int, result: VideoResult) -> JobCheck:
    if isinstance(result, Unavailable):
        return JobCheck(
            job_id=job_id, version=version, suggestion="provider_status_unavailable", message=result.message
        )
    if result.id != job_id:
        return JobCheck(
            job_id=job_id,
            version=version,
            suggestion="provider_status_unavailable",
            message="HeyGen meldete eine andere Video-ID; nichts übernommen",
        )
    suggestion: Final[Suggestion] = (
        "finalize_after_confirming_usage"
        if result.status == "completed"
        else "review_failure"
        if result.status == "failed"
        else "wait"
    )
    return JobCheck(
        job_id=job_id,
        version=version,
        provider_status=result.status,
        completed_at=datetime.fromtimestamp(result.completed_at, UTC) if result.completed_at is not None else None,
        duration_seconds=result.duration,
        failure_code=result.failure_code if result.status == "failed" else None,
        suggestion=suggestion,
    )


def check_heygen_jobs(
    path: Path,
    account_id: str,
    jobs: RecentJobs,
    now: datetime,
    fetch: Callable[[SecretStr, str], VideoResult] = fetch_video,
) -> JobCheckReport:
    """Reads the HeyGen status of every submitted job of the account (at most CHECK_LIMIT per run)."""
    submitted: Final = tuple(view for view in jobs.recent(now, account_id, 200).jobs if view.job.state == "submitted")
    key: Final = read_private_key(path, "HEYGEN_API_KEY", "HeyGen")
    if isinstance(key, Unavailable):
        return JobCheckReport(account_id=account_id, checked_at=now, checks=(), truncated=False, message=key.message)
    return JobCheckReport(
        account_id=account_id,
        checked_at=now,
        checks=tuple(
            _check(view.job.job_id, view.version, fetch(key, view.job.job_id)) for view in submitted[:CHECK_LIMIT]
        ),
        truncated=len(submitted) > CHECK_LIMIT,
    )
