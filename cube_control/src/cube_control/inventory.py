from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import chain
from typing import Final, Literal

from pydantic import AwareDatetime

from cube_control.gateway_inventory import (
    SOURCE_PAIRS,
    GatewayInventoryState,
    InventoryCredential,
    InventoryDeployment,
    InventoryRoute,
    Origin,
    SourceName,
)
from cube_control.models import Account, FrozenModel, GatewayRecord, Observation, UsageEvent

Reference = Literal["catalogue", "gateway_credential", "gateway_route", "observation", "usage_event", "gateway_record"]
AccountGap = Literal[
    "inventory_not_collected",
    "not_in_catalogue",
    "no_gateway_credential",
    "credential_only_in_database",
    "credential_only_in_runtime",
    "credential_unused",
    "invalid_binding",
    "no_usage_source",
    "no_verified_provider_usage",
]
GatewayState = Literal["current", "stale", "last_attempt_failed", "never_collected"]
USAGE_METRICS: Final = frozenset(("spend", "usage"))
STALE_AFTER: Final = timedelta(hours=24)


class GatewayCredential(FrozenModel):
    gateway_origin: str
    credential: InventoryCredential


class GatewayRoute(FrozenModel):
    gateway_origin: str
    route: InventoryRoute


class UsageEvidence(FrozenModel):
    source: str
    metric: str
    status: str
    observed_at: AwareDatetime


class AccountInventoryEntry(FrozenModel):
    account_id: str
    provider: str | None = None
    name: str | None = None
    kind: Literal["api", "subscription"] | None = None
    state: str | None = None
    references: tuple[Reference, ...]
    credentials: tuple[GatewayCredential, ...] = ()
    routes: tuple[GatewayRoute, ...] = ()
    observation_sources: tuple[str, ...] = ()
    usage_evidence: tuple[UsageEvidence, ...] = ()
    last_observed_at: AwareDatetime | None = None
    last_usage_at: AwareDatetime | None = None
    gaps: tuple[AccountGap, ...] = ()


class GatewayCoverage(FrozenModel):
    gateway_origin: str
    state: GatewayState
    sources_complete: bool
    last_attempt_at: AwareDatetime
    last_attempt_status: Literal["collected", "error", "uncertain"]
    last_attempt_message: str
    last_good_checked_at: AwareDatetime | None = None
    last_good_observed_at: AwareDatetime | None = None


class InventoryFinding(FrozenModel):
    kind: Literal[
        "unbound_credential",
        "unbound_route",
        "invalid_credential",
        "invalid_route",
        "unattributed_deployment",
        "binding_conflict",
        "rejected_record",
        "source_partial",
        "source_unavailable",
    ]
    gateway_origin: str | None = None
    origin: Origin | None = None
    source: SourceName | None = None
    name: str | None = None
    account_id: str | None = None
    detail: str = ""


class AccountInventoryReport(FrozenModel):
    generated_at: AwareDatetime
    coverage: Literal["not_collected", "partial", "configured_sources_complete"]
    gateways: tuple[GatewayCoverage, ...]
    accounts: tuple[AccountInventoryEntry, ...]
    findings: tuple[InventoryFinding, ...]


def _latest(values: Iterable[datetime]) -> datetime | None:
    ordered: Final = tuple(values)
    return max(ordered) if ordered else None


def _sources_complete(state: GatewayInventoryState) -> bool:
    if state.last_good is None:
        return False
    inventory: Final = state.last_good.inventory
    pairs: Final = tuple((source.name, source.origin) for source in inventory.sources)
    return (
        len(pairs) == len(SOURCE_PAIRS)
        and frozenset(pairs) == SOURCE_PAIRS
        and all(source.status == "complete" for source in inventory.sources)
        and not inventory.rejected
    )


def _coverage(state: GatewayInventoryState, now: datetime) -> GatewayCoverage:
    good: Final = state.last_good
    gateway_state: Final[GatewayState] = (
        "never_collected"
        if good is None
        else "last_attempt_failed"
        if state.attempt.status != "collected"
        else "stale"
        if now - good.inventory.observed_at > STALE_AFTER
        else "current"
    )
    return GatewayCoverage(
        gateway_origin=state.gateway_origin,
        state=gateway_state,
        sources_complete=_sources_complete(state),
        last_attempt_at=state.attempt.attempted_at,
        last_attempt_status=state.attempt.status,
        last_attempt_message=state.attempt.message,
        last_good_checked_at=good.checked_at if good is not None else None,
        last_good_observed_at=good.inventory.observed_at if good is not None else None,
    )


def _findings(state: GatewayInventoryState) -> tuple[InventoryFinding, ...]:
    if state.last_good is None:
        return ()
    gateway: Final = state.gateway_origin
    inventory: Final = state.last_good.inventory
    bound: Final = frozenset(
        (item.credential_name, item.origin) for item in inventory.credentials if item.binding == "bound"
    )
    return (
        *(
            InventoryFinding(
                kind="source_unavailable" if source.status == "unavailable" else "source_partial",
                gateway_origin=gateway,
                origin=source.origin,
                source=source.name,
                detail=source.detail,
            )
            for source in inventory.sources
            if source.status != "complete"
        ),
        *(
            InventoryFinding(
                kind="rejected_record",
                gateway_origin=gateway,
                origin=item.origin,
                source=item.source,
                name=item.name,
                detail=item.reason,
            )
            for item in inventory.rejected
        ),
        *(
            InventoryFinding(
                kind="unbound_credential" if item.binding == "unbound" else "invalid_credential",
                gateway_origin=gateway,
                origin=item.origin,
                name=item.credential_name,
                account_id=item.account_id,
            )
            for item in inventory.credentials
            if item.binding != "bound"
        ),
        *(
            InventoryFinding(
                kind="unbound_route" if item.binding == "unbound" else "invalid_route",
                gateway_origin=gateway,
                origin=item.origin,
                name=item.route_id or item.path,
                account_id=item.account_id,
            )
            for item in inventory.routes
            if item.binding != "account"
        ),
        *(
            InventoryFinding(
                kind="unattributed_deployment",
                gateway_origin=gateway,
                origin=item.origin,
                name=item.model_name,
                detail=item.auth,
            )
            for item in inventory.deployments
            if (item.credential_name, item.origin) not in bound
        ),
    )


def _conflicts(credentials: tuple[GatewayCredential, ...]) -> tuple[InventoryFinding, ...]:
    bound: Final = tuple(
        (item.gateway_origin, item.credential.credential_name, item.credential.account_id)
        for item in credentials
        if item.credential.binding == "bound"
    )
    conflicting: Final = sorted(
        frozenset(
            (gateway, name)
            for gateway, name, account in bound
            if any(
                (other_gateway, other_name) == (gateway, name) and other != account
                for other_gateway, other_name, other in bound
            )
        )
    )
    return tuple(
        InventoryFinding(kind="binding_conflict", gateway_origin=gateway, name=name) for gateway, name in conflicting
    )


def _gaps(
    account: Account | None,
    credentials: tuple[GatewayCredential, ...],
    referenced: frozenset[tuple[str, str, str]],
    collected: bool,
    has_usage: bool,
    verified_usage: bool,
    invalid: bool,
) -> tuple[AccountGap, ...]:
    bound: Final = tuple(item for item in credentials if item.credential.binding == "bound")
    database: Final = frozenset(
        (item.gateway_origin, item.credential.credential_name) for item in bound if item.credential.origin == "database"
    )
    runtime: Final = frozenset(
        (item.gateway_origin, item.credential.credential_name) for item in bound if item.credential.origin == "runtime"
    )
    unused: Final = any(
        (item.gateway_origin, item.credential.origin, item.credential.credential_name) not in referenced
        for item in bound
    )
    candidates: Final[tuple[tuple[AccountGap, bool], ...]] = (
        ("inventory_not_collected", not collected),
        ("not_in_catalogue", account is None),
        ("no_gateway_credential", collected and not bound and (account is None or account.kind == "api")),
        ("credential_only_in_database", bool(database - runtime)),
        ("credential_only_in_runtime", bool(runtime - database)),
        ("credential_unused", unused),
        ("invalid_binding", invalid),
        ("no_usage_source", not has_usage),
        ("no_verified_provider_usage", not verified_usage),
    )
    return tuple(gap for gap, applies in candidates if applies)


def _references(
    gateway: str, routes: tuple[InventoryRoute, ...], deployments: tuple[InventoryDeployment, ...]
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (gateway, origin, name)
        for origin, name in chain(
            ((route.origin, route.credential_name) for route in routes),
            ((deployment.origin, deployment.credential_name) for deployment in deployments),
        )
        if name is not None
    )


def account_inventory(
    accounts: tuple[Account, ...],
    observations: tuple[Observation, ...],
    events: tuple[UsageEvent, ...],
    gateway_records: tuple[GatewayRecord, ...],
    gateways: tuple[GatewayInventoryState, ...],
    now: datetime,
) -> AccountInventoryReport:
    collected: Final = tuple(state for state in gateways if state.last_good is not None)
    credentials: Final = tuple(
        GatewayCredential(gateway_origin=state.gateway_origin, credential=item)
        for state in collected
        if state.last_good is not None
        for item in state.last_good.inventory.credentials
    )  # comprehension-ok: flattening per-gateway snapshots keeps each record's gateway origin
    routes: Final = tuple(
        GatewayRoute(gateway_origin=state.gateway_origin, route=item)
        for state in collected
        if state.last_good is not None
        for item in state.last_good.inventory.routes
    )  # comprehension-ok: flattening per-gateway snapshots keeps each record's gateway origin
    referenced: Final = frozenset(
        chain.from_iterable(
            _references(state.gateway_origin, state.last_good.inventory.routes, state.last_good.inventory.deployments)
            for state in collected
            if state.last_good is not None
        )
    )
    catalogued: Final = {account.id: account for account in accounts}
    account_ids: Final = sorted(
        frozenset(
            chain(
                catalogued,
                (item.credential.account_id for item in credentials if item.credential.account_id is not None),
                (item.route.account_id for item in routes if item.route.account_id is not None),
                (item.account_id for item in observations),
                (item.account_id for item in events),
                (record.account_id for record in gateway_records if record.account_id is not None),
            )
        )
    )

    def entry(account_id: str) -> AccountInventoryEntry:
        account: Final = catalogued.get(account_id)
        own_credentials: Final = tuple(item for item in credentials if item.credential.account_id == account_id)
        own_routes: Final = tuple(item for item in routes if item.route.account_id == account_id)
        own_observations: Final = tuple(item for item in observations if item.account_id == account_id)
        usage_observations: Final = tuple(item for item in own_observations if item.metric in USAGE_METRICS)
        own_events: Final = tuple(item for item in events if item.account_id == account_id)
        own_records: Final = tuple(item for item in gateway_records if item.account_id == account_id)
        reference_flags: Final[tuple[tuple[Reference, bool], ...]] = (
            ("catalogue", account is not None),
            ("gateway_credential", bool(own_credentials)),
            ("gateway_route", bool(own_routes)),
            ("observation", bool(own_observations)),
            ("usage_event", bool(own_events)),
            ("gateway_record", bool(own_records)),
        )
        invalid: Final = any(item.credential.binding == "invalid" for item in own_credentials) or any(
            item.route.binding == "invalid" for item in own_routes
        )
        return AccountInventoryEntry(
            account_id=account_id,
            provider=str(account.provider) if account is not None else None,
            name=account.name if account is not None else None,
            kind=account.kind if account is not None else None,
            state=account.state if account is not None else None,
            references=tuple(reference for reference, present in reference_flags if present),
            credentials=own_credentials,
            routes=own_routes,
            observation_sources=tuple(sorted(frozenset(item.source for item in own_observations))),
            usage_evidence=tuple(
                UsageEvidence(source=item.source, metric=item.metric, status=item.status, observed_at=item.observed_at)
                for item in sorted(usage_observations, key=lambda item: item.observed_at)
            ),
            last_observed_at=_latest(item.observed_at for item in own_observations),
            last_usage_at=_latest(
                chain((item.occurred_at for item in own_events), (record.occurred_at for record in own_records))
            ),
            gaps=_gaps(
                account,
                own_credentials,
                referenced,
                bool(collected),
                bool(own_events or own_records)
                or any(item.status in ("known", "estimated", "manual") for item in usage_observations),
                any(item.source == "provider_api" and item.status == "known" for item in usage_observations),
                invalid,
            ),
        )

    coverage: Final = tuple(_coverage(state, now) for state in gateways)
    return AccountInventoryReport(
        generated_at=now,
        coverage="not_collected"
        if not collected
        else "configured_sources_complete"
        if all(item.state == "current" and item.sources_complete for item in coverage)
        else "partial",
        gateways=coverage,
        accounts=tuple(map(entry, account_ids)),
        findings=(*chain.from_iterable(map(_findings, collected)), *_conflicts(credentials)),
    )
