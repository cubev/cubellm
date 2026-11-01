from dataclasses import dataclass
from typing import Annotated, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from typing_extensions import Self

from litellm._logging import verbose_proxy_logger

from .account_credentials import AccountHeaderCredential, Identifier
from .account_grants import GrantDatabase, GrantTransaction
from .account_resources import ResourcePathMatch, ResourceTypeName, canonical_resource_id, find_resource

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


async def record_ownership(database: GrantDatabase, claim: ResourceClaim) -> OwnershipRecorded | OwnershipConflict:
    async with database.tx() as transaction:
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
