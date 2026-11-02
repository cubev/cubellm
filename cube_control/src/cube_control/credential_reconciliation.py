from datetime import datetime
from itertools import chain
from typing import Final, Literal

from pydantic import AwareDatetime

from cube_control.credential_binding import BindingOrigin, CredentialBindingEntry, binding_revision, bound_account
from cube_control.credential_inventory import (
    SCAN_STALE_AFTER,
    SERVICE_PROVIDER_HINTS,
    CredentialCandidate,
    CredentialInventorySnapshot,
    CredentialInventoryState,
    CredentialKind,
    ImportStatus,
    public_reference,
)
from cube_control.models import FrozenModel

UNASSIGNED_SERVICE: Final = "Unzugeordnet"
Freshness = Literal["fresh", "stale"]


class HostCoverage(FrozenModel):
    host: str
    scanned_at: AwareDatetime
    presence: Literal["latest_scan", "retained"]
    scope_id: str
    roots: int
    files_read: int
    unreadable: int
    size_skips: int
    errors: int
    verification_errors: int
    complete: bool


class CredentialSourceView(FrozenModel):
    source_id: str
    last_attempt_at: AwareDatetime
    last_attempt_status: ImportStatus
    last_attempt_message: str
    scanned_at: AwareDatetime | None = None
    imported_at: AwareDatetime | None = None
    scan_freshness: Freshness | None = None
    scan_complete: bool = False
    hosts: tuple[HostCoverage, ...] = ()
    candidates: int = 0
    retained_candidates: int = 0
    quarantined: int = 0
    validity: Literal["not_checked"] = "not_checked"


class CandidateView(FrozenModel):
    source_id: str
    reference: str
    service: str
    provider_hint: str | None = None
    state: Literal["current", "archived_only"]
    binding: Literal["unbound", "operator_confirmed"] = "unbound"
    bound_account_id: str | None = None
    binding_revision: int = 0
    binding_actor: str | None = None
    binding_changed_at: AwareDatetime | None = None
    presence: Literal["latest_scan", "retained"]
    last_observed_at: AwareDatetime
    retained_locations: int
    kinds: tuple[CredentialKind, ...]
    hosts: tuple[str, ...]
    projects: tuple[str, ...]
    variables: tuple[str, ...]
    current_locations: int
    archive_locations: int
    shared_with: tuple[str, ...] = ()
    validity: Literal["not_checked"]
    scanned_at: AwareDatetime
    scan_freshness: Freshness


class BindingHistoryEntry(FrozenModel):
    revision: int
    action: Literal["bind", "unbind"]
    account_id: str | None = None
    actor: str
    changed_at: AwareDatetime
    origin: BindingOrigin | None = None


class CredentialBindingView(FrozenModel):
    reference: str
    account_id: str | None = None
    revision: int
    candidate: Literal["current", "retained", "missing"]
    origin: BindingOrigin | None = None
    history: tuple[BindingHistoryEntry, ...]


class CredentialFinding(FrozenModel):
    kind: Literal[
        "unassigned_candidate",
        "provider_hint_without_account",
        "archived_candidate",
        "unbound_candidate",
        "binding_candidate_missing",
        "quarantined_record",
        "incomplete_scan",
    ]
    source_id: str
    reference: str | None = None
    service: str | None = None
    host: str | None = None
    detail: str = ""


def _freshness(snapshot: CredentialInventorySnapshot, now: datetime) -> Freshness:
    return "stale" if now - snapshot.scanned_at > SCAN_STALE_AFTER else "fresh"


def _candidate(snapshot: CredentialInventorySnapshot, item: CredentialCandidate, now: datetime) -> CandidateView:
    current: Final = sum(1 for location in item.locations if location.source_class == "current")
    retained: Final = sum(1 for location in item.locations if location.presence == "retained")
    return CandidateView(
        source_id=snapshot.source_id,
        reference=public_reference(snapshot.source_id, item.fingerprint),
        service=item.service,
        provider_hint=SERVICE_PROVIDER_HINTS.get(item.service),
        state="current" if current else "archived_only",
        presence="retained" if retained == len(item.locations) else "latest_scan",
        last_observed_at=max(location.observed_at for location in item.locations),
        retained_locations=retained,
        kinds=item.kinds,
        hosts=tuple(sorted(frozenset(location.host for location in item.locations))),
        projects=tuple(sorted(frozenset(location.project for location in item.locations))),
        variables=tuple(sorted(frozenset(location.variable for location in item.locations))),
        current_locations=current,
        archive_locations=len(item.locations) - current,
        shared_with=item.shared_with,
        validity=snapshot.validity,
        scanned_at=snapshot.scanned_at,
        scan_freshness=_freshness(snapshot, now),
    )


def _source(state: CredentialInventoryState, now: datetime) -> CredentialSourceView:
    attempt: Final = state.attempt
    snapshot: Final = state.evidence
    base: Final = CredentialSourceView(
        source_id=state.source_id,
        last_attempt_at=attempt.attempted_at,
        last_attempt_status=attempt.status,
        last_attempt_message=attempt.message,
    )
    if snapshot is None:
        return base
    hosts: Final = tuple(
        HostCoverage(
            host=host.host,
            scanned_at=host.scanned_at,
            presence=host.presence,
            scope_id=host.scope_id,
            roots=len(host.roots),
            files_read=host.files_read,
            unreadable=host.unreadable,
            size_skips=host.size_skips,
            errors=host.errors,
            verification_errors=host.verification_errors,
            complete=host.complete and host.presence == "latest_scan",
        )
        for host in snapshot.hosts
    )
    return base.model_copy(
        update={
            "scanned_at": snapshot.scanned_at,
            "imported_at": snapshot.imported_at,
            "scan_freshness": _freshness(snapshot, now),
            "scan_complete": bool(hosts) and all(host.complete for host in hosts) and not snapshot.quarantined,
            "hosts": hosts,
            "candidates": len(snapshot.candidates),
            "retained_candidates": sum(
                1 for item in snapshot.candidates if all(location.presence == "retained" for location in item.locations)
            ),
            "quarantined": len(snapshot.quarantined),
            "validity": snapshot.validity,
        }
    )


def _candidate_finding(item: CandidateView, catalogued_providers: frozenset[str]) -> CredentialFinding | None:
    if item.service == UNASSIGNED_SERVICE:
        return CredentialFinding(
            kind="unassigned_candidate",
            source_id=item.source_id,
            reference=item.reference,
            detail="Dienst und Konto unbekannt",
        )
    if item.provider_hint is not None and item.provider_hint not in catalogued_providers:
        return CredentialFinding(
            kind="provider_hint_without_account",
            source_id=item.source_id,
            reference=item.reference,
            service=item.service,
            detail="Dienstname deutet auf einen Anbieter ohne Katalogkonto; Kontozugehörigkeit unbewiesen",
        )
    if item.state == "archived_only":
        return CredentialFinding(
            kind="archived_candidate",
            source_id=item.source_id,
            reference=item.reference,
            service=item.service,
            detail="Nur in Archiv- oder Exportfundstellen",
        )
    if item.binding == "operator_confirmed":
        return None
    return CredentialFinding(
        kind="unbound_candidate",
        source_id=item.source_id,
        reference=item.reference,
        service=item.service,
        detail="Keine bestätigte Kontobindung"
        + (f"; Dienstname deutet auf {item.provider_hint}" if item.provider_hint else "; Anbieter nicht zugeordnet"),
    )


def _scan_findings(snapshot: CredentialInventorySnapshot) -> tuple[CredentialFinding, ...]:
    return (
        *(
            CredentialFinding(
                kind="quarantined_record",
                source_id=snapshot.source_id,
                reference=public_reference(snapshot.source_id, item.fingerprint) if item.fingerprint else None,
                service=item.service or None,
                detail=item.reason,
            )
            for item in snapshot.quarantined
        ),
        *(
            CredentialFinding(
                kind="incomplete_scan",
                source_id=snapshot.source_id,
                host=host.host,
                detail=f"{host.unreadable} unlesbar, {host.size_skips} übersprungen, {host.errors + host.verification_errors} Fehler",
            )
            for host in snapshot.hosts
            if not host.complete
        ),
    )


def _with_binding(item: CandidateView, entries: tuple[CredentialBindingEntry, ...]) -> CandidateView:
    account: Final = bound_account(entries)
    latest: Final = max(entries, key=lambda entry: entry.revision, default=None)
    return item.model_copy(
        update={
            "binding": "operator_confirmed" if account is not None else "unbound",
            "bound_account_id": account,
            "binding_revision": binding_revision(entries),
            "binding_actor": latest.actor if latest is not None else None,
            "binding_changed_at": latest.received_at if latest is not None else None,
        }
    )


def _binding_view(
    reference: str, entries: tuple[CredentialBindingEntry, ...], candidates: dict[str, CandidateView]
) -> CredentialBindingView:
    candidate: Final = candidates.get(reference)
    ordered: Final = sorted(entries, key=lambda entry: entry.revision)
    return CredentialBindingView(
        reference=reference,
        account_id=bound_account(entries),
        revision=binding_revision(entries),
        candidate="missing" if candidate is None else "retained" if candidate.presence == "retained" else "current",
        origin=next((entry.origin for entry in reversed(ordered) if entry.origin is not None), None),
        history=tuple(
            BindingHistoryEntry(
                revision=entry.revision,
                action=entry.command.action,
                account_id=entry.command.account_id,
                actor=entry.actor,
                changed_at=entry.received_at,
                origin=entry.origin,
            )
            for entry in ordered
        ),
    )


def credential_views(
    states: tuple[CredentialInventoryState, ...],
    bindings: tuple[CredentialBindingEntry, ...],
    catalogued_providers: frozenset[str],
    now: datetime,
) -> tuple[
    tuple[CredentialSourceView, ...],
    tuple[CandidateView, ...],
    tuple[CredentialBindingView, ...],
    tuple[CredentialFinding, ...],
]:
    snapshots: Final = tuple(state.evidence for state in states if state.evidence is not None)
    by_reference: Final = {
        reference: tuple(entry for entry in bindings if entry.command.reference == reference)
        for reference in sorted(frozenset(entry.command.reference for entry in bindings))
    }
    candidates: Final = tuple(
        _with_binding(item, by_reference.get(item.reference, ()))
        for item in chain.from_iterable(
            (_candidate(snapshot, candidate, now) for candidate in snapshot.candidates) for snapshot in snapshots
        )
    )
    indexed: Final = {item.reference: item for item in candidates}
    binding_views: Final = tuple(
        _binding_view(reference, entries, indexed) for reference, entries in by_reference.items()
    )
    return (
        tuple(_source(state, now) for state in states),
        candidates,
        binding_views,
        (
            *(
                finding
                for finding in (_candidate_finding(item, catalogued_providers) for item in candidates)
                if finding is not None
            ),
            *(
                CredentialFinding(
                    kind="binding_candidate_missing",
                    source_id=view.origin.source_id if view.origin is not None else "",
                    reference=view.reference,
                    service=view.origin.service if view.origin is not None else None,
                    detail=f"Zuordnung zu {view.account_id} besteht, Fund im letzten Scan nicht mehr enthalten",
                )
                for view in binding_views
                if view.account_id is not None and view.candidate == "missing"
            ),
            *chain.from_iterable(map(_scan_findings, snapshots)),
        ),
    )
