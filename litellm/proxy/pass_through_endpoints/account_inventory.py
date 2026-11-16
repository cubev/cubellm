from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from types import MappingProxyType
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from .account_credentials import AccountHeaderCredential, CredentialAccountInfo
from .account_grants import READ_SETTINGS_SQL, GrantDatabase

Origin: TypeAlias = Literal["runtime", "database"]
SourceName: TypeAlias = Literal["credentials", "routes", "deployments"]
AuthKind: TypeAlias = Literal[
    "credential_reference", "environment_reference", "inline_api_key", "stored_api_key", "cloud_identity", "unknown"
]

STORED_CREDENTIALS_SQL: Final = (
    'SELECT "credential_name", "credential_info" FROM "LiteLLM_CredentialsTable" ORDER BY "credential_name"'
)
STORED_DEPLOYMENTS_SQL: Final = """SELECT "model_id", "model_name", ARRAY(
    SELECT jsonb_object_keys(CASE jsonb_typeof(params.value) WHEN 'object' THEN params.value ELSE '{}'::jsonb END)
) AS "parameter_names"
FROM "LiteLLM_ProxyModelTable" CROSS JOIN LATERAL (
    SELECT CASE jsonb_typeof("litellm_params") WHEN 'string' THEN ("litellm_params" #>> '{}')::jsonb
        ELSE "litellm_params" END AS value
) AS params
ORDER BY "model_name", "model_id\""""
_CLOUD_IDENTITY_PARAMETERS: Final = frozenset(
    (
        "aws_access_key_id",
        "aws_role_name",
        "aws_profile_name",
        "aws_web_identity_token",
        "aws_session_token",
        "vertex_credentials",
        "vertex_project",
        "azure_ad_token",
        "azure_ad_token_provider",
        "use_azure_ad",
        "tenant_id",
        "client_id",
    )
)


class InventorySource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: SourceName
    origin: Origin
    status: Literal["complete", "partial", "unavailable"]
    records: Annotated[int, Field(ge=0)] = 0
    rejected: Annotated[int, Field(ge=0)] = 0
    detail: Literal["", "unreadable", "database_unavailable", "database_read_failed"] = ""


class InventoryRejection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: SourceName
    origin: Origin
    index: Annotated[int, Field(ge=0)]
    name: str | None = None
    reason: Literal["not_an_object", "unreadable_fields"]


class InventoryCredential(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    origin: Origin
    credential_name: str
    account_id: str | None = None
    provider_id: str | None = None
    llm_provider: str | None = None
    allowed_origins: tuple[str, ...] = ()
    binding: Literal["bound", "unbound", "invalid"]


class InventoryRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
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


class InventoryDeployment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    origin: Origin
    model_name: str
    model_id: str | None = None
    provider: str | None = None
    provider_hint: str | None = None
    credential_name: str | None = None
    auth: AuthKind


class GatewayAccountInventory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    observed_at: AwareDatetime
    sources: tuple[InventorySource, ...]
    credentials: tuple[InventoryCredential, ...]
    routes: tuple[InventoryRoute, ...]
    deployments: tuple[InventoryDeployment, ...]
    rejected: tuple[InventoryRejection, ...]


@dataclass(frozen=True, slots=True)
class StoredUnavailable:
    detail: Literal["database_unavailable", "database_read_failed"]


@dataclass(frozen=True, slots=True)
class StoredRows:
    settings: object
    credentials: object
    deployments: object


_OBJECT: Final[TypeAdapter[Mapping[str, JsonValue]]] = TypeAdapter(Mapping[str, JsonValue])
_SETTINGS_ROWS: Final[TypeAdapter[tuple[Mapping[str, JsonValue], ...]]] = TypeAdapter(
    tuple[Mapping[str, JsonValue], ...]
)
InventoryRecord: TypeAlias = InventoryCredential | InventoryRoute | InventoryDeployment
_EMPTY: Final[Mapping[str, JsonValue]] = MappingProxyType({})


class _CredentialMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    credential_name: str
    credential_info: Mapping[str, JsonValue] | None = None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


_FIELDS: Final[TypeAdapter[Mapping[str, object]]] = TypeAdapter(Mapping[str, object])
_ITEMS: Final[TypeAdapter[tuple[object, ...]]] = TypeAdapter(tuple[object, ...])


def _name_of(item: object, field: str) -> str | None:
    try:
        return _text(_FIELDS.validate_python(item).get(field))
    except ValidationError:
        return _text(getattr(item, field, None))


def _credential(origin: Origin, metadata: _CredentialMetadata) -> InventoryCredential:
    info: Final = metadata.credential_info if metadata.credential_info is not None else _EMPTY
    llm_provider: Final = _text(info.get("custom_llm_provider"))
    if "cube_account_id" not in info:
        return InventoryCredential(
            origin=origin, credential_name=metadata.credential_name, llm_provider=llm_provider, binding="unbound"
        )
    try:
        bound: Final = CredentialAccountInfo.model_validate(info)
    except ValidationError:
        return InventoryCredential(
            origin=origin,
            credential_name=metadata.credential_name,
            account_id=_text(info.get("cube_account_id")),
            llm_provider=llm_provider,
            binding="invalid",
        )
    return InventoryCredential(
        origin=origin,
        credential_name=metadata.credential_name,
        account_id=bound.cube_account_id,
        provider_id=bound.cube_provider_id,
        llm_provider=llm_provider,
        allowed_origins=bound.cube_allowed_origins,
        binding="bound",
    )


def _route(origin: Origin, entry: Mapping[str, JsonValue]) -> InventoryRoute:
    route_id: Final = _text(entry.get("id"))
    path: Final = _text(entry.get("path"))
    policy_value: Final = entry.get("account_credential")
    if policy_value is None:
        headers: Final = entry.get("headers")
        return InventoryRoute(
            origin=origin,
            route_id=route_id,
            path=path,
            inline_header_names=tuple(sorted(headers)) if isinstance(headers, Mapping) else (),
            binding="unbound",
        )
    try:
        policy: Final = AccountHeaderCredential.model_validate(policy_value)
    except ValidationError:
        account: Final = policy_value.get("account_id") if isinstance(policy_value, Mapping) else None
        return InventoryRoute(origin=origin, route_id=route_id, path=path, account_id=_text(account), binding="invalid")
    methods: Final = entry.get("methods")
    return InventoryRoute(
        origin=origin,
        route_id=route_id,
        path=path,
        account_id=policy.account_id,
        credential_name=policy.credential_name,
        methods=tuple(method for method in methods if isinstance(method, str)) if isinstance(methods, list) else (),
        resource_types=tuple(resource.name for resource in policy.resource_types),
        applications=tuple(sorted(frozenset(grant.application_id for grant in policy.application_grants))),
        grant_count=len(policy.application_grants),
        binding="account",
    )


def _auth(parameter_names: frozenset[str], api_key: str | None, stored: bool) -> AuthKind:
    if "litellm_credential_name" in parameter_names:
        return "credential_reference"
    if "api_key" in parameter_names:
        if stored:
            return "stored_api_key"
        return (
            "environment_reference" if api_key is not None and api_key.startswith("os.environ/") else "inline_api_key"
        )
    return "cloud_identity" if parameter_names & _CLOUD_IDENTITY_PARAMETERS else "unknown"


def _fields(value: object) -> Mapping[str, object] | None:
    try:
        return _FIELDS.validate_python(value)
    except ValidationError:
        return None


def _runtime_deployment(entry: object) -> InventoryDeployment | None:
    fields: Final = _fields(entry)
    name: Final = _text(fields.get("model_name")) if fields is not None else None
    parameters: Final = _fields(fields.get("litellm_params")) if fields is not None else None
    if name is None or parameters is None or fields is None:
        return None
    model: Final = _text(parameters.get("model")) or ""
    info: Final = _fields(fields.get("model_info"))
    return InventoryDeployment(
        origin="runtime",
        model_name=name,
        model_id=_text(info.get("id")) if info is not None else None,
        provider=_text(parameters.get("custom_llm_provider")),
        provider_hint=model.split("/", 1)[0] if "/" in model else None,
        credential_name=_text(parameters.get("litellm_credential_name")),
        auth=_auth(frozenset(parameters), _text(parameters.get("api_key")), stored=False),
    )


def _stored_deployment(entry: Mapping[str, JsonValue]) -> InventoryDeployment | None:
    name: Final = _text(entry.get("model_name"))
    names: Final = entry.get("parameter_names")
    if name is None or not isinstance(names, list):
        return None
    return InventoryDeployment(
        origin="database",
        model_name=name,
        model_id=_text(entry.get("model_id")),
        auth=_auth(frozenset(item for item in names if isinstance(item, str)), None, stored=True),
    )


@dataclass(frozen=True, slots=True)
class _Parsed:
    source: InventorySource
    records: tuple[InventoryCredential | InventoryRoute | InventoryDeployment, ...]
    rejected: tuple[InventoryRejection, ...]


def _parse(
    source: SourceName,
    origin: Origin,
    value: object,
    convert: Callable[[object], InventoryCredential | InventoryRoute | InventoryDeployment | None],
    name_field: str,
) -> _Parsed:
    if value is None:
        return _Parsed(InventorySource(name=source, origin=origin, status="complete"), (), ())
    try:
        items: Final = _ITEMS.validate_python(value)
    except ValidationError:
        return _Parsed(InventorySource(name=source, origin=origin, status="unavailable", detail="unreadable"), (), ())
    outcomes: Final = tuple((index, item, convert(item)) for index, item in enumerate(items))
    records: Final = tuple(record for _, _, record in outcomes if record is not None)
    rejected: Final = tuple(
        InventoryRejection(
            source=source,
            origin=origin,
            index=index,
            name=_name_of(item, name_field),
            reason="not_an_object"
            if not isinstance(item, Mapping) and source != "credentials"
            else "unreadable_fields",
        )
        for index, item, record in outcomes
        if record is None
    )
    return _Parsed(
        InventorySource(
            name=source,
            origin=origin,
            status="partial" if rejected else "complete",
            records=len(records),
            rejected=len(rejected),
        ),
        records,
        rejected,
    )


def _object_then(
    convert: Callable[[Mapping[str, JsonValue]], InventoryRecord | None],
) -> Callable[[object], InventoryRecord | None]:
    def converted(item: object) -> InventoryRecord | None:
        try:
            return convert(_OBJECT.validate_python(item))
        except ValidationError:
            return None

    return converted


def _credential_from(origin: Origin) -> Callable[[object], InventoryCredential | None]:
    def converted(item: object) -> InventoryCredential | None:
        try:
            return _credential(origin, _CredentialMetadata.model_validate(item, from_attributes=True))
        except ValidationError:
            return None

    return converted


_UNREADABLE: Final = "unreadable"


def _settings_routes(rows: object) -> object:
    try:
        parsed: Final = _SETTINGS_ROWS.validate_python(rows)
    except ValidationError:
        return _UNREADABLE
    if not parsed:
        return None
    if len(parsed) != 1:
        return _UNREADABLE
    value: Final = parsed[0].get("param_value")
    if isinstance(value, str):
        try:
            decoded: Final = _OBJECT.validate_json(value)
        except ValidationError:
            return _UNREADABLE
        return decoded.get("pass_through_endpoints")
    return value.get("pass_through_endpoints") if isinstance(value, Mapping) else _UNREADABLE


def _unavailable(
    origin: Origin, detail: Literal["database_unavailable", "database_read_failed"]
) -> tuple[_Parsed, ...]:
    names: Final[tuple[SourceName, ...]] = ("credentials", "routes", "deployments")
    return tuple(
        _Parsed(InventorySource(name=name, origin=origin, status="unavailable", detail=detail), (), ())
        for name in names
    )


def _stored(
    source: SourceName,
    value: object,
    convert: Callable[[object], InventoryRecord | None],
    name_field: str,
) -> _Parsed:
    if isinstance(value, StoredUnavailable):
        return _Parsed(
            InventorySource(name=source, origin="database", status="unavailable", detail=value.detail), (), ()
        )
    return _parse(source, "database", value, convert, name_field)


def build_inventory(
    observed_at: datetime,
    runtime_credentials: Iterable[object],
    runtime_routes: object,
    runtime_deployments: object,
    stored: StoredRows | StoredUnavailable,
) -> GatewayAccountInventory:
    runtime: Final = (
        _parse("credentials", "runtime", tuple(runtime_credentials), _credential_from("runtime"), "credential_name"),
        _parse("routes", "runtime", runtime_routes, _object_then(lambda entry: _route("runtime", entry)), "id"),
        _parse("deployments", "runtime", runtime_deployments, _runtime_deployment, "model_name"),
    )
    persisted: Final = (
        _unavailable("database", stored.detail)
        if isinstance(stored, StoredUnavailable)
        else (
            _stored("credentials", stored.credentials, _credential_from("database"), "credential_name"),
            _stored(
                "routes",
                stored.settings
                if isinstance(stored.settings, StoredUnavailable)
                else _settings_routes(stored.settings),
                _object_then(lambda entry: _route("database", entry)),
                "id",
            ),
            _stored("deployments", stored.deployments, _object_then(_stored_deployment), "model_name"),
        )
    )
    parsed: Final = (*runtime, *persisted)
    records: Final = tuple(chain.from_iterable(part.records for part in parsed))
    return GatewayAccountInventory(
        observed_at=observed_at,
        sources=tuple(part.source for part in parsed),
        credentials=tuple(record for record in records if isinstance(record, InventoryCredential)),
        routes=tuple(record for record in records if isinstance(record, InventoryRoute)),
        deployments=tuple(record for record in records if isinstance(record, InventoryDeployment)),
        rejected=tuple(chain.from_iterable(part.rejected for part in parsed)),
    )


async def _read_source(database: GrantDatabase, query: str, *parameters: str) -> object:
    try:
        async with database.tx() as transaction:
            return await transaction.query_raw(query, *parameters)
    except Exception:
        return StoredUnavailable("database_read_failed")


async def read_stored_rows(database: GrantDatabase | None) -> StoredRows | StoredUnavailable:
    if database is None:
        return StoredUnavailable("database_unavailable")
    return StoredRows(
        settings=await _read_source(database, READ_SETTINGS_SQL, "general_settings"),
        credentials=await _read_source(database, STORED_CREDENTIALS_SQL),
        deployments=await _read_source(database, STORED_DEPLOYMENTS_SQL),
    )
