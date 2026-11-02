import unicodedata
from typing import Annotated, Final, Literal, Self
from urllib.parse import quote, unquote

import httpx
from pydantic import BaseModel, Field, JsonValue, SecretStr, StrictInt, StrictStr, ValidationError, model_validator

from cube_control.application_accounts import (
    AccountCapabilities,
    AccountMethods,
    AccountPath,
    AccountResourceCapabilities,
    AccountRoute,
    Fingerprint,
    has_placeholder,
    is_resource_template,
    require_managed_path,
)
from cube_control.application_gateway import gateway_client, request_gateway, require_admin_origin
from cube_control.models import FrozenModel, Identifier


def _has_blank_or_control(text: str) -> bool:
    return any(character.isspace() or unicodedata.category(character) == "Cc" for character in text)


def _target_url(target: str) -> httpx.URL:
    try:
        url: Final = httpx.URL(target)
    except httpx.InvalidURL:
        raise ValueError("Account route target is not a valid URL") from None
    if (
        url.scheme not in ("http", "https")
        or not url.host
        or _has_blank_or_control(target)
        or _has_blank_or_control(unquote(url.host))
        or url.userinfo
        or url.query
        or url.fragment
        or (url.port is not None and not 1 <= url.port <= 65535)
    ):
        raise ValueError("Account route targets require an explicit HTTP origin without credentials or query")
    return url


class AccountRouteHeader(FrozenModel):
    name: Annotated[str, Field(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")]
    field: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\r\n\x00]+$")]
    prefix: Annotated[str, Field(pattern=r"^[ -~]{0,32}$")] = ""


class AccountRouteQuery(FrozenModel):
    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9._~-]{1,128}$")]
    field: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\r\n\x00]+$")]
    prefix: Annotated[str, Field(pattern=r"^[ -~]{0,32}$")] = ""


ResourceTypeName = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")]
ResourceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._~:@-]{1,256}$")]
ResourceSelector = Annotated[
    tuple[Annotated[StrictStr, Field(min_length=1, max_length=128)] | Annotated[StrictInt, Field(ge=0, le=1024)], ...],
    Field(min_length=1, max_length=8),
]


class AccountResourceCreation(FrozenModel):
    path: AccountPath
    methods: AccountMethods
    id_selector: ResourceSelector

    @model_validator(mode="after")
    def validate_creation(self) -> Self:
        require_managed_path(self.path)
        if has_placeholder(self.path) or len(set(self.methods)) != len(self.methods):
            raise ValueError("Resource creation needs a concrete path and unique methods")
        return self


class AccountResourceType(FrozenModel):
    name: ResourceTypeName
    item_paths: Annotated[tuple[AccountPath, ...], Field(min_length=1, max_length=16)]
    creation: AccountResourceCreation | None = None

    @model_validator(mode="after")
    def validate_resource_type(self) -> Self:
        for path in self.item_paths:
            require_managed_path(path)
        if not all(is_resource_template(path) for path in self.item_paths):
            raise ValueError("Resource item paths need exactly one {resource_id} segment")
        return self


class AccountRouteSpec(FrozenModel):
    gateway_origin: str
    route_id: Identifier
    account_id: Identifier
    credential_name: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\r\n\x00]+$")]
    headers: Annotated[tuple[AccountRouteHeader, ...], Field(max_length=8)] = ()
    query: AccountRouteQuery | None = None
    allowed_query_parameters: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    target: Annotated[str, Field(min_length=1, max_length=2048)]
    path: AccountPath
    methods: AccountMethods
    include_subpath: bool = False
    resource_types: Annotated[tuple[AccountResourceType, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        require_admin_origin(self.gateway_origin)
        require_managed_path(self.path)
        if not self.headers and self.query is None:
            raise ValueError("Account routes need a credential header or query parameter")
        if self.query is not None and any(
            name.casefold() == self.query.name.casefold() for name in self.allowed_query_parameters
        ):
            raise ValueError("The credential query parameter cannot also be a client query parameter")
        if self.path.endswith("/"):
            raise ValueError("Managed account routes cannot end with a slash")
        if has_placeholder(self.path):
            raise ValueError("Account route base paths must be concrete")
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("Account route methods must be unique")
        _ = _target_url(self.target)
        if self.resource_types and not self.include_subpath:
            raise ValueError("Account resources require a route that forwards subpaths")
        if len({resource.name for resource in self.resource_types}) != len(self.resource_types):
            raise ValueError("Account resource type names must be unique")
        return self

    @property
    def origin(self) -> str:
        target: Final = _target_url(self.target)
        return target.scheme + "://" + target.netloc.decode("ascii")

    def command(self) -> dict[str, JsonValue]:
        return {
            "route": {
                "id": self.route_id,
                "path": self.path,
                "target": self.target,
                "methods": list(self.methods),
                "include_subpath": self.include_subpath,
                "account_credential": {
                    "account_id": self.account_id,
                    "credential_name": self.credential_name,
                    "headers": [header.model_dump(mode="json") for header in self.headers],
                    **({"query": self.query.model_dump(mode="json")} if self.query is not None else {}),
                    "allowed_query_parameters": list(self.allowed_query_parameters),
                    **(
                        {
                            "resource_types": [
                                resource.model_dump(mode="json", exclude_none=True) for resource in self.resource_types
                            ]
                        }
                        if self.resource_types
                        else {}
                    ),
                },
            }
        }


class AccountRouteResult(FrozenModel):
    route_id: str
    status: Literal["created", "existing", "uncertain", "error"]
    enforcement: Literal["not_verified"] = "not_verified"
    fingerprint: Fingerprint | None = None
    message: str


class _RouteCapabilities(AccountCapabilities):
    passthrough_account_route_management: Literal["atomic_create_v1"]


class _QueryCredentialCapabilities(_RouteCapabilities):
    passthrough_account_query_credential: Literal["query_parameter_v1"]


class _RouteReceipt(BaseModel):
    stored: Literal[True]
    created: bool
    enforcement: Literal["not_verified"]
    route: AccountRoute


_REJECTIONS: Final = {
    400: "Gateway rejected the account route definition",
    409: "Account route conflicts with an existing route, path or router pattern; nothing stored",
    422: "Named credential is unavailable for this account and target origin; nothing stored",
}


def _ensure(client: httpx.Client, spec: AccountRouteSpec) -> AccountRouteResult:
    def result(status: Literal["created", "existing", "uncertain", "error"], message: str) -> AccountRouteResult:
        return AccountRouteResult(route_id=spec.route_id, status=status, message=message)

    caps_code, caps_raw = request_gateway(client, "GET", "/cube_control/capabilities")
    if caps_code != 200:
        return result("error", "Gateway route management capabilities unavailable")
    _RouteCapabilities.model_validate_json(caps_raw)
    if spec.query is not None:
        try:
            _ = _QueryCredentialCapabilities.model_validate_json(caps_raw)
        except ValidationError:
            return result("error", "Gateway does not support query parameter credentials; nothing stored")
    if spec.resource_types:
        AccountResourceCapabilities.model_validate_json(caps_raw)
    code, raw = request_gateway(client, "POST", "/cube_control/account_routes", spec.command())
    rejection: Final = _REJECTIONS.get(code)
    if rejection is not None:
        return result("error", rejection)
    if code != 200:
        return result("uncertain", "Account route storage is unconfirmed; repeating the identical request is safe")
    receipt: Final = _RouteReceipt.model_validate_json(raw)
    route: Final = receipt.route
    if (
        route.route_id != spec.route_id
        or route.account_id != spec.account_id
        or route.path != spec.path
        or route.methods != spec.methods
        or route.include_subpath != spec.include_subpath
        or route.origin != spec.origin
        or any(grant.account_id != spec.account_id for grant in route.grants)
    ):
        return result("error", "Gateway route receipt conflicts with the requested definition")
    return AccountRouteResult(
        route_id=spec.route_id,
        status="created" if receipt.created else "existing",
        fingerprint=route.fingerprint,
        message="Account route stored; runtime enforcement and provider access unverified",
    )


def ensure_account_route(
    spec: AccountRouteSpec, master_key: SecretStr, transport: httpx.BaseTransport | None = None
) -> AccountRouteResult:
    try:
        with gateway_client(spec.gateway_origin, master_key, transport) as client:
            return _ensure(client, spec)
    except httpx.HTTPError:
        return AccountRouteResult(
            route_id=spec.route_id,
            status="uncertain",
            message="Gateway request failed; repeating the identical request is safe",
        )
    except ValueError:
        return AccountRouteResult(route_id=spec.route_id, status="error", message="Gateway response rejected")


class AccountResourceAdoptionSpec(FrozenModel):
    gateway_origin: str
    route_id: Identifier
    expected_fingerprint: Fingerprint
    resource_type: ResourceTypeName
    resource_id: ResourceId
    application_id: Identifier

    @model_validator(mode="after")
    def validate_adoption(self) -> Self:
        require_admin_origin(self.gateway_origin)
        if self.resource_id in (".", ".."):
            raise ValueError("Resource ids must be one canonical path segment")
        return self

    def command(self) -> dict[str, JsonValue]:
        return {
            "expected_fingerprint": self.expected_fingerprint,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "application_id": self.application_id,
        }


class AccountResourceAdoptionResult(FrozenModel):
    route_id: str
    resource_type: str
    resource_id: str
    status: Literal["adopted", "existing", "conflict", "uncertain", "error"]
    enforcement: Literal["not_verified"] = "not_verified"
    message: str


class _Ownership(BaseModel):
    route_id: str
    owner_application_id: str
    provenance: Literal["created", "adopted"]


class _AdoptionReceipt(BaseModel):
    stored: Literal[True]
    inserted: bool
    enforcement: Literal["not_verified"]
    ownership: _Ownership


_ADOPTION_REJECTIONS: Final = {
    400: "Resource type is not declared on this account route; nothing stored",
    404: "Account route does not exist; nothing stored",
    409: "Route changed since the expected fingerprint or the resource belongs to another application",
    422: "Gateway rejected the adoption request",
}


def _adopt(client: httpx.Client, spec: AccountResourceAdoptionSpec) -> AccountResourceAdoptionResult:
    def result(
        status: Literal["adopted", "existing", "conflict", "uncertain", "error"], message: str
    ) -> AccountResourceAdoptionResult:
        return AccountResourceAdoptionResult(
            route_id=spec.route_id,
            resource_type=spec.resource_type,
            resource_id=spec.resource_id,
            status=status,
            message=message,
        )

    caps_code, caps_raw = request_gateway(client, "GET", "/cube_control/capabilities")
    if caps_code != 200:
        return result("error", "Gateway resource capabilities unavailable")
    AccountResourceCapabilities.model_validate_json(caps_raw)
    code, raw = request_gateway(
        client, "POST", "/cube_control/account_routes/" + quote(spec.route_id, safe="") + "/resources", spec.command()
    )
    rejection: Final = _ADOPTION_REJECTIONS.get(code)
    if rejection is not None:
        return result("conflict" if code == 409 else "error", rejection)
    if code != 200:
        return result("uncertain", "Resource adoption is unconfirmed; repeating the identical request is safe")
    receipt: Final = _AdoptionReceipt.model_validate_json(raw)
    if receipt.ownership.route_id != spec.route_id or receipt.ownership.owner_application_id != spec.application_id:
        return result("error", "Gateway ownership receipt conflicts with the requested adoption")
    return result(
        "adopted" if receipt.inserted else "existing",
        "Resource owned by the application; runtime enforcement unverified",
    )


def adopt_account_resource(
    spec: AccountResourceAdoptionSpec, master_key: SecretStr, transport: httpx.BaseTransport | None = None
) -> AccountResourceAdoptionResult:
    try:
        with gateway_client(spec.gateway_origin, master_key, transport) as client:
            return _adopt(client, spec)
    except httpx.HTTPError:
        return AccountResourceAdoptionResult(
            route_id=spec.route_id,
            resource_type=spec.resource_type,
            resource_id=spec.resource_id,
            status="uncertain",
            message="Gateway request failed; repeating the identical request is safe",
        )
    except ValueError:
        return AccountResourceAdoptionResult(
            route_id=spec.route_id,
            resource_type=spec.resource_type,
            resource_id=spec.resource_id,
            status="error",
            message="Gateway response rejected",
        )
