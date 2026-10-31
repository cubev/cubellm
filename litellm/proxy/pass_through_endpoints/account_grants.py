import json
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Annotated, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from .account_credentials import (
    AccountApplicationGrant,
    AccountHeaderCredential,
    credential_origin,
    validate_account_route,
)

LOCK_SETTINGS_SQL: Final = 'SELECT "param_value" FROM "LiteLLM_Config" WHERE "param_name" = $1 FOR UPDATE'
READ_SETTINGS_SQL: Final = 'SELECT "param_value" FROM "LiteLLM_Config" WHERE "param_name" = $1'
WRITE_SETTINGS_SQL: Final = """UPDATE "LiteLLM_Config" SET "param_value" = jsonb_set(
    CASE jsonb_typeof("param_value") WHEN 'string' THEN ("param_value" #>> '{}')::jsonb ELSE "param_value" END,
    ARRAY['pass_through_endpoints', $1, 'account_credential', 'application_grants'], $2::jsonb, true
) WHERE "param_name" = $3"""
_OBJECT: Final = TypeAdapter(dict[str, JsonValue])
_ROUTES: Final = TypeAdapter(tuple[dict[str, JsonValue], ...])


class GrantTransaction(Protocol):
    async def query_raw(self, query: str, *parameters: str) -> object: ...

    async def execute_raw(self, query: str, *parameters: str) -> int: ...


class GrantDatabase(Protocol):
    def tx(self) -> AbstractAsyncContextManager[GrantTransaction]: ...


class AccountGrantCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_fingerprint: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    grant: AccountApplicationGrant


class AccountRouteDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    route_id: str
    account_id: str
    path: str
    methods: tuple[str, ...]
    include_subpath: bool = False
    origin: str
    fingerprint: str
    grants: tuple[AccountApplicationGrant, ...]


class AccountGrantReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    stored: Literal[True] = True
    enforcement: Literal["not_verified"] = "not_verified"
    route: AccountRouteDescriptor


@dataclass(frozen=True, slots=True)
class GrantFailure:
    kind: Literal["not_found", "conflict", "invalid"]


class _ConfigRow(BaseModel):
    param_value: JsonValue


class _Route(BaseModel):
    id: str
    path: str
    target: str
    auth: Literal[True]
    methods: tuple[str, ...]
    include_subpath: bool = False
    forward_headers: Literal[False] | None = False
    headers: dict[str, JsonValue] | None = None
    default_query_params: dict[str, JsonValue] | None = None
    account_credential: AccountHeaderCredential


@dataclass(frozen=True, slots=True)
class GrantPlan:
    descriptor: AccountRouteDescriptor
    changed: bool
    route_index: int


def _route(settings: Mapping[str, JsonValue], route_id: str) -> Mapping[str, JsonValue] | GrantFailure:
    try:
        routes: Final = _ROUTES.validate_python(settings.get("pass_through_endpoints", ()))
    except ValueError:
        return GrantFailure("invalid")
    matches: Final = tuple(route for route in routes if route.get("id") == route_id)
    if not matches:
        return GrantFailure("not_found")
    return MappingProxyType(matches[0]) if len(matches) == 1 else GrantFailure("conflict")


def describe_account_route(settings: Mapping[str, JsonValue], route_id: str) -> AccountRouteDescriptor | GrantFailure:
    raw: Final = _route(settings, route_id)
    if isinstance(raw, GrantFailure):
        return raw
    try:
        route: Final = _Route.model_validate(raw)
        validate_account_route(
            route.target,
            route.auth,
            route.forward_headers,
            route.headers or {},
            route.default_query_params,
            route.account_credential,
            route.methods,
        )
        credential: Final = _OBJECT.validate_python(raw.get("account_credential"))
        stable: Final = {
            **raw,
            "account_credential": {key: value for key, value in credential.items() if key != "application_grants"},
        }
        return AccountRouteDescriptor(
            route_id=route.id,
            account_id=route.account_credential.account_id,
            path=route.path,
            methods=route.methods,
            include_subpath=route.include_subpath,
            origin=credential_origin(route.target),
            fingerprint=sha256(json.dumps(stable, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            grants=route.account_credential.application_grants,
        )
    except ValueError:
        return GrantFailure("invalid")


def plan_account_grant(
    settings: Mapping[str, JsonValue], route_id: str, command: AccountGrantCommand
) -> GrantPlan | GrantFailure:
    descriptor: Final = describe_account_route(settings, route_id)
    if isinstance(descriptor, GrantFailure):
        return descriptor
    if descriptor.fingerprint != command.expected_fingerprint:
        return GrantFailure("conflict")
    raw: Final = _route(settings, route_id)
    if isinstance(raw, GrantFailure):
        return raw
    route: Final = _Route.model_validate(raw)
    routes: Final = _ROUTES.validate_python(settings["pass_through_endpoints"])
    route_index: Final = next(index for index, value in enumerate(routes) if value.get("id") == route_id)
    grant: Final = command.grant
    path_matches: Final = grant.path == route.path or (
        route.include_subpath and grant.path.startswith(route.path.rstrip("/") + "/")
    )
    if grant.account_id != descriptor.account_id or not path_matches or not set(grant.methods).issubset(route.methods):
        return GrantFailure("invalid")
    if grant in descriptor.grants:
        return GrantPlan(descriptor, False, route_index)
    if any(
        existing.application_id == grant.application_id
        and existing.key_hash == grant.key_hash
        and existing.path == grant.path
        for existing in descriptor.grants
    ):
        return GrantFailure("conflict")
    try:
        updated_policy: Final = AccountHeaderCredential.model_validate(
            route.account_credential.model_dump() | {"application_grants": (*descriptor.grants, grant)}
        )
    except ValueError:
        return GrantFailure("invalid")
    return GrantPlan(descriptor.model_copy(update={"grants": updated_policy.application_grants}), True, route_index)


def _settings(rows: object) -> Mapping[str, JsonValue] | GrantFailure:
    try:
        parsed: Final = TypeAdapter(tuple[_ConfigRow, ...]).validate_python(rows)
        if len(parsed) != 1:
            return GrantFailure("not_found")
        value: Final = parsed[0].param_value
        return MappingProxyType(
            _OBJECT.validate_json(value) if isinstance(value, str) else _OBJECT.validate_python(value)
        )
    except ValueError:
        return GrantFailure("invalid")


async def read_account_route(database: GrantDatabase, route_id: str) -> AccountRouteDescriptor | GrantFailure:
    async with database.tx() as transaction:
        settings: Final = _settings(await transaction.query_raw(READ_SETTINGS_SQL, "general_settings"))
        return settings if isinstance(settings, GrantFailure) else describe_account_route(settings, route_id)


async def append_account_grant(
    database: GrantDatabase, route_id: str, command: AccountGrantCommand
) -> AccountGrantReceipt | GrantFailure:
    async with database.tx() as transaction:
        settings: Final = _settings(await transaction.query_raw(LOCK_SETTINGS_SQL, "general_settings"))
        if isinstance(settings, GrantFailure):
            return settings
        plan: Final = plan_account_grant(settings, route_id, command)
        if isinstance(plan, GrantFailure):
            return plan
        if plan.changed:
            count: Final = await transaction.execute_raw(
                WRITE_SETTINGS_SQL,
                str(plan.route_index),
                json.dumps([grant.model_dump(mode="json") for grant in plan.descriptor.grants]),
                "general_settings",
            )
            if count != 1:
                raise RuntimeError("Account grant transaction did not update exactly one configuration row")
            stored: Final = _settings(await transaction.query_raw(READ_SETTINGS_SQL, "general_settings"))
            verified: Final = stored if isinstance(stored, GrantFailure) else describe_account_route(stored, route_id)
            if (
                isinstance(verified, GrantFailure)
                or verified.fingerprint != command.expected_fingerprint
                or command.grant not in verified.grants
            ):
                raise RuntimeError("Account grant transaction verification failed")
            return AccountGrantReceipt(route=verified)
        return AccountGrantReceipt(route=plan.descriptor)
