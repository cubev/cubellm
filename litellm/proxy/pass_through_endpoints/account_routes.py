import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import chain
from types import MappingProxyType
from typing import Annotated, Final, Literal, Protocol, TypeAlias
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, model_validator
from starlette.applications import Starlette
from starlette.convertors import Convertor, PathConvertor
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute, compile_path
from typing_extensions import Self

from .account_credentials import (
    AccountHeaderCredential,
    CredentialHeader,
    Identifier,
    validate_account_resources,
    validate_account_route,
)
from .account_grants import (
    LOCK_SETTINGS_SQL,
    READ_SETTINGS_SQL,
    AccountRouteDescriptor,
    GrantDatabase,
    GrantFailure,
    describe_account_route,
    settings_from_rows,
)
from .account_resources import AccountResourceType

INSERT_SETTINGS_SQL: Final = """INSERT INTO "LiteLLM_Config" ("param_name", "param_value") VALUES ($1, '{}'::jsonb)
ON CONFLICT ("param_name") DO NOTHING"""
APPEND_ROUTE_SQL: Final = """UPDATE "LiteLLM_Config" SET "param_value" = jsonb_set(
    current.settings,
    ARRAY['pass_through_endpoints'],
    CASE jsonb_typeof(current.settings -> 'pass_through_endpoints')
        WHEN 'array' THEN current.settings -> 'pass_through_endpoints' ELSE '[]'::jsonb
    END || jsonb_build_array($1::jsonb),
    true
) FROM (
    SELECT CASE jsonb_typeof("param_value") WHEN 'string' THEN ("param_value" #>> '{}')::jsonb ELSE "param_value" END
        AS settings
    FROM "LiteLLM_Config" WHERE "param_name" = $2
) AS current WHERE "LiteLLM_Config"."param_name" = $2"""
_ROUTES: Final = TypeAdapter(tuple[Mapping[str, JsonValue], ...])
_OBJECT: Final = TypeAdapter(Mapping[str, JsonValue])


def _has_blank_or_control(text: str) -> bool:
    return any(character.isspace() or unicodedata.category(character) == "Cc" for character in text)


class AccountRouteDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: Identifier
    path: Annotated[str, Field(max_length=1024, pattern=r"^/cube_api/[A-Za-z0-9._~!$&'()+,;=:@/-]+$")]
    target: Annotated[str, Field(min_length=1, max_length=2048)]
    methods: Annotated[
        tuple[Annotated[str, Field(pattern=r"^[!#$%&'+.^_`|~0-9A-Z-]{1,32}$")], ...],
        Field(min_length=1, max_length=16),
    ]
    include_subpath: bool = False
    account_credential: AccountHeaderCredential

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        if "//" in self.path or self.path.endswith("/") or any(part in (".", "..") for part in self.path.split("/")):
            raise ValueError("Managed account routes require canonical concrete paths")
        if len(frozenset(self.methods)) != len(self.methods):
            raise ValueError("Managed account route methods must be unique")
        if self.account_credential.application_grants:
            raise ValueError("Managed account routes start without grants; append grants separately")
        hostname: Final = urlsplit(self.target).hostname or ""
        if _has_blank_or_control(self.target) or _has_blank_or_control(unquote(hostname)):
            raise ValueError("Managed account route targets cannot contain blank or control characters")
        validate_account_route(
            self.target, True, False, MappingProxyType({}), None, self.account_credential, self.methods
        )
        validate_account_resources(self.path, self.include_subpath, self.account_credential)
        return self

    def stored(self) -> Mapping[str, JsonValue]:
        route: Final = _StoredRoute(
            id=self.id,
            path=self.path,
            target=self.target,
            methods=self.methods,
            include_subpath=self.include_subpath,
            account_credential=(
                _StoredResourceCredential.model_validate(self.account_credential, from_attributes=True)
                if self.account_credential.resource_types
                else _StoredCredential.model_validate(self.account_credential, from_attributes=True)
            ),
        )
        return _OBJECT.validate_json(route.model_dump_json())


class _StoredCredential(BaseModel):
    model_config = ConfigDict(frozen=True)
    account_id: str
    credential_name: str
    headers: tuple[CredentialHeader, ...]
    allowed_query_parameters: tuple[str, ...]


class _StoredResourceCredential(BaseModel):
    model_config = ConfigDict(frozen=True)
    account_id: str
    credential_name: str
    headers: tuple[CredentialHeader, ...]
    allowed_query_parameters: tuple[str, ...]
    resource_types: Annotated[tuple[AccountResourceType, ...], Field(min_length=1)]


class _StoredRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    path: str
    target: str
    methods: tuple[str, ...]
    include_subpath: bool
    auth: Literal[True] = True
    forward_headers: Literal[False] = False
    account_credential: _StoredCredential | _StoredResourceCredential


class AccountRouteCreateCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    route: AccountRouteDefinition


class AccountRouteReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    stored: Literal[True] = True
    created: bool
    enforcement: Literal["not_verified"] = "not_verified"
    route: AccountRouteDescriptor


def _without_grants(route: Mapping[str, JsonValue]) -> Mapping[str, object] | None:
    credential: Final = route.get("account_credential")
    if not isinstance(credential, dict):
        return None
    return MappingProxyType(
        {
            **route,
            "account_credential": MappingProxyType(
                {name: value for name, value in credential.items() if name != "application_grants"}
            ),
        }
    )


def _routes(settings: Mapping[str, JsonValue]) -> tuple[Mapping[str, JsonValue], ...] | GrantFailure:
    value: Final = settings.get("pass_through_endpoints")
    try:
        return () if value is None else _ROUTES.validate_python(value)
    except ValidationError:
        return GrantFailure("invalid")


@dataclass(frozen=True, slots=True)
class RoutePattern:
    regex: re.Pattern[str]
    path_format: str
    remainder_parameters: frozenset[str]


UNKNOWN_ROUTE: Final = RoutePattern(re.compile(r"^.*$"), "{unknown}", frozenset(("unknown",)))


def _pattern(regex: re.Pattern[str], path_format: str, convertors: Mapping[str, Convertor[object]]) -> RoutePattern:
    remainder: Final = frozenset(name for name, convertor in convertors.items() if isinstance(convertor, PathConvertor))
    return RoutePattern(regex, path_format, remainder)


def route_pattern(path: str) -> RoutePattern:
    try:
        regex, path_format, convertors = compile_path(path)
    except (AssertionError, ValueError):
        return UNKNOWN_ROUTE
    return _pattern(regex, path_format, convertors)


def application_route_patterns(routes: Iterable[BaseRoute]) -> tuple[RoutePattern, ...]:
    return tuple(
        _pattern(route.path_regex, route.path_format, route.param_convertors)
        if isinstance(route, Route | WebSocketRoute | Mount)
        else UNKNOWN_ROUTE
        for route in routes
    )


def serving_route_patterns(scope: Mapping[str, object]) -> tuple[RoutePattern, ...]:
    application: Final = scope.get("app")
    if not isinstance(application, Starlette):
        return (UNKNOWN_ROUTE,)
    return application_route_patterns(application.router.routes)


def _stored_patterns(route: Mapping[str, JsonValue]) -> tuple[RoutePattern, ...] | GrantFailure:
    path: Final = route.get("path")
    include_subpath: Final = route.get("include_subpath", False)
    if not isinstance(path, str) or not isinstance(include_subpath, bool):
        return GrantFailure("invalid")
    exact: Final = route_pattern(path)
    return (exact, route_pattern(path + "/{subpath:path}")) if include_subpath else (exact,)


def _may_match_child(pattern: RoutePattern, path: str) -> bool:
    candidate: Final = path.split("/")
    for index, segment in enumerate(pattern.path_format.split("/")):
        if any("{" + name + "}" in segment for name in pattern.remainder_parameters) or index >= len(candidate):
            return True
        if "{" not in segment and segment != candidate[index]:
            return False
    return False


def _shadows(pattern: RoutePattern, definition: AccountRouteDefinition) -> bool:
    return pattern.regex.match(definition.path) is not None or (
        definition.include_subpath and _may_match_child(pattern, definition.path)
    )


def plan_account_route(
    settings: Mapping[str, JsonValue],
    definition: AccountRouteDefinition,
    registered: tuple[RoutePattern, ...] = (),
) -> bool | GrantFailure:
    routes: Final = _routes(settings)
    if isinstance(routes, GrantFailure):
        return routes
    same_id: Final = tuple(route for route in routes if route.get("id") == definition.id)
    if len(same_id) > 1:
        return GrantFailure("conflict")
    if same_id:
        return False if _without_grants(same_id[0]) == definition.stored() else GrantFailure("conflict")
    stored: Final = tuple(_stored_patterns(route) for route in routes)
    valid: Final = tuple(group for group in stored if not isinstance(group, GrantFailure))
    if len(valid) != len(stored):
        return GrantFailure("invalid")
    patterns: Final = (*registered, *chain.from_iterable(valid))
    return GrantFailure("conflict") if any(_shadows(pattern, definition) for pattern in patterns) else True


async def create_account_route(
    database: GrantDatabase, command: AccountRouteCreateCommand, registered: tuple[RoutePattern, ...] = ()
) -> AccountRouteReceipt | GrantFailure:
    definition: Final = command.route
    async with database.tx() as transaction:
        _ = await transaction.execute_raw(INSERT_SETTINGS_SQL, "general_settings")
        settings: Final = settings_from_rows(await transaction.query_raw(LOCK_SETTINGS_SQL, "general_settings"))
        if isinstance(settings, GrantFailure):
            return settings
        plan: Final = plan_account_route(settings, definition, registered)
        if isinstance(plan, GrantFailure):
            return plan
        if not plan:
            existing: Final = describe_account_route(settings, definition.id)
            return (
                existing if isinstance(existing, GrantFailure) else AccountRouteReceipt(created=False, route=existing)
            )
        before: Final = _routes(settings)
        count: Final = await transaction.execute_raw(
            APPEND_ROUTE_SQL, json.dumps(definition.stored(), allow_nan=False), "general_settings"
        )
        if count != 1 or isinstance(before, GrantFailure):
            raise RuntimeError("Account route transaction did not update exactly one configuration row")
        stored: Final = settings_from_rows(await transaction.query_raw(READ_SETTINGS_SQL, "general_settings"))
        after: Final = GrantFailure("invalid") if isinstance(stored, GrantFailure) else _routes(stored)
        verified: Final = (
            GrantFailure("invalid")
            if isinstance(stored, GrantFailure)
            else describe_account_route(stored, definition.id)
        )
        if (
            isinstance(after, GrantFailure)
            or isinstance(verified, GrantFailure)
            or after[: len(before)] != before
            or len(after) != len(before) + 1
            or _without_grants(after[-1]) != definition.stored()
            or verified.grants
        ):
            raise RuntimeError("Account route transaction verification failed")
        return AccountRouteReceipt(created=True, route=verified)


WRITE_ROUTES_SQL: Final = """UPDATE "LiteLLM_Config" SET "param_value" = jsonb_set(
    current.settings, ARRAY['pass_through_endpoints'], $1::jsonb, true
) FROM (
    SELECT CASE jsonb_typeof("param_value") WHEN 'string' THEN ("param_value" #>> '{}')::jsonb ELSE "param_value" END
        AS settings
    FROM "LiteLLM_Config" WHERE "param_name" = $2
) AS current WHERE "LiteLLM_Config"."param_name" = $2"""
DELETE_ROUTE_OWNERSHIP_SQL: Final = 'DELETE FROM "LiteLLM_AccountResourceOwnership" WHERE "route_id" = $1'

StoredRoutes: TypeAlias = tuple[Mapping[str, JsonValue], ...]


class RoutesPlan(Protocol):
    def __call__(self, routes: StoredRoutes, /) -> StoredRoutes | GrantFailure: ...


@dataclass(frozen=True, slots=True)
class RoutesMutation:
    before: Mapping[str, JsonValue]
    after: Mapping[str, JsonValue]


async def mutate_settings_routes(
    database: GrantDatabase, plan: RoutesPlan, removed_route_id: str | None = None
) -> RoutesMutation | GrantFailure:
    async with database.tx() as transaction:
        _ = await transaction.execute_raw(INSERT_SETTINGS_SQL, "general_settings")
        settings: Final = settings_from_rows(await transaction.query_raw(LOCK_SETTINGS_SQL, "general_settings"))
        if isinstance(settings, GrantFailure):
            return settings
        current: Final = _routes(settings)
        if isinstance(current, GrantFailure):
            return current
        proposed: Final = plan(current)
        if isinstance(proposed, GrantFailure):
            return proposed
        planned: Final = _ROUTES.validate_python(proposed)
        written: Final = await transaction.execute_raw(
            WRITE_ROUTES_SQL, _ROUTES.dump_json(planned).decode(), "general_settings"
        )
        if written != 1:
            raise RuntimeError("Pass-through route update did not change exactly one configuration row")
        stored: Final = settings_from_rows(await transaction.query_raw(READ_SETTINGS_SQL, "general_settings"))
        if isinstance(stored, GrantFailure) or _routes(stored) != planned:
            raise RuntimeError("Pass-through route update verification failed")
        if any(key != "pass_through_endpoints" and stored.get(key) != value for key, value in settings.items()):
            raise RuntimeError("Pass-through route update changed other settings")
        if removed_route_id is not None:
            _ = await transaction.execute_raw(DELETE_ROUTE_OWNERSHIP_SQL, removed_route_id)
        return RoutesMutation(before=settings, after=stored)
