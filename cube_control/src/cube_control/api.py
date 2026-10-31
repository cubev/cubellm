from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import RequestResponseEndpoint

from cube_control.gateway import gateway_metrics
from cube_control.media import JobHistory, JobReader, JobSnapshot
from cube_control.models import (
    AccountView,
    FleetSnapshot,
    GatewayMetrics,
    GatewayScanView,
    Observation,
    ProviderId,
    utc_now,
)
from cube_control.store import Repository
from cube_control.summary import account_view, current_measurement
from cube_control.usage import UsageFailure, UsageHistory, UsagePage, UsageQuery, UsageReader


class ReadingView(BaseModel):
    model_config = ConfigDict(frozen=True)
    reading: Observation
    freshness: Literal["fresh", "stale", "clock_skew", "expired"]
    current: bool


class SummaryAccount(BaseModel):
    model_config = ConfigDict(frozen=True)
    data: AccountView
    readings: tuple[ReadingView, ...]


class ProviderGatewayView(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider: ProviderId
    metrics: GatewayMetrics


class SummaryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: Literal[1] = 1
    generated_at: datetime
    period: str
    accounts: tuple[SummaryAccount, ...]
    gateway_sources: tuple[GatewayScanView, ...] = ()
    gateway_unassigned: GatewayMetrics | None = None
    gateway_providers: tuple[ProviderGatewayView, ...] = ()


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: Literal[1] = 1
    gateway_projection_fields: tuple[str, ...] = (
        "account_id",
        "application_id",
        "deployment_id",
        "call_type",
        "completed_at",
        "duration_ms",
    )


def reading_view(reading: Observation, observations: tuple[Observation, ...], now: datetime) -> ReadingView:
    return ReadingView(
        reading=reading,
        freshness=reading.freshness(now),
        current=current_measurement(reading, observations, now, now.strftime("%Y-%m")),
    )


def create_app(
    repository: Repository,
    clock: Callable[[], datetime] = utc_now,
    usage_reader: UsageReader | None = None,
    job_reader: JobReader | None = None,
) -> FastAPI:
    app: Final[FastAPI] = FastAPI(title="CubeLLM Control", docs_url=None, redoc_url=None, openapi_url=None)
    static: Final[Path] = Path(__file__).parent / "static"

    async def response_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response: Final[Response] = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def health() -> JSONResponse:
        ready: Final[bool] = repository.healthy()
        return JSONResponse({"status": "ok" if ready else "unavailable"}, status_code=200 if ready else 503)

    def capabilities() -> CapabilitiesResponse:
        return CapabilitiesResponse()

    def summary() -> SummaryResponse:
        try:
            state: Final = repository.state()
        except psycopg.Error:
            raise HTTPException(503, detail="Zentrale Datenbank ist nicht erreichbar") from None
        now: Final[datetime] = clock()
        period: Final[str] = now.strftime("%Y-%m")
        views: Final[tuple[AccountView, ...]] = tuple(account_view(a, state, now, period) for a in state.accounts)
        return SummaryResponse(
            generated_at=now,
            period=period,
            gateway_sources=state.gateway_scans,
            gateway_unassigned=gateway_metrics(state.gateway_records, None, period) if state.gateway_scans else None,
            gateway_providers=tuple(
                ProviderGatewayView(provider=provider, metrics=gateway_metrics(state.gateway_records, provider, period))
                for provider in sorted(
                    {record.provider for record in state.gateway_records if record.provider is not None}
                )
            ),
            accounts=tuple(
                SummaryAccount(
                    data=view, readings=tuple(reading_view(o, view.observations, now) for o in view.observations)
                )
                for view in views
            ),
        )

    def fleet() -> FleetSnapshot | None:
        try:
            return repository.state().fleet
        except psycopg.Error:
            raise HTTPException(503, detail="Zentrale Datenbank ist nicht erreichbar") from None

    def usage_result(result: UsagePage | UsageHistory | UsageFailure) -> UsagePage | UsageHistory:
        if isinstance(result, UsageFailure):
            status: Final = {"invalid_cursor": 422, "changed": 409, "unavailable": 503}[result.code]
            raise HTTPException(status, detail=result.message)
        return result

    def usage(query: Annotated[UsageQuery, Query()]) -> UsagePage | UsageHistory:
        if usage_reader is None:
            raise HTTPException(503, detail="Nutzungsübersicht nicht eingerichtet")
        return usage_result(usage_reader.page(query, query.cursor))

    def usage_history(
        source_id: Annotated[str, PathParameter(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")],
        source_key: Annotated[str, PathParameter(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> UsagePage | UsageHistory:
        if usage_reader is None:
            raise HTTPException(503, detail="Nutzungsübersicht nicht eingerichtet")
        return usage_result(usage_reader.history(source_id, source_key, after, limit))

    def job_detail(
        account_id: Annotated[str, PathParameter(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")],
        job_id: Annotated[str, PathParameter(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")],
    ) -> JobSnapshot:
        if job_reader is None:
            raise HTTPException(503, detail="Auftragsübersicht nicht eingerichtet")
        try:
            snapshot: Final = job_reader.get(account_id, job_id)
        except psycopg.Error:
            raise HTTPException(503, detail="Auftragsdatenbank ist nicht erreichbar") from None
        if snapshot is None:
            raise HTTPException(404, detail="Auftrag nicht erfasst")
        return snapshot

    def job_history(
        account_id: Annotated[str, PathParameter(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")],
        job_id: Annotated[str, PathParameter(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> JobHistory:
        if job_reader is None:
            raise HTTPException(503, detail="Auftragsübersicht nicht eingerichtet")
        try:
            return job_reader.history(account_id, job_id, after, limit)
        except psycopg.Error:
            raise HTTPException(503, detail="Auftragsdatenbank ist nicht erreichbar") from None

    def index() -> FileResponse:
        return FileResponse(static / "index.html", media_type="text/html")

    def script() -> FileResponse:
        return FileResponse(static / "app.js", media_type="application/javascript")

    def stylesheet() -> FileResponse:
        return FileResponse(static / "style.css", media_type="text/css")

    def usage_script() -> FileResponse:
        return FileResponse(static / "usage.js", media_type="application/javascript")

    app.middleware("http")(response_headers)
    app.add_api_route("/healthz", health, methods=["GET"])
    app.add_api_route("/api/v1/capabilities", capabilities, methods=["GET"], response_model=CapabilitiesResponse)
    app.add_api_route("/api/v1/summary", summary, methods=["GET"], response_model=SummaryResponse)
    app.add_api_route("/api/v1/fleet", fleet, methods=["GET"], response_model=FleetSnapshot | None)
    app.add_api_route("/api/v1/usage", usage, methods=["GET"], response_model=UsagePage)
    app.add_api_route(
        "/api/v1/accounts/{account_id}/jobs/{job_id}", job_detail, methods=["GET"], response_model=JobSnapshot
    )
    app.add_api_route(
        "/api/v1/accounts/{account_id}/jobs/{job_id}/history", job_history, methods=["GET"], response_model=JobHistory
    )
    app.add_api_route(
        "/api/v1/usage/{source_id}/{source_key}/history", usage_history, methods=["GET"], response_model=UsageHistory
    )
    app.add_api_route("/", index, methods=["GET"])
    app.add_api_route("/app.js", script, methods=["GET"])
    app.add_api_route("/style.css", stylesheet, methods=["GET"])
    app.add_api_route("/usage.js", usage_script, methods=["GET"])
    return app
