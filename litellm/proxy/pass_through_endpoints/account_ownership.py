from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Final, Literal, Protocol, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, model_validator
from typing_extensions import Self

from litellm._logging import verbose_proxy_logger

from .account_credentials import AccountHeaderCredential, Identifier
from .account_grants import (
    LOCK_SETTINGS_SQL,
    GrantDatabase,
    GrantFailure,
    GrantTransaction,
    describe_account_policy,
    settings_from_rows,
)
from .account_resources import (
    ResourceCreation,
    ResourcePathMatch,
    ResourceTypeName,
    canonical_resource_id,
    find_resource,
    select_resource_id,
)

_COLUMNS: Final = '"account_id", "resource_type", "resource_id", "route_id", "owner_application_id", "provenance"'
CLAIM_CREATED_SQL: Final = f"""INSERT INTO "LiteLLM_AccountResourceOwnership" ({_COLUMNS}, "creator_key_hash")
VALUES ($1, $2, $3, $4, $5, 'created', $6)
ON CONFLICT ("account_id", "resource_type", "resource_id") DO NOTHING"""
CLAIM_ADOPTED_SQL: Final = f"""INSERT INTO "LiteLLM_AccountResourceOwnership" ({_COLUMNS}, "creator_key_hash")
VALUES ($1, $2, $3, $4, $5, 'adopted', NULL)
ON CONFLICT ("account_id", "resource_type", "resource_id") DO NOTHING"""
READ_OWNERSHIP_SQL: Final = """SELECT "route_id", "owner_application_id", "provenance", "creator_key_hash"
FROM "LiteLLM_AccountResourceOwnership"
WHERE "account_id" = $1 AND "resource_type" = $2 AND "resource_id" = $3"""


class ResourceClaim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    account_id: Identifier
    resource_type: ResourceTypeName
    resource_id: Annotated[str, Field(min_length=1, max_length=256)]
    route_id: Identifier
    owner_application_id: Identifier
    provenance: Literal["created", "adopted"]
    creator_key_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        if not canonical_resource_id(self.resource_id):
            raise ValueError("Resource ids must be one canonical path segment")
        if (self.provenance == "created") != (self.creator_key_hash is not None):
            raise ValueError("Created resources record their creator key; adopted resources do not")
        return self


class ResourceOwnership(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    route_id: str
    owner_application_id: str
    provenance: Literal["created", "adopted"]
    creator_key_hash: str | None = None


@dataclass(frozen=True, slots=True)
class OwnershipRecorded:
    ownership: ResourceOwnership
    inserted: bool


@dataclass(frozen=True, slots=True)
class OwnershipConflict:
    ownership: ResourceOwnership


_ROWS: Final = TypeAdapter(tuple[ResourceOwnership, ...])


async def _read(
    transaction: GrantTransaction, account_id: str, resource_type: str, resource_id: str
) -> ResourceOwnership | None:
    try:
        rows: Final = _ROWS.validate_python(
            await transaction.query_raw(READ_OWNERSHIP_SQL, account_id, resource_type, resource_id)
        )
    except ValidationError:
        return None
    return rows[0] if len(rows) == 1 else None


async def record_in_transaction(
    transaction: GrantTransaction, claim: ResourceClaim
) -> OwnershipRecorded | OwnershipConflict:
    key: Final = (
        claim.account_id,
        claim.resource_type,
        claim.resource_id,
        claim.route_id,
        claim.owner_application_id,
    )
    inserted: Final = (
        await transaction.execute_raw(CLAIM_CREATED_SQL, *key, claim.creator_key_hash)
        if claim.creator_key_hash is not None
        else await transaction.execute_raw(CLAIM_ADOPTED_SQL, *key)
    )
    if inserted not in (0, 1):
        raise RuntimeError("Resource ownership claim changed an unexpected number of rows")
    stored: Final = await _read(transaction, claim.account_id, claim.resource_type, claim.resource_id)
    if stored is None:
        raise RuntimeError("Resource ownership could not be read back")
    if stored.owner_application_id == claim.owner_application_id and stored.route_id == claim.route_id:
        return OwnershipRecorded(stored, inserted == 1)
    if inserted == 1:
        raise RuntimeError("Resource ownership readback does not match the new claim")
    return OwnershipConflict(stored)


async def record_ownership(database: GrantDatabase, claim: ResourceClaim) -> OwnershipRecorded | OwnershipConflict:
    async with database.tx() as transaction:
        return await record_in_transaction(transaction, claim)


async def read_ownership(
    database: GrantDatabase, account_id: str, resource_type: str, resource_id: str
) -> ResourceOwnership | None:
    if not canonical_resource_id(resource_id):
        return None
    async with database.tx() as transaction:
        return await _read(transaction, account_id, resource_type, resource_id)


class OwnershipLookup(Protocol):
    async def __call__(self, account_id: str, resource_type: str, resource_id: str, /) -> ResourceOwnership | None: ...


def resource_path_match(policy: AccountHeaderCredential, path: str) -> ResourcePathMatch | None:
    return find_resource(policy.resource_types, path)


async def resource_access_allowed(
    policy: AccountHeaderCredential,
    match: ResourcePathMatch,
    route_id: str | None,
    application_id: str | None,
    key_hash: str | None,
    method: str,
    lookup: OwnershipLookup,
) -> bool:
    resource_id: Final = match.resource_id
    if resource_id is None or route_id is None or application_id is None or key_hash is None:
        return False
    granted: Final = any(
        grant.application_id == application_id
        and grant.key_hash == key_hash
        and grant.account_id == policy.account_id
        and grant.path == match.template
        and method in grant.methods
        for grant in policy.application_grants
    )
    if not granted:
        return False
    try:
        ownership: Final = await lookup(policy.account_id, match.resource_type, resource_id)
    except Exception:
        verbose_proxy_logger.warning("Account resource ownership lookup failed; access denied")
        return False
    return ownership is not None and ownership.owner_application_id == application_id and ownership.route_id == route_id


def _proxy_database() -> GrantDatabase:
    from litellm.proxy import proxy_server

    if proxy_server.prisma_client is None:
        raise RuntimeError("Account resource ownership requires the proxy database")
    return cast(  # cast-ok: PrismaClient provides the tx() protocol used for account configuration
        GrantDatabase, proxy_server.prisma_client
    )


async def proxy_ownership_lookup(account_id: str, resource_type: str, resource_id: str, /) -> ResourceOwnership | None:
    return await read_ownership(_proxy_database(), account_id, resource_type, resource_id)


async def record_ownerships(
    database: GrantDatabase, claims: tuple[ResourceClaim, ...]
) -> tuple[OwnershipRecorded | OwnershipConflict, ...]:
    async with database.tx() as transaction:
        return tuple([await record_in_transaction(transaction, claim) for claim in claims])


class OwnershipRecorder(Protocol):
    async def __call__(
        self, claims: tuple[ResourceClaim, ...], /
    ) -> tuple[OwnershipRecorded | OwnershipConflict, ...]: ...


async def proxy_ownership_record(
    claims: tuple[ResourceClaim, ...], /
) -> tuple[OwnershipRecorded | OwnershipConflict, ...]:
    return await record_ownerships(_proxy_database(), claims)


CaptureSkip: TypeAlias = Literal[
    "unsuccessful_response", "unbuffered_response", "missing_identity", "invalid_json", "missing_id", "capture_failed"
]


@dataclass(frozen=True, slots=True)
class ResourceNotCaptured:
    reason: CaptureSkip


@dataclass(frozen=True, slots=True)
class CapturedResource:
    resource_type: str
    outcome: OwnershipRecorded | OwnershipConflict | ResourceNotCaptured


_JSON: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)


def _skipped(creations: tuple[tuple[str, ResourceCreation], ...], reason: CaptureSkip) -> tuple[CapturedResource, ...]:
    return tuple(CapturedResource(name, ResourceNotCaptured(reason)) for name, _ in creations)


async def _capture(
    creations: tuple[tuple[str, ResourceCreation], ...],
    account_id: str,
    route_id: str | None,
    application_id: str | None,
    key_hash: str | None,
    status_code: int,
    body: bytes | memoryview | None,
    record: OwnershipRecorder,
) -> tuple[CapturedResource, ...]:
    if not 200 <= status_code < 300:
        return _skipped(creations, "unsuccessful_response")
    if body is None:
        return _skipped(creations, "unbuffered_response")
    if route_id is None or application_id is None or key_hash is None:
        return _skipped(creations, "missing_identity")
    try:
        document: Final = _JSON.validate_json(bytes(body))
    except ValidationError:
        return _skipped(creations, "invalid_json")
    selected: Final = tuple((name, select_resource_id(document, creation.id_selector)) for name, creation in creations)
    claims: Final = tuple(
        ResourceClaim(
            account_id=account_id,
            resource_type=name,
            resource_id=resource_id,
            route_id=route_id,
            owner_application_id=application_id,
            provenance="created",
            creator_key_hash=key_hash,
        )
        for name, resource_id in selected
        if resource_id is not None
    )
    recorded: Final = MappingProxyType(
        dict(zip((claim.resource_type for claim in claims), await record(claims) if claims else (), strict=True))
    )
    return tuple(CapturedResource(name, recorded.get(name, ResourceNotCaptured("missing_id"))) for name, _ in selected)


async def capture_created_resources(
    policy: AccountHeaderCredential,
    route_id: str | None,
    application_id: str | None,
    key_hash: str | None,
    path: str,
    method: str,
    status_code: int,
    body: bytes | memoryview | None,
    record: OwnershipRecorder,
) -> tuple[CapturedResource, ...]:
    creations: Final = tuple(
        (resource.name, resource.creation)
        for resource in policy.resource_types
        if resource.creation is not None and resource.creation.path == path and method in resource.creation.methods
    )
    if not creations:
        return ()
    try:
        captured: Final = await _capture(
            creations, policy.account_id, route_id, application_id, key_hash, status_code, body, record
        )
    except Exception:
        verbose_proxy_logger.warning("Account resource capture failed; created resources stay unowned")
        return _skipped(creations, "capture_failed")
    for item in captured:
        if not isinstance(item.outcome, OwnershipRecorded):
            verbose_proxy_logger.warning(
                "Account resource %s was not captured: %s",
                item.resource_type,
                item.outcome.reason
                if isinstance(item.outcome, ResourceNotCaptured)
                else "owned by another application",
            )
    return captured


class ResourceAdoptionCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_fingerprint: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    resource_type: ResourceTypeName
    resource_id: Annotated[str, Field(min_length=1, max_length=256)]
    application_id: Identifier

    @model_validator(mode="after")
    def validate_adoption(self) -> Self:
        if not canonical_resource_id(self.resource_id):
            raise ValueError("Resource ids must be one canonical path segment")
        return self


class AccountResourceReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    stored: Literal[True] = True
    inserted: bool
    enforcement: Literal["not_verified"] = "not_verified"
    ownership: ResourceOwnership


async def adopt_resource(
    database: GrantDatabase, route_id: str, command: ResourceAdoptionCommand
) -> OwnershipRecorded | OwnershipConflict | GrantFailure:
    async with database.tx() as transaction:
        settings: Final = settings_from_rows(await transaction.query_raw(LOCK_SETTINGS_SQL, "general_settings"))
        if isinstance(settings, GrantFailure):
            return settings
        described: Final = describe_account_policy(settings, route_id)
        if isinstance(described, GrantFailure):
            return described
        descriptor, policy = described
        if descriptor.fingerprint != command.expected_fingerprint:
            return GrantFailure("conflict")
        if command.resource_type not in frozenset(resource.name for resource in policy.resource_types):
            return GrantFailure("invalid")
        return await record_in_transaction(
            transaction,
            ResourceClaim(
                account_id=policy.account_id,
                resource_type=command.resource_type,
                resource_id=command.resource_id,
                route_id=route_id,
                owner_application_id=command.application_id,
                provenance="adopted",
            ),
        )
