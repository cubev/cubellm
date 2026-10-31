from collections.abc import Mapping
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

import httpx
from pydantic import JsonValue, ValidationError

from cube_control.gateway import GatewayProjection, GatewayProjectionBatch, prepare_gateway, projected_record
from cube_control.models import GatewayBatch, GatewayRecord, ProviderId

PROJECTION_COLUMNS: Final = """request_id,custom_llm_provider AS reported_provider,api_base,model,
       "startTime" AT TIME ZONE 'UTC' AS occurred_at,
       COALESCE(updated_at,created_at,"endTime","startTime") AT TIME ZONE 'UTC' AS source_updated_at,
       NULLIF(metadata->>'user_api_key_application_id','') AS application_id,
       CASE WHEN metadata->>'provider_account_source' = 'configured_credential'
            THEN NULLIF(metadata->>'provider_account_id','') END AS account_id,
       CASE WHEN metadata->>'provider_account_source' = 'configured_credential'
            THEN jsonb_build_object('provider',metadata->>'provider_account_provider',
                                   'source',metadata->>'provider_account_source',
                                   'origin',metadata->>'provider_origin',
                                   'cost_status',metadata->>'provider_cost_status') END AS account_evidence,
       NULLIF(model_id,'') AS deployment_id,NULLIF(call_type,'') AS call_type,
       "endTime" AT TIME ZONE 'UTC' AS completed_at,request_duration_ms AS duration_ms,
       status,spend::numeric::text AS amount_usd,prompt_tokens AS input_tokens,completion_tokens AS output_tokens,
       COALESCE(metadata #>> '{additional_usage_values,cache_read_input_tokens}',
          metadata #>> '{additional_usage_values,prompt_tokens_details,cached_tokens}') AS cache_read_tokens,
       COALESCE(metadata #>> '{additional_usage_values,cache_creation_input_tokens}',
          metadata #>> '{additional_usage_values,prompt_tokens_details,cache_write_tokens}',
          metadata #>> '{additional_usage_values,prompt_tokens_details,cache_creation_tokens}') AS cache_write_tokens"""
ORIGIN_PROVIDERS: Final[Mapping[str, ProviderId]] = MappingProxyType(
    {
        "api.openai.com": "openai",
        "api.anthropic.com": "anthropic",
        "openrouter.ai": "openrouter",
        "api.x.ai": "xai",
        "api.heygen.com": "heygen",
        "api.github.com": "github",
    }
)


class NativeSpendProjection(GatewayProjection):
    api_base: str | None = None
    endpoint_provider: ProviderId | None = None
    custom_endpoint: bool = False


def native_projection(raw: JsonValue, identity: str) -> GatewayRecord | None:
    try:
        row: Final = NativeSpendProjection.model_validate(raw)
        address: Final = urlsplit(row.api_base or "")
        hostname: Final = address.hostname
        if row.account_evidence is not None:
            target: Final = httpx.URL(row.api_base or "")
            origin: Final = target.scheme + "://" + target.netloc.decode("ascii")
            if address.username is not None or address.password is not None or origin != row.account_evidence.origin:
                return None
        configured_provider: Final = row.account_evidence.provider if row.account_evidence is not None else None
        known_provider: Final = ORIGIN_PROVIDERS.get(hostname or "")
        if configured_provider is not None and known_provider is not None and configured_provider != known_provider:
            return None
        selected_provider: Final = configured_provider or known_provider
        if row.account_evidence is not None and selected_provider is None:
            return None
        projection: Final = GatewayProjection.model_validate(
            row.model_dump(exclude={"api_base"})
            | {"endpoint_provider": selected_provider, "custom_endpoint": bool(row.api_base)}
        )
        record: Final = projected_record(projection, identity)
        return (
            record.model_copy(
                update={
                    "input_tokens": None,
                    "output_tokens": None,
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                }
            )
            if record.account_evidence is not None and record.call_type == "pass_through_endpoint"
            else record
        )
    except (ValidationError, ValueError, httpx.InvalidURL):
        return None


def prepare_native_gateway(batch: GatewayProjectionBatch) -> GatewayBatch:
    return prepare_gateway(batch, project=native_projection)
