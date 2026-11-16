from collections import Counter
from datetime import datetime, timedelta
from hashlib import sha256
from itertools import chain
from types import MappingProxyType
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from cube_control.models import FrozenModel, Identifier

MaterialStatus = Literal["value_present", "reference", "template"]
CredentialKind = Literal[
    "api_key_or_token",
    "connection_string",
    "encrypted_material",
    "local_application_secret",
    "public_firebase_client_configuration",
]
SourceClass = Literal["current", "archive_or_export"]
QuarantineReason = Literal[
    "duplicate_service",
    "duplicate_candidate",
    "register_only_candidate",
    "plan_only_candidate",
    "unexpected_validity",
    "candidate_without_locations",
]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{20}$")]
Presence = Literal["latest_scan", "retained"]
SCAN_STALE_AFTER: Final = timedelta(days=7)
MAX_SOURCE_BYTES: Final = 64 * 1024 * 1024
SERVICE_PROVIDER_HINTS: Final = MappingProxyType(
    {
        "OpenAI": "openai",
        "Anthropic": "anthropic",
        "OpenRouter": "openrouter",
        "xAI": "xai",
        "ElevenLabs": "elevenlabs",
        "GitHub": "github",
    }
)


class HostScan(FrozenModel):
    host: Identifier
    roots: tuple[Annotated[str, Field(max_length=2000)], ...]
    files_read: Annotated[int, Field(ge=0)]
    unreadable: Annotated[int, Field(ge=0)]
    size_skips: Annotated[int, Field(ge=0)]
    errors: Annotated[int, Field(ge=0)]
    verification_errors: Annotated[int, Field(ge=0)]
    secret_values_exported: bool
    scanned_at: AwareDatetime
    presence: Presence = "latest_scan"

    @property
    def scope_id(self) -> str:
        return "scope-" + sha256("\0".join(self.roots).encode()).hexdigest()[:12] if self.roots else "scope-unknown"

    def covers(self, path: str) -> bool:
        return any(path == root or path.startswith(root.rstrip("/") + "/") for root in self.roots)

    @property
    def complete(self) -> bool:
        return bool(self.roots) and not (
            self.unreadable or self.size_skips or self.errors or self.verification_errors or self.secret_values_exported
        )


class ServiceSummary(FrozenModel):
    service: Annotated[str, Field(min_length=1, max_length=100)]
    status: MaterialStatus
    hosts: tuple[Identifier, ...]
    current_value_locations: Annotated[int, Field(ge=0)]
    archive_value_locations: Annotated[int, Field(ge=0)]


class CandidateLocation(FrozenModel):
    host: Identifier
    project: Annotated[str, Field(max_length=500)]
    variable: Annotated[str, Field(max_length=200)]
    path: Annotated[str, Field(max_length=2000)]
    source_class: SourceClass
    observed_at: AwareDatetime
    presence: Presence = "latest_scan"


class CredentialCandidate(FrozenModel):
    fingerprint: Fingerprint
    service: Annotated[str, Field(min_length=1, max_length=100)]
    kinds: tuple[CredentialKind, ...]
    locations: Annotated[tuple[CandidateLocation, ...], Field(min_length=1)]
    shared_with: tuple[str, ...] = ()


class Quarantined(FrozenModel):
    reason: QuarantineReason
    service: str
    fingerprint: Fingerprint | None = None


class CredentialInventorySnapshot(FrozenModel):
    source_id: Identifier
    scanned_at: AwareDatetime
    imported_at: AwareDatetime
    validity: Literal["not_checked"] = "not_checked"
    hosts: tuple[HostScan, ...]
    services: tuple[ServiceSummary, ...]
    candidates: tuple[CredentialCandidate, ...]
    quarantined: tuple[Quarantined, ...] = ()


def public_reference(source_id: str, fingerprint: str) -> str:
    return "cand-" + sha256(f"cube-control-candidate\0{source_id}\0{fingerprint}".encode()).hexdigest()[:16]


class _Verification(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    errors: tuple[object, ...]
    secret_values_exported: bool


class _Host(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: Identifier
    roots: tuple[str, ...]
    files_read: int
    unreadable: int
    size_skips: int
    errors: tuple[object, ...]
    verification: _Verification


class _Service(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    service: str
    status: MaterialStatus
    hosts: tuple[Identifier, ...]
    credential_ids: tuple[Fingerprint, ...]
    current_value_locations: int
    archive_value_locations: int


class _Shared(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    credential_id: Fingerprint
    services: tuple[str, ...]


class _Register(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    created: AwareDatetime
    hosts: tuple[_Host, ...]
    services: tuple[_Service, ...]
    reused_credentials: tuple[_Shared, ...] = ()


class _Location(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    host: Identifier
    project: str
    variable: str
    path: str
    source_class: SourceClass


class _PlanEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: Fingerprint
    service: str
    kind: tuple[CredentialKind, ...]
    validity: str
    locations: tuple[_Location, ...]


class _Plan(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    credentials: tuple[_PlanEntry, ...]


def _quarantine(register: _Register, plan: _Plan) -> tuple[Quarantined, ...]:
    service_counts: Final = Counter(item.service for item in register.services)
    candidate_counts: Final = Counter(item.id for item in plan.credentials)
    registered: Final = frozenset(
        chain.from_iterable(
            ((item.service, fingerprint) for fingerprint in item.credential_ids) for item in register.services
        )
    )
    planned: Final = frozenset((item.service, item.id) for item in plan.credentials)
    return (
        *(Quarantined(reason="duplicate_service", service=name) for name, count in service_counts.items() if count > 1),
        *(
            Quarantined(reason="duplicate_candidate", service="", fingerprint=fingerprint)
            for fingerprint, count in candidate_counts.items()
            if count > 1
        ),
        *(
            Quarantined(reason="register_only_candidate", service=service, fingerprint=fingerprint)
            for service, fingerprint in sorted(registered - planned)
        ),
        *(
            Quarantined(reason="plan_only_candidate", service=service, fingerprint=fingerprint)
            for service, fingerprint in sorted(planned - registered)
        ),
        *(
            Quarantined(reason="unexpected_validity", service=item.service, fingerprint=item.id)
            for item in plan.credentials
            if item.validity != "not_checked"
        ),
        *(
            Quarantined(reason="candidate_without_locations", service=item.service, fingerprint=item.id)
            for item in plan.credentials
            if not item.locations
        ),
    )


def parse_inventory(
    register_raw: bytes, plan_raw: bytes, source_id: str, now: datetime
) -> CredentialInventorySnapshot | str:
    if len(register_raw) > MAX_SOURCE_BYTES or len(plan_raw) > MAX_SOURCE_BYTES:
        return "Credential inventory exceeds size limit"
    try:
        register: Final = _Register.model_validate_json(register_raw)
        plan: Final = _Plan.model_validate_json(plan_raw)
        quarantined: Final = _quarantine(register, plan)
        excluded: Final = frozenset(item.fingerprint for item in quarantined if item.fingerprint is not None)
        shared: Final = {item.credential_id: tuple(sorted(item.services)) for item in register.reused_credentials}
        return CredentialInventorySnapshot(
            source_id=source_id,
            scanned_at=register.created,
            imported_at=now,
            hosts=tuple(
                HostScan(
                    host=host.id,
                    scanned_at=register.created,
                    roots=tuple(sorted(frozenset(host.roots))),
                    files_read=host.files_read,
                    unreadable=host.unreadable,
                    size_skips=host.size_skips,
                    errors=len(host.errors),
                    verification_errors=len(host.verification.errors),
                    secret_values_exported=host.verification.secret_values_exported,
                )
                for host in register.hosts
            ),
            services=tuple(
                ServiceSummary(
                    service=item.service,
                    status=item.status,
                    hosts=item.hosts,
                    current_value_locations=item.current_value_locations,
                    archive_value_locations=item.archive_value_locations,
                )
                for item in register.services
            ),
            candidates=tuple(
                CredentialCandidate(
                    fingerprint=item.id,
                    service=item.service,
                    kinds=tuple(sorted(frozenset(item.kind))),
                    locations=tuple(
                        CandidateLocation(
                            host=location.host,
                            project=location.project,
                            variable=location.variable,
                            path=location.path,
                            source_class=location.source_class,
                            observed_at=register.created,
                        )
                        for location in item.locations
                    ),
                    shared_with=tuple(service for service in shared.get(item.id, ()) if service != item.service),
                )
                for item in plan.credentials
                if item.id not in excluded
            ),
            quarantined=quarantined,
        )
    except ValidationError:
        return "Credential inventory rejected; input values are not logged"


def _location_key(location: CandidateLocation) -> tuple[str, str, str]:
    return location.host, location.path, location.variable


def _retained(location: CandidateLocation) -> CandidateLocation:
    return location.model_copy(update={"presence": "retained"})


def merge_snapshot(
    previous: CredentialInventorySnapshot | None, latest: CredentialInventorySnapshot
) -> CredentialInventorySnapshot:
    if previous is None:
        return latest
    if latest.scanned_at <= previous.scanned_at:
        return previous
    scanned_hosts: Final = frozenset(host.host for host in latest.hosts)
    complete_scans: Final = {host.host: host for host in latest.hosts if host.complete}
    doubtful: Final = frozenset(item.fingerprint for item in latest.quarantined if item.fingerprint is not None)
    doubtful_services: Final = frozenset(
        item.service for item in latest.quarantined if item.reason == "duplicate_service"
    )
    current: Final = {item.fingerprint: item for item in latest.candidates}
    earlier: Final = {item.fingerprint: item for item in previous.candidates}

    def kept(fingerprint: str) -> tuple[CandidateLocation, ...]:
        fresh: Final[frozenset[tuple[str, str, str]]] = frozenset(
            map(_location_key, current[fingerprint].locations if fingerprint in current else ())
        )
        prior: Final = earlier.get(fingerprint)
        authoritative: Final = (
            prior is not None and fingerprint not in doubtful and prior.service not in doubtful_services
        )
        return tuple(
            _retained(location)
            for location in (prior.locations if prior is not None else ())
            if not (
                authoritative
                and location.host in complete_scans
                and complete_scans[location.host].covers(location.path)
            )
            and _location_key(location) not in fresh
        )

    def candidate(fingerprint: str) -> CredentialCandidate | None:
        retained: Final = kept(fingerprint)
        if fingerprint in current:
            return current[fingerprint].model_copy(update={"locations": (*current[fingerprint].locations, *retained)})
        return earlier[fingerprint].model_copy(update={"locations": retained}) if retained else None

    merged: Final = (candidate(fingerprint) for fingerprint in sorted(frozenset(current) | frozenset(earlier)))
    return latest.model_copy(
        update={
            "hosts": (
                *latest.hosts,
                *(
                    host.model_copy(update={"presence": "retained"})
                    for host in previous.hosts
                    if host.host not in scanned_hosts
                ),
            ),
            "candidates": tuple(item for item in merged if item is not None),
        }
    )


ImportStatus = Literal["imported", "older_or_equal_ignored", "rejected", "unreadable"]


class CredentialImportAttempt(FrozenModel):
    attempted_at: AwareDatetime
    status: ImportStatus
    message: Annotated[str, Field(max_length=300)] = ""
    scanned_at: AwareDatetime | None = None


class CredentialInventoryState(FrozenModel):
    source_id: Identifier
    attempt: CredentialImportAttempt
    evidence: CredentialInventorySnapshot | None = None


class _LegacyLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: Identifier
    project: str
    variable: str
    path: str
    source_class: SourceClass


class _LegacyCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fingerprint: Fingerprint
    service: str
    kinds: tuple[CredentialKind, ...]
    locations: tuple[_LegacyLocation, ...]
    shared_with: tuple[str, ...] = ()


class _LegacyHost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    host: Identifier
    roots: int = 0
    files_read: int
    unreadable: int
    size_skips: int
    errors: int
    verification_errors: int
    secret_values_exported: bool


class _LegacySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: Identifier
    scanned_at: AwareDatetime
    imported_at: AwareDatetime
    validity: Literal["not_checked"] = "not_checked"
    hosts: tuple[_LegacyHost, ...]
    services: tuple[ServiceSummary, ...]
    candidates: tuple[_LegacyCandidate, ...]
    quarantined: tuple[Quarantined, ...] = ()


def from_legacy(raw: str) -> CredentialInventorySnapshot:
    legacy: Final = _LegacySnapshot.model_validate_json(raw)
    return CredentialInventorySnapshot(
        source_id=legacy.source_id,
        scanned_at=legacy.scanned_at,
        imported_at=legacy.imported_at,
        hosts=tuple(
            HostScan(
                host=host.host,
                roots=(),
                files_read=host.files_read,
                unreadable=host.unreadable,
                size_skips=host.size_skips,
                errors=host.errors,
                verification_errors=host.verification_errors,
                secret_values_exported=host.secret_values_exported,
                scanned_at=legacy.scanned_at,
            )
            for host in legacy.hosts
        ),
        services=legacy.services,
        candidates=tuple(
            CredentialCandidate(
                fingerprint=item.fingerprint,
                service=item.service,
                kinds=item.kinds,
                locations=tuple(
                    CandidateLocation(
                        host=location.host,
                        project=location.project,
                        variable=location.variable,
                        path=location.path,
                        source_class=location.source_class,
                        observed_at=legacy.scanned_at,
                    )
                    for location in item.locations
                ),
                shared_with=item.shared_with,
            )
            for item in legacy.candidates
        ),
        quarantined=legacy.quarantined,
    )
