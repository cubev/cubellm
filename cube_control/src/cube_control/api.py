from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import RequestResponseEndpoint

from cube_control.gateway import gateway_metrics
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


def reading_view(reading: Observation, observations: tuple[Observation, ...], now: datetime) -> ReadingView:
    return ReadingView(
        reading=reading,
        freshness=reading.freshness(now),
        current=current_measurement(reading, observations, now, now.strftime("%Y-%m")),
    )


def create_app(repository: Repository, clock: Callable[[], datetime] = utc_now) -> FastAPI:
    app: Final[FastAPI] = FastAPI(title="CubeLLM Control", docs_url=None, redoc_url=None, openapi_url=None)
    static: Final[Path] = Path(__file__).parent / "static"

    @app.middleware("http")
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

    @app.get("/healthz")
    def health() -> JSONResponse:
        ready: Final[bool] = repository.healthy()
        return JSONResponse({"status": "ok" if ready else "unavailable"}, status_code=200 if ready else 503)

    @app.get("/api/v1/summary", response_model=SummaryResponse)
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

    @app.get("/api/v1/fleet", response_model=FleetSnapshot | None)
    def fleet() -> FleetSnapshot | None:
        try:
            return repository.state().fleet
        except psycopg.Error:
            raise HTTPException(503, detail="Zentrale Datenbank ist nicht erreichbar") from None

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "index.html", media_type="text/html")

    @app.get("/app.js")
    def script() -> FileResponse:
        return FileResponse(static / "app.js", media_type="application/javascript")

    @app.get("/style.css")
    def stylesheet() -> FileResponse:
        return FileResponse(static / "style.css", media_type="text/css")

    return app
