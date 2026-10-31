from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Final, Literal, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cube_control.gateway_store import latest_scans
from cube_control.models import FrozenModel, GatewayMetrics, GatewayRecord, GatewayScanView, Identifier, ProviderId

TrafficKind = Literal["provider_request", "gateway_management", "unknown"]
PROVIDER_CALL_TYPES: Final = frozenset(
    {
        "completion",
        "acompletion",
        "text_completion",
        "atext_completion",
        "embedding",
        "aembedding",
        "responses",
        "aresponses",
        "anthropic_messages",
        "aanthropic_messages",
        "speech",
        "aspeech",
        "transcription",
        "atranscription",
        "image_generation",
        "aimage_generation",
        "moderation",
        "amoderation",
        "rerank",
        "arerank",
        "generate_content",
        "agenerate_content",
        "generate_content_stream",
        "agenerate_content_stream",
        "create_batch",
        "acreate_batch",
        "retrieve_batch",
        "aretrieve_batch",
    }
)
MANAGEMENT_CALL_TYPES: Final = frozenset(
    {
        "/v1/models",
        "/models",
        "/model/info",
        "/key/list",
        "/get/latest_release_info",
        "/health",
        "/health/liveliness",
        "/health/readiness",
        "/health/services",
    }
)
TRAFFIC_SQL: Final = sql.SQL(
    "CASE WHEN payload->>'call_type' IN ({provider_types}) THEN 'provider_request' "
    "WHEN payload->>'call_type' IN ({management_types}) THEN 'gateway_management' ELSE 'unknown' END"
).format(
    provider_types=sql.SQL(",").join(sql.Literal(name) for name in sorted(PROVIDER_CALL_TYPES)),
    management_types=sql.SQL(",").join(sql.Literal(name) for name in sorted(MANAGEMENT_CALL_TYPES)),
)


class UsageQuery(FrozenModel):
    period: Annotated[str, Field(pattern=r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")]
    source_id: Identifier | None = None
    provider: ProviderId | None = None
    account_id: Identifier | None = None
    application_id: Identifier | None = None
    model: Annotated[str, Field(min_length=1, max_length=150)] | None = None
    status: Literal["succeeded", "failed", "unknown"] | None = None
    traffic_kind: TrafficKind | None = None
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    cursor: Annotated[str | None, Field(max_length=4096, exclude=True)] = None

    @model_validator(mode="after")
    def valid_period(self) -> "UsageQuery":
        datetime.strptime(self.period, "%Y-%m")
        return self


class UsageRow(FrozenModel):
    source_id: Identifier
    first_sequence: int
    last_sequence: int
    record: GatewayRecord
    traffic_kind: TrafficKind


class UsagePage(FrozenModel):
    schema_version: Literal[1] = 1
    scope: Literal["stored_gateway_rows"] = "stored_gateway_rows"
    cost_basis: Literal["gateway_reported"] = "gateway_reported"
    query: UsageQuery
    snapshot: str
    sources: tuple[GatewayScanView, ...]
    total: int
    unknown_account_calls: int
    unknown_application_calls: int
    provider_calls: int
    management_calls: int
    unknown_traffic_calls: int
    metrics: GatewayMetrics
    rows: tuple[UsageRow, ...]
    next_cursor: str | None


class UsageChange(FrozenModel):
    sequence: int
    previous_hash: str | None
    content_hash: str
    record: GatewayRecord


class UsageHistory(FrozenModel):
    source_id: Identifier
    source_key: Identifier
    changes: tuple[UsageChange, ...]
    next_after: int | None


class UsageCursor(FrozenModel):
    query_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    snapshot: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    offset: Annotated[int, Field(ge=0, le=1_000_000_000)]


class AttributionTotals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    calls: int
    unknown_account_calls: int
    unknown_application_calls: int
    provider_calls: int
    management_calls: int
    unknown_traffic_calls: int


@dataclass(frozen=True, slots=True)
class UsageFailure:
    code: Literal["invalid_cursor", "changed", "unavailable"]
    message: str


class UsageReader(Protocol):
    def page(self, query: UsageQuery, cursor: str | None) -> UsagePage | UsageFailure: ...

    def history(self, source_id: str, source_key: str, after: int, limit: int) -> UsageHistory | UsageFailure: ...


FILTER: Final = sql.SQL("""
FROM cube_control.gateway_records
WHERE (payload->>'occurred_at')::timestamptz >= %(start)s::timestamptz
AND (payload->>'occurred_at')::timestamptz < %(end)s::timestamptz
AND (%(source_id)s::text IS NULL OR source_id = %(source_id)s)
AND (%(provider)s::text IS NULL OR payload->>'provider' = %(provider)s)
AND (%(account_id)s::text IS NULL OR payload->>'account_id' = %(account_id)s)
AND (%(application_id)s::text IS NULL OR payload->>'application_id' = %(application_id)s)
AND (%(model)s::text IS NULL OR payload->>'model' = %(model)s)
AND (%(status)s::text IS NULL OR payload->>'status' = %(status)s)
 AND (%(traffic_kind)s::text IS NULL OR ({traffic}) = %(traffic_kind)s)
""").format(traffic=TRAFFIC_SQL)

TOTALS: Final = sql.SQL("""
SELECT count(*) AS calls,
count(*) FILTER (WHERE payload->>'status'='failed') AS failures,
count(*) FILTER (WHERE payload->>'status'='unknown') AS unknown_status,
COALESCE(sum((payload->>'amount_usd')::numeric),0) AS reported_usd,
count(*) FILTER (WHERE payload->>'amount_usd' IS NULL) AS unknown_cost_calls,
COALESCE(sum((payload->>'input_tokens')::bigint),0) AS input_tokens,
count(*) FILTER (WHERE payload->>'input_tokens' IS NULL) AS unknown_input_calls,
COALESCE(sum((payload->>'output_tokens')::bigint),0) AS output_tokens,
count(*) FILTER (WHERE payload->>'output_tokens' IS NULL) AS unknown_output_calls,
COALESCE(sum((payload->>'cache_read_tokens')::bigint),0) AS cache_read_tokens,
count(*) FILTER (WHERE payload->>'cache_read_tokens' IS NULL) AS unknown_cache_read_calls,
COALESCE(sum((payload->>'cache_write_tokens')::bigint),0) AS cache_write_tokens,
count(*) FILTER (WHERE payload->>'cache_write_tokens' IS NULL) AS unknown_cache_write_calls,
count(*) FILTER (WHERE payload->>'account_id' IS NULL) AS unknown_account_calls,
count(*) FILTER (WHERE payload->>'application_id' IS NULL) AS unknown_application_calls
""")
TRAFFIC_TOTALS: Final = sql.SQL(
    ",count(*) FILTER (WHERE ({traffic})='provider_request') AS provider_calls"
    ",count(*) FILTER (WHERE ({traffic})='gateway_management') AS management_calls"
    ",count(*) FILTER (WHERE ({traffic})='unknown') AS unknown_traffic_calls "
).format(traffic=TRAFFIC_SQL)


def query_parameters(query: UsageQuery, offset: int) -> dict[str, str | int | None]:
    start: Final = datetime.strptime(query.period, "%Y-%m").replace(tzinfo=UTC)
    end_year: Final = start.year + (start.month == 12)
    end_month: Final = 1 if start.month == 12 else start.month + 1
    return {
        "start": start.isoformat(),
        "end": f"{end_year:04d}-{end_month:02d}-01T00:00:00+00:00",
        "source_id": query.source_id,
        "provider": query.provider,
        "account_id": query.account_id,
        "application_id": query.application_id,
        "model": query.model,
        "status": query.status,
        "traffic_kind": query.traffic_kind,
        "limit": query.limit + 1,
        "offset": offset,
    }


@dataclass(frozen=True, slots=True)
class PostgresUsageReader:
    dsn: str

    def page(self, query: UsageQuery, cursor: str | None = None) -> UsagePage | UsageFailure:
        try:
            decoded: Final = UsageCursor.model_validate_json(cursor) if cursor is not None else None
        except ValidationError:
            return UsageFailure("invalid_cursor", "Ungültige Fortsetzung; Abfrage neu starten")
        query_hash: Final = sha256(query.model_dump_json().encode()).hexdigest()
        if decoded is not None and decoded.query_hash != query_hash:
            return UsageFailure("invalid_cursor", "Fortsetzung gehört zu einer anderen Abfrage")
        try:
            with psycopg.connect(self.dsn, connect_timeout=5) as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                connection.execute("SET LOCAL statement_timeout='10s'")
                sources: Final = tuple(
                    source
                    for source in latest_scans(connection)
                    if query.source_id is None or source.scan.source_id == query.source_id
                )
                snapshot: Final = sha256(
                    "\n".join(f"{row.scan.source_id}:{row.sequence}" for row in sources).encode()
                ).hexdigest()
                if decoded is not None and decoded.snapshot != snapshot:
                    return UsageFailure("changed", "Erfassung wurde aktualisiert; Abfrage neu starten")
                params: Final = query_parameters(query, decoded.offset if decoded else 0)
                with connection.cursor(row_factory=dict_row) as cursor_rows:
                    totals: Final = cursor_rows.execute(TOTALS + TRAFFIC_TOTALS + FILTER, params).fetchone()
                rows: Final = tuple(
                    UsageRow.model_validate(
                        {
                            "source_id": row[0],
                            "first_sequence": row[1],
                            "last_sequence": row[2],
                            "record": row[3],
                            "traffic_kind": row[4],
                        }
                    )
                    for row in connection.execute(
                        sql.SQL("SELECT source_id,first_sequence,last_sequence,payload,")
                        + TRAFFIC_SQL
                        + sql.SQL(" ")
                        + FILTER
                        + sql.SQL(
                            " ORDER BY (payload->>'occurred_at')::timestamptz DESC,source_id,source_key "
                            "LIMIT %(limit)s OFFSET %(offset)s"
                        ),
                        params,
                    )
                )
                if totals is None:
                    return UsageFailure("unavailable", "Nutzungsübersicht nicht verfügbar")
                attribution: Final = AttributionTotals.model_validate(totals)
                return UsagePage(
                    query=query,
                    snapshot=snapshot,
                    sources=sources,
                    total=attribution.calls,
                    unknown_account_calls=attribution.unknown_account_calls,
                    unknown_application_calls=attribution.unknown_application_calls,
                    provider_calls=attribution.provider_calls,
                    management_calls=attribution.management_calls,
                    unknown_traffic_calls=attribution.unknown_traffic_calls,
                    metrics=GatewayMetrics.model_validate({name: totals[name] for name in GatewayMetrics.model_fields}),
                    rows=rows[: query.limit],
                    next_cursor=UsageCursor(
                        query_hash=query_hash,
                        snapshot=snapshot,
                        offset=(decoded.offset if decoded else 0) + query.limit,
                    ).model_dump_json()
                    if len(rows) > query.limit
                    else None,
                )
        except psycopg.Error:
            return UsageFailure("unavailable", "Nutzungsdatenbank nicht erreichbar")

    def history(self, source_id: str, source_key: str, after: int = 0, limit: int = 50) -> UsageHistory | UsageFailure:
        try:
            with psycopg.connect(self.dsn, connect_timeout=5) as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                connection.execute("SET LOCAL statement_timeout='10s'")
                changes: Final = tuple(
                    UsageChange.model_validate(
                        {
                            "sequence": row[0],
                            "previous_hash": row[1],
                            "content_hash": row[2],
                            "record": row[3],
                        }
                    )
                    for row in connection.execute(
                        "SELECT sequence,previous_hash,content_hash,payload FROM cube_control.gateway_changes "
                        "WHERE source_id=%s AND source_key=%s AND sequence>%s ORDER BY sequence LIMIT %s",
                        (source_id, source_key, after, limit + 1),
                    )
                )
                return UsageHistory(
                    source_id=source_id,
                    source_key=source_key,
                    changes=changes[:limit],
                    next_after=changes[limit - 1].sequence if len(changes) > limit else None,
                )
        except psycopg.Error:
            return UsageFailure("unavailable", "Nutzungsverlauf nicht erreichbar")
