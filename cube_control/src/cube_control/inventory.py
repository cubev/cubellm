from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import chain
from typing import Final, Literal

from pydantic import AwareDatetime

from cube_control.credential_reconciliation import (
    CandidateView,
    CredentialBindingView,
    CredentialFinding,
    CredentialSourceView,
    credential_views,
)
from cube_control.gateway_inventory import SOURCE_PAIRS, InventoryCredential, InventoryRoute, Origin, SourceName
from cube_control.inventory_evidence import (
    GatewayEvidence,
    GatewayInventoryState,
    KnownDeployment,
    KnownRoute,
    Presence,
    SourceEvidence,
)
from cube_control.models import Account, FrozenModel, Observation
from cube_control.store import StoredState
from cube_control.summary import ReadingView, latest_measurements, reading_view

Reference = Literal[
    "catalogue",
    "gateway_credential",
    "gateway_route",
    "observation",
    "usage_event",
    "gateway_record",
    "media_job",
    "manual_entry",
    "credential_binding",
]
AccountGap = Literal[
    "inventory_not_collected",
    "not_in_catalogue",
    "no_gateway_credential",
    "credential_only_in_database",
    "credential_only_in_runtime",
    "credential_unused",
    "binding_not_in_latest_collection",
    "invalid_binding",
    "no_usage_source",
    "no_verified_provider_usage",
    "provider_usage_not_current",
    "no_confirmed_credential_binding",
    "confirmed_credential_not_in_latest_scan",
]
VerifiedUsage = Literal["current", "historical", "none"]
GatewayState = Literal["current", "stale", "last_attempt_failed", "never_collected"]
USAGE_METRICS: Final = frozenset(("spend", "usage"))
STALE_AFTER: Final = timedelta(hours=24)


class GatewayCredential(FrozenModel):
    gateway_origin: str
    credential: InventoryCredential
    observed_at: AwareDatetime
    presence: Presence


class GatewayRoute(FrozenModel):
    gateway_origin: str
    route: InventoryRoute
    observed_at: AwareDatetime
    presence: Presence


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
    usage_evidence: tuple[ReadingView, ...] = ()
    last_observed_at: AwareDatetime | None = None
    last_usage_at: AwareDatetime | None = None
    gaps: tuple[AccountGap, ...] = ()
    confirmed_credentials: tuple[CredentialBindingView, ...] = ()


class GatewayCoverage(FrozenModel):
    gateway_origin: str
    state: GatewayState
    sources_complete: bool
    last_attempt_at: AwareDatetime
    last_attempt_status: Literal["collected", "error", "uncertain"]
    last_attempt_message: str
    last_collected_checked_at: AwareDatetime | None = None
    last_collected_observed_at: AwareDatetime | None = None
    sources: tuple[SourceEvidence, ...] = ()


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
    credential_sources: tuple[CredentialSourceView, ...] = ()
    credential_candidates: tuple[CandidateView, ...] = ()
    credential_bindings: tuple[CredentialBindingView, ...] = ()
    credential_findings: tuple[CredentialFinding, ...] = ()


def _latest(values: Iterable[datetime]) -> datetime | None:
    ordered: Final = tuple(values)
    return max(ordered) if ordered else None


def _sources_complete(evidence: GatewayEvidence | None) -> bool:
    if evidence is None:
        return False
    pairs: Final = tuple((source.name, source.origin) for source in evidence.sources)
    return (
        len(pairs) == len(SOURCE_PAIRS)
        and frozenset(pairs) == SOURCE_PAIRS
        and all(source.status == "complete" for source in evidence.sources)
        and not evidence.rejected
    )


def _coverage(state: GatewayInventoryState, now: datetime) -> GatewayCoverage:
    evidence: Final = state.evidence
    gateway_state: Final[GatewayState] = (
        "never_collected"
        if evidence is None
        else "last_attempt_failed"
        if state.attempt.status != "collected"
        else "stale"
        if now - evidence.observed_at > STALE_AFTER
        else "current"
    )
    return GatewayCoverage(
        gateway_origin=state.gateway_origin,
        state=gateway_state,
        sources_complete=_sources_complete(evidence),
        last_attempt_at=state.attempt.attempted_at,
        last_attempt_status=state.attempt.status,
        last_attempt_message=state.attempt.message,
        last_collected_checked_at=evidence.checked_at if evidence is not None else None,
        last_collected_observed_at=evidence.observed_at if evidence is not None else None,
        sources=evidence.sources if evidence is not None else (),
    )


def _findings(gateway: str, evidence: GatewayEvidence) -> tuple[InventoryFinding, ...]:
    credentials: Final = tuple(item.credential for item in evidence.credentials)
    bound: Final = frozenset((item.credential_name, item.origin) for item in credentials if item.binding == "bound")
    return (
        *(
            InventoryFinding(
                kind="source_unavailable" if source.status == "unavailable" else "source_partial",
                gateway_origin=gateway,
                origin=source.origin,
                source=source.name,
                detail=source.detail,
            )
            for source in evidence.sources
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
            for item in evidence.rejected
        ),
        *(
            InventoryFinding(
                kind="unbound_credential" if item.binding == "unbound" else "invalid_credential",
                gateway_origin=gateway,
                origin=item.origin,
                name=item.credential_name,
                account_id=item.account_id,
            )
            for item in credentials
            if item.binding != "bound"
        ),
        *(
            InventoryFinding(
                kind="unbound_route" if item.route.binding == "unbound" else "invalid_route",
                gateway_origin=gateway,
                origin=item.route.origin,
                name=item.route.route_id or item.route.path,
                account_id=item.route.account_id,
            )
            for item in evidence.routes
            if item.route.binding != "account"
        ),
        *(
            InventoryFinding(
                kind="unattributed_deployment",
                gateway_origin=gateway,
                origin=item.deployment.origin,
                name=item.deployment.model_name,
                detail=item.deployment.auth,
            )
            for item in evidence.deployments
            if (item.deployment.credential_name, item.deployment.origin) not in bound
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
    verified_usage: VerifiedUsage,
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
        ("binding_not_in_latest_collection", bool(bound) and all(item.presence == "retained" for item in bound)),
        ("invalid_binding", invalid),
        ("no_usage_source", not has_usage),
        ("no_verified_provider_usage", verified_usage == "none"),
        ("provider_usage_not_current", verified_usage == "historical"),
    )
    return tuple(gap for gap, applies in candidates if applies)


def _references(
    gateway: str, routes: tuple[KnownRoute, ...], deployments: tuple[KnownDeployment, ...]
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (gateway, origin, name)
        for origin, name in chain(
            ((item.route.origin, item.route.credential_name) for item in routes),
            ((item.deployment.origin, item.deployment.credential_name) for item in deployments),
        )
        if name is not None
    )


def _gateway_credentials(gateway: str, evidence: GatewayEvidence) -> Iterable[GatewayCredential]:
    return (
        GatewayCredential(
            gateway_origin=gateway, credential=item.credential, observed_at=item.observed_at, presence=item.presence
        )
        for item in evidence.credentials
    )


def _gateway_routes(gateway: str, evidence: GatewayEvidence) -> Iterable[GatewayRoute]:
    return (
        GatewayRoute(gateway_origin=gateway, route=item.route, observed_at=item.observed_at, presence=item.presence)
        for item in evidence.routes
    )


def _history_view(view: ReadingView, latest: tuple[Observation, ...]) -> ReadingView:
    return ReadingView(reading=view.reading, freshness=view.freshness, current=view.current and view.reading in latest)


def account_inventory(stored: StoredState, now: datetime) -> AccountInventoryReport:
    observations: Final = stored.observations
    events: Final = stored.events
    gateway_records: Final = stored.gateway_records
    gateways: Final = stored.gateway_inventories
    collected: Final = tuple((state.gateway_origin, state.evidence) for state in gateways if state.evidence is not None)
    credentials: Final = tuple(
        chain.from_iterable(_gateway_credentials(origin, evidence) for origin, evidence in collected)
    )
    routes: Final = tuple(chain.from_iterable(_gateway_routes(origin, evidence) for origin, evidence in collected))
    referenced: Final = frozenset(
        chain.from_iterable(
            _references(origin, evidence.routes, evidence.deployments) for origin, evidence in collected
        )
    )
    catalogued: Final = {account.id: account for account in stored.accounts}
    credential_sources, credential_candidates, credential_bindings, credential_findings = credential_views(
        stored.credential_inventories,
        stored.credential_bindings,
        frozenset(str(account.provider) for account in stored.accounts),
        now,
    )
    account_ids: Final = sorted(
        frozenset(
            chain(
                catalogued,
                (item.credential.account_id for item in credentials if item.credential.account_id is not None),
                (item.route.account_id for item in routes if item.route.account_id is not None),
                (item.account_id for item in observations),
                (item.account_id for item in events),
                (record.account_id for record in gateway_records if record.account_id is not None),
                (job.account_id for job in stored.jobs),
                (entry.command.account_id for entry in stored.manual_entries),
                (
                    entry.command.account_id
                    for entry in stored.credential_bindings
                    if entry.command.account_id is not None
                ),
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
            ("media_job", any(job.account_id == account_id for job in stored.jobs)),
            ("manual_entry", any(entry.command.account_id == account_id for entry in stored.manual_entries)),
            (
                "credential_binding",
                any(entry.command.account_id == account_id for entry in stored.credential_bindings),
            ),
        )
        latest: Final = latest_measurements(account_id, stored.observations)
        evidence: Final = tuple(
            _history_view(reading_view(item, stored.observations, now), latest)
            for item in sorted(usage_observations, key=lambda item: (item.observed_at, item.collected_at))
        )
        verified: Final = tuple(
            view.current
            for view in evidence
            if view.reading.source == "provider_api" and view.reading.status == "known"
        )
        invalid: Final = any(item.credential.binding == "invalid" for item in own_credentials) or any(
            item.route.binding == "invalid" for item in own_routes
        )
        confirmed: Final = tuple(item for item in credential_bindings if item.account_id == account_id)
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
            usage_evidence=evidence,
            last_observed_at=_latest(item.observed_at for item in own_observations),
            last_usage_at=_latest(
                chain((item.occurred_at for item in own_events), (record.occurred_at for record in own_records))
            ),
            gaps=(
                *_gaps(
                    account,
                    own_credentials,
                    referenced,
                    bool(collected),
                    bool(own_events or own_records)
                    or any(item.status in ("known", "estimated", "manual") for item in usage_observations),
                    "current" if any(verified) else "historical" if verified else "none",
                    invalid,
                ),
                *(
                    ("no_confirmed_credential_binding",)
                    if credential_candidates and account is not None and account.kind == "api" and not confirmed
                    else ()
                ),
                *(
                    ("confirmed_credential_not_in_latest_scan",)
                    if confirmed and all(item.candidate != "current" for item in confirmed)
                    else ()
                ),
            ),
            confirmed_credentials=confirmed,
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
        findings=(
            *chain.from_iterable(_findings(origin, evidence) for origin, evidence in collected),
            *_conflicts(credentials),
        ),
        credential_sources=credential_sources,
        credential_candidates=credential_candidates,
        credential_bindings=credential_bindings,
        credential_findings=credential_findings,
    )
