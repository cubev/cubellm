from datetime import datetime, timedelta
from typing import Annotated, Final, Literal

import httpx
from pydantic import AwareDatetime, BaseModel, Field, SecretStr, ValidationError

from cube_control.application_gateway import gateway_client, request_gateway, require_admin_origin
from cube_control.models import FrozenModel

Origin = Literal["runtime", "database"]
SourceName = Literal["credentials", "routes", "deployments"]


class InventorySource(FrozenModel):
    name: SourceName
    origin: Origin
    status: Literal["complete", "partial", "unavailable"]
    records: Annotated[int, Field(ge=0)] = 0
    rejected: Annotated[int, Field(ge=0)] = 0
    detail: Literal["", "unreadable", "database_unavailable", "database_read_failed"] = ""


class InventoryRejection(FrozenModel):
    source: SourceName
    origin: Origin
    index: Annotated[int, Field(ge=0)]
    name: str | None = None
    reason: Literal["not_an_object", "unreadable_fields"]


class InventoryCredential(FrozenModel):
    origin: Origin
    credential_name: str
    account_id: str | None = None
    provider_id: str | None = None
    llm_provider: str | None = None
    allowed_origins: tuple[str, ...] = ()
    binding: Literal["bound", "unbound", "invalid"]


class InventoryRoute(FrozenModel):
    origin: Origin
    route_id: str | None = None
    path: str | None = None
    account_id: str | None = None
    credential_name: str | None = None
    methods: tuple[str, ...] = ()
    resource_types: tuple[str, ...] = ()
    applications: tuple[str, ...] = ()
    grant_count: Annotated[int, Field(ge=0)] = 0
    inline_header_names: tuple[str, ...] = ()
    binding: Literal["account", "unbound", "invalid"]


class InventoryDeployment(FrozenModel):
    origin: Origin
    model_name: str
    model_id: str | None = None
    provider: str | None = None
    provider_hint: str | None = None
    credential_name: str | None = None
    auth: Literal[
        "credential_reference", "environment_reference", "inline_api_key", "stored_api_key", "cloud_identity", "unknown"
    ]


class GatewayInventory(FrozenModel):
    schema_version: Literal[1]
    observed_at: AwareDatetime
    sources: tuple[InventorySource, ...]
    credentials: tuple[InventoryCredential, ...]
    routes: tuple[InventoryRoute, ...]
    deployments: tuple[InventoryDeployment, ...]
    rejected: tuple[InventoryRejection, ...]


class GatewayInventorySnapshot(FrozenModel):
    checked_at: AwareDatetime
    gateway_origin: str
    inventory: GatewayInventory


class GatewayInventoryResult(FrozenModel):
    status: Literal["collected", "error", "uncertain"]
    message: str
    snapshot: GatewayInventorySnapshot | None = None


class GatewayInventoryAttempt(FrozenModel):
    attempted_at: AwareDatetime
    status: Literal["collected", "error", "uncertain"]
    message: str


class GatewayInventoryState(FrozenModel):
    gateway_origin: str
    attempt: GatewayInventoryAttempt
    last_good: GatewayInventorySnapshot | None = None


CLOCK_SKEW: Final = timedelta(minutes=5)


class _InventoryCapabilities(BaseModel):
    account_inventory: Literal["gateway_bindings_v1"]


SOURCE_PAIRS: Final[frozenset[tuple[SourceName, Origin]]] = frozenset(
    (
        ("credentials", "runtime"),
        ("credentials", "database"),
        ("routes", "runtime"),
        ("routes", "database"),
        ("deployments", "runtime"),
        ("deployments", "database"),
    )
)


def _consistent(inventory: GatewayInventory) -> bool:
    pairs: Final = tuple((source.name, source.origin) for source in inventory.sources)
    if len(pairs) != len(SOURCE_PAIRS) or frozenset(pairs) != SOURCE_PAIRS:
        return False
    collections: Final[dict[str, tuple[InventoryCredential | InventoryRoute | InventoryDeployment, ...]]] = {
        "credentials": inventory.credentials,
        "routes": inventory.routes,
        "deployments": inventory.deployments,
    }
    return all(
        source.records == sum(1 for item in collections[source.name] if item.origin == source.origin)
        and source.rejected
        == sum(1 for item in inventory.rejected if (item.source, item.origin) == (source.name, source.origin))
        and (source.status != "complete" or source.rejected == 0)
        and (source.status != "partial" or source.rejected > 0)
        and (source.status != "unavailable" or source.records == source.rejected == 0)
        for source in inventory.sources
    )


def _collect(client: httpx.Client, gateway_origin: str, now: datetime) -> GatewayInventoryResult:
    caps_code, caps_raw = request_gateway(client, "GET", "/cube_control/capabilities")
    if caps_code != 200:
        return GatewayInventoryResult(status="error", message="Gateway capabilities unavailable")
    _InventoryCapabilities.model_validate_json(caps_raw)
    code, raw = request_gateway(client, "GET", "/cube_control/account_inventory")
    if code != 200:
        return GatewayInventoryResult(status="error", message="Gateway account inventory unavailable")
    inventory: Final = GatewayInventory.model_validate_json(raw)
    if not _consistent(inventory):
        return GatewayInventoryResult(status="error", message="Gateway account inventory is inconsistent")
    if inventory.observed_at > now + CLOCK_SKEW:
        return GatewayInventoryResult(status="error", message="Gateway inventory time is ahead of the collector clock")
    return GatewayInventoryResult(
        status="collected",
        message="Gateway bindings collected; coverage and provider billing are not verified by this snapshot",
        snapshot=GatewayInventorySnapshot(checked_at=now, gateway_origin=gateway_origin, inventory=inventory),
    )


def collect_gateway_inventory(
    gateway_origin: str, master_key: SecretStr, now: datetime, transport: httpx.BaseTransport | None = None
) -> GatewayInventoryResult:
    try:
        require_admin_origin(gateway_origin)
    except ValueError:
        return GatewayInventoryResult(status="error", message="Gateway origin rejected")
    try:
        with gateway_client(gateway_origin, master_key, transport) as client:
            return _collect(client, gateway_origin, now)
    except httpx.HTTPError:
        return GatewayInventoryResult(status="uncertain", message="Gateway request failed")
    except ValidationError:
        return GatewayInventoryResult(status="error", message="Gateway response rejected")
    except ValueError:
        return GatewayInventoryResult(status="error", message="Gateway response exceeds size limit")
