from datetime import UTC
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

from cube_control.models import (
    GatewayBatch,
    GatewayMetrics,
    GatewayRecord,
    GatewayScan,
    Identifier,
    Provider,
    ProviderId,
)


class GatewayProjection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: Annotated[str, Field(min_length=1, max_length=512)]
    reported_provider: Annotated[str, Field(max_length=100)] | None
    endpoint_provider: Provider | ProviderId | None
    custom_endpoint: bool
    model: Annotated[str, Field(max_length=150)] | None
    occurred_at: AwareDatetime
    source_updated_at: AwareDatetime | None
    account_id: Identifier | None = None
    application_id: Identifier | None = None
    deployment_id: Identifier | None = None
    call_type: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    completed_at: AwareDatetime | None = None
    duration_ms: Annotated[int, Field(ge=0, strict=True)] | None = None
    status: str | None
    amount_usd: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None
    input_tokens: Annotated[int, Field(ge=0)] | None
    output_tokens: Annotated[int, Field(ge=0)] | None
    cache_read_tokens: Annotated[int, Field(ge=0)] | None
    cache_write_tokens: Annotated[int, Field(ge=0)] | None

    @field_validator("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", mode="before")
    @staticmethod
    def reject_boolean_counter(value: JsonValue) -> JsonValue:
        if isinstance(value, bool):
            raise ValueError("Boolean values are not token counts")  # noqa: TRY004  # Pydantic converts ValueError into ValidationError
        return value


class ReportedProvider(BaseModel):
    value: ProviderId


def attributed_provider(row: GatewayProjection) -> Provider | ProviderId | None:
    if row.endpoint_provider is not None:
        return row.endpoint_provider
    if row.custom_endpoint:
        return None
    normalized: Final[str] = (row.reported_provider or "").lower().strip()
    if not normalized:
        return None
    try:
        return ReportedProvider.model_validate({"value": normalized}).value
    except ValidationError:
        return None


def projected_record(row: GatewayProjection, source_id: str) -> GatewayRecord:
    return GatewayRecord(
        source_key=sha256(f"{source_id}:LiteLLM_SpendLogs:{row.request_id}".encode()).hexdigest(),
        provider=attributed_provider(row),
        account_id=row.account_id,
        application_id=row.application_id,
        deployment_id=row.deployment_id,
        call_type=row.call_type,
        model=row.model or "Unbekanntes Modell",
        occurred_at=row.occurred_at,
        source_updated_at=row.source_updated_at,
        completed_at=row.completed_at,
        duration_ms=row.duration_ms,
        status="succeeded" if row.status == "success" else "failed" if row.status == "failure" else "unknown",
        amount_usd=row.amount_usd,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_read_tokens=row.cache_read_tokens,
        cache_write_tokens=row.cache_write_tokens,
    )


def token_value(
    record: GatewayRecord, field: Literal["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"]
) -> int | None:
    match field:
        case "input_tokens":
            return record.input_tokens
        case "output_tokens":
            return record.output_tokens
        case "cache_read_tokens":
            return record.cache_read_tokens
        case "cache_write_tokens":
            return record.cache_write_tokens


def token_sum(
    records: tuple[GatewayRecord, ...],
    field: Literal["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"],
) -> int:
    return sum(token_value(record, field) or 0 for record in records)


def missing_tokens(
    records: tuple[GatewayRecord, ...],
    field: Literal["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"],
) -> int:
    return sum(token_value(record, field) is None for record in records)


def gateway_metrics(
    records: tuple[GatewayRecord, ...], provider: Provider | ProviderId | None, period: str
) -> GatewayMetrics:
    matching: Final = tuple(
        record
        for record in records
        if record.provider == provider and record.occurred_at.astimezone(UTC).strftime("%Y-%m") == period
    )
    return GatewayMetrics(
        calls=len(matching),
        failures=sum(record.status == "failed" for record in matching),
        unknown_status=sum(record.status == "unknown" for record in matching),
        reported_usd=sum((record.amount_usd for record in matching if record.amount_usd is not None), Decimal(0)),
        unknown_cost_calls=sum(record.amount_usd is None for record in matching),
        input_tokens=token_sum(matching, "input_tokens"),
        unknown_input_calls=missing_tokens(matching, "input_tokens"),
        output_tokens=token_sum(matching, "output_tokens"),
        unknown_output_calls=missing_tokens(matching, "output_tokens"),
        cache_read_tokens=token_sum(matching, "cache_read_tokens"),
        unknown_cache_read_calls=missing_tokens(matching, "cache_read_tokens"),
        cache_write_tokens=token_sum(matching, "cache_write_tokens"),
        unknown_cache_write_calls=missing_tokens(matching, "cache_write_tokens"),
    )


class GatewayProjectionBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scan: GatewayScan
    identity: Identifier
    rows: Annotated[tuple[JsonValue, ...], Field(max_length=1000)] = ()


def safe_projection(raw: JsonValue, identity: str) -> GatewayRecord | None:
    try:
        return projected_record(GatewayProjection.model_validate(raw), identity)
    except ValidationError:
        return None


def prepare_gateway(batch: GatewayProjectionBatch) -> GatewayBatch:
    projected: Final = tuple(safe_projection(row, batch.identity) for row in batch.rows)
    records: Final = tuple(record for record in projected if record is not None)
    rejected: Final = batch.scan.rejected_rows + sum(record is None for record in projected)
    scan: Final = batch.scan.model_copy(
        update={
            "rejected_rows": rejected,
            "unassigned_rows": batch.scan.unassigned_rows + sum(record.provider is None for record in records),
            "state": "partial" if rejected and batch.scan.state == "ok" else batch.scan.state,
            "message": "Einzelne Quellzeilen konnten nicht validiert werden" if rejected else batch.scan.message,
        }
    )
    return GatewayBatch(scan=scan, records=records)
