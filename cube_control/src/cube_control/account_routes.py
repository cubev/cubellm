import unicodedata
from typing import Annotated, Final, Literal, Self
from urllib.parse import unquote

import httpx
from pydantic import BaseModel, Field, JsonValue, SecretStr, model_validator

from cube_control.application_accounts import (
    AccountCapabilities,
    AccountMethods,
    AccountPath,
    AccountRoute,
    Fingerprint,
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


class AccountRouteSpec(FrozenModel):
    gateway_origin: str
    route_id: Identifier
    account_id: Identifier
    credential_name: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\r\n\x00]+$")]
    headers: Annotated[tuple[AccountRouteHeader, ...], Field(min_length=1, max_length=8)]
    allowed_query_parameters: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    target: Annotated[str, Field(min_length=1, max_length=2048)]
    path: AccountPath
    methods: AccountMethods
    include_subpath: bool = False

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        require_admin_origin(self.gateway_origin)
        require_managed_path(self.path)
        if self.path.endswith("/"):
            raise ValueError("Managed account routes cannot end with a slash")
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("Account route methods must be unique")
        _ = _target_url(self.target)
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
                    "allowed_query_parameters": list(self.allowed_query_parameters),
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
