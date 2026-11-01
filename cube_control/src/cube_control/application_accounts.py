from dataclasses import dataclass
from typing import Annotated, Final, Literal, Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, model_validator

from cube_control.application_gateway import request_gateway
from cube_control.models import FrozenModel, Identifier

Fingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
AccountPath = Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^/[A-Za-z0-9._~!$&'()+,;=:@/-]*$")]
AccountMethods = Annotated[
    tuple[Annotated[str, Field(pattern=r"^[!#$%&'+.^_`|~0-9A-Z-]{1,32}$")], ...],
    Field(min_length=1, max_length=16),
]


def require_managed_path(path: str) -> None:
    if "//" in path or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("Account scopes require canonical concrete paths")
    if not path.startswith("/cube_api/") or path == "/cube_api/":
        raise ValueError("Managed account scopes require the dedicated /cube_api/ namespace")


class AccountRouteScope(FrozenModel):
    route_id: Identifier
    account_id: Identifier
    expected_fingerprint: Fingerprint
    path: AccountPath
    methods: AccountMethods

    @model_validator(mode="after")
    def concrete_scope(self) -> Self:
        require_managed_path(self.path)
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("Account scope methods must be unique")
        return self

    @property
    def management_path(self) -> str:
        return "/cube_control/account_routes/" + quote(self.route_id, safe="")


class AccountCapabilities(BaseModel):
    application_exact_routes: Literal["exact_path_v1"]
    passthrough_account_grants: Literal["application_account_path_method_v1"]
    passthrough_account_grant_management: Literal["atomic_append_v1"]
    passthrough_account_binding: Literal["configured_credential_v1"]
    passthrough_header_isolation: Literal[True]
    credential_values_full_masking: Literal[True]


class AccountGrant(FrozenModel):
    application_id: Identifier
    key_hash: Fingerprint
    account_id: Identifier
    path: AccountPath
    methods: AccountMethods


class AccountRoute(FrozenModel):
    route_id: str
    account_id: str
    path: str
    methods: tuple[str, ...]
    include_subpath: bool = False
    origin: str
    fingerprint: Fingerprint
    grants: tuple[AccountGrant, ...]


class AccountReceipt(FrozenModel):
    stored: Literal[True]
    enforcement: Literal["not_verified"]
    route: AccountRoute


@dataclass(frozen=True, slots=True)
class AccountAccessFailure:
    status: Literal["error", "uncertain"]
    message: str


def desired_grant(scope: AccountRouteScope, application_id: str, key_hash: str) -> AccountGrant:
    return AccountGrant(
        application_id=application_id,
        key_hash=key_hash,
        account_id=scope.account_id,
        path=scope.path,
        methods=scope.methods,
    )


def _read_scope(client: httpx.Client, scope: AccountRouteScope) -> AccountRoute | AccountAccessFailure:
    code, raw = request_gateway(client, "GET", scope.management_path)
    if code != 200:
        return AccountAccessFailure("error", "Configured account route cannot be inspected")
    route: Final = AccountRoute.model_validate_json(raw)
    if (
        route.route_id != scope.route_id
        or route.account_id != scope.account_id
        or route.fingerprint != scope.expected_fingerprint
        or not set(scope.methods).issubset(route.methods)
        or not (
            scope.path == route.path or (route.include_subpath and scope.path.startswith(route.path.rstrip("/") + "/"))
        )
    ):
        return AccountAccessFailure("error", "Account route conflicts with the prepared scope")
    return route


def _inspect_scope(
    client: httpx.Client,
    scopes: tuple[AccountRouteScope, ...],
    scope: AccountRouteScope,
    application_id: str,
    key_hash: str,
    require_grants: bool,
) -> AccountAccessFailure | None:
    route: Final = _read_scope(client, scope)
    if isinstance(route, AccountAccessFailure):
        return route
    expected: Final = tuple(
        desired_grant(candidate, application_id, key_hash)
        for candidate in scopes
        if candidate.route_id == scope.route_id
    )
    if any(grant.key_hash == key_hash and grant not in expected for grant in route.grants):
        return AccountAccessFailure("error", "Stored account grants exceed the prepared key scope")
    if require_grants and desired_grant(scope, application_id, key_hash) not in route.grants:
        return AccountAccessFailure("error", "Prepared account grant is missing; no credential delivered")
    return None


def inspect_account_routes(
    client: httpx.Client,
    scopes: tuple[AccountRouteScope, ...],
    application_id: str,
    key_hash: str,
    require_grants: bool = False,
) -> AccountAccessFailure | None:
    for failure in (
        _inspect_scope(client, scopes, scope, application_id, key_hash, require_grants) for scope in scopes
    ):
        if failure is not None:
            return failure
    return None


def _ensure_scope(
    client: httpx.Client,
    scope: AccountRouteScope,
    application_id: str,
    key_hash: str,
    allow_creation: bool,
) -> AccountAccessFailure | None:
    route: Final = _read_scope(client, scope)
    if isinstance(route, AccountAccessFailure):
        return route
    grant: Final = desired_grant(scope, application_id, key_hash)
    if grant in route.grants:
        return None
    if not allow_creation:
        return AccountAccessFailure("error", "Prepared account grant is missing; delivery cannot create it")
    code, raw = request_gateway(
        client,
        "POST",
        scope.management_path + "/grants",
        {"expected_fingerprint": scope.expected_fingerprint, "grant": grant.model_dump(mode="json")},
    )
    if code != 200:
        return AccountAccessFailure("uncertain", "Account grant storage requires reconciliation")
    receipt: Final = AccountReceipt.model_validate_json(raw)
    if receipt.route.fingerprint != scope.expected_fingerprint or grant not in receipt.route.grants:
        return AccountAccessFailure("uncertain", "Account grant receipt conflicts with the prepared scope")
    return None


def ensure_account_grants(
    client: httpx.Client,
    scopes: tuple[AccountRouteScope, ...],
    application_id: str,
    key_hash: str,
    allow_creation: bool,
) -> AccountAccessFailure | None:
    for failure in (_ensure_scope(client, scope, application_id, key_hash, allow_creation) for scope in scopes):
        if failure is not None:
            return failure
    return inspect_account_routes(client, scopes, application_id, key_hash, require_grants=True)
