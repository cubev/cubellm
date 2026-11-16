from datetime import datetime
from typing import Final

from cube_control.inventory_evidence import GatewayInventoryState, merge_evidence
from cube_control.gateway_inventory import (
    SOURCE_PAIRS,
    GatewayInventory,
    GatewayInventoryAttempt,
    GatewayInventorySnapshot,
    InventoryCredential,
    InventoryDeployment,
    InventoryRejection,
    InventoryRoute,
    InventorySource,
    Origin,
    SourceName,
)


def _source(
    name: SourceName,
    origin: Origin,
    records: int,
    rejected: int,
    unavailable: frozenset[tuple[SourceName, Origin]],
) -> InventorySource:
    if (name, origin) in unavailable:
        return InventorySource(name=name, origin=origin, status="unavailable", detail="database_read_failed")
    return InventorySource(
        name=name, origin=origin, status="partial" if rejected else "complete", records=records, rejected=rejected
    )


def gateway_inventory(
    observed_at: datetime,
    credentials: tuple[InventoryCredential, ...] = (),
    routes: tuple[InventoryRoute, ...] = (),
    deployments: tuple[InventoryDeployment, ...] = (),
    rejected: tuple[InventoryRejection, ...] = (),
    unavailable: frozenset[tuple[SourceName, Origin]] = frozenset(),
) -> GatewayInventory:
    origins: Final[dict[SourceName, tuple[Origin, ...]]] = {
        "credentials": tuple(item.origin for item in credentials),
        "routes": tuple(item.origin for item in routes),
        "deployments": tuple(item.origin for item in deployments),
    }
    return GatewayInventory(
        schema_version=1,
        observed_at=observed_at,
        sources=tuple(
            _source(
                name,
                origin,
                origins[name].count(origin),
                sum(1 for item in rejected if (item.source, item.origin) == (name, origin)),
                unavailable,
            )
            for name, origin in sorted(SOURCE_PAIRS)
        ),
        credentials=credentials,
        routes=routes,
        deployments=deployments,
        rejected=rejected,
    )


def snapshot(gateway_origin: str, checked_at: datetime, inventory: GatewayInventory) -> GatewayInventorySnapshot:
    return GatewayInventorySnapshot(checked_at=checked_at, gateway_origin=gateway_origin, inventory=inventory)


def collected(gateway_origin: str, at: datetime, inventory: GatewayInventory) -> GatewayInventoryState:
    return GatewayInventoryState(
        gateway_origin=gateway_origin,
        attempt=GatewayInventoryAttempt(attempted_at=at, status="collected", message="collected"),
        evidence=merge_evidence(None, snapshot(gateway_origin, at, inventory)),
    )


def bound(origin: Origin, name: str, account_id: str) -> InventoryCredential:
    return InventoryCredential(
        origin=origin,
        credential_name=name,
        account_id=account_id,
        allowed_origins=("https://api.example",),
        binding="bound",
    )
