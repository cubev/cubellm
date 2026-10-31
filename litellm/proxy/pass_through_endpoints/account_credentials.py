import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import ip_address
from types import MappingProxyType
from typing import Annotated, Final, Self, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from litellm.models.credentials import CredentialItem

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")]


class CredentialHeader(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Annotated[str, Field(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")]
    field: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\r\n\x00]+$")]
    prefix: Annotated[str, Field(pattern=r"^[ -~]{0,32}$")] = ""

    @model_validator(mode="after")
    def validate_header(self) -> Self:
        if self.name.lower() in {
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "proxy-authorization",
            "content-type",
            "traceparent",
            "tracestate",
            "baggage",
            "accept-encoding",
        }:
            raise ValueError("Credential header cannot control HTTP routing or framing")
        return self


class AccountHeaderCredential(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    account_id: Identifier
    credential_name: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\r\n\x00]+$")]
    headers: Annotated[tuple[CredentialHeader, ...], Field(min_length=1, max_length=8)]
    allowed_query_parameters: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = ()

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if len({header.name.lower() for header in self.headers}) != len(self.headers):
            raise ValueError("Credential headers must be unique")
        if len(set(self.allowed_query_parameters)) != len(self.allowed_query_parameters):
            raise ValueError("Allowed query parameters must be unique")
        if any(
            name.casefold() in {"key", "api_key", "api-key", "token", "access_token", "authorization"}
            for name in self.allowed_query_parameters
        ):
            raise ValueError("Header credential routes cannot accept query credentials")
        return self


@dataclass(frozen=True, slots=True)
class AccountRouteBinding:
    account_id: str
    credential_name: str
    origin: str | None = None
    provider: str | None = None


class CredentialAccountInfo(BaseModel):
    model_config = ConfigDict(frozen=True)
    cube_account_id: Identifier
    cube_provider_id: Annotated[str, Field(pattern=r"^[a-z](?:[a-z0-9_-]{0,62}[a-z0-9])?$")] | None = None
    cube_allowed_origins: Annotated[tuple[str, ...], Field(min_length=1)]


def credential_origin(value: str, origin_only: bool = False) -> str:
    try:
        parsed: Final = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError("Credential origin is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.host
        or parsed.userinfo
        or parsed.query
        or parsed.fragment
        or (origin_only and parsed.path not in {"", "/"})
    ):
        raise ValueError("Credential origins must be explicit HTTP origins")
    return parsed.scheme + "://" + parsed.netloc.decode("ascii")


@dataclass(frozen=True, slots=True)
class ResolvedAccountCredential:
    binding: AccountRouteBinding
    headers: Mapping[str, str] = field(repr=False)


def validate_account_route(
    target: str,
    auth: bool,
    forward_headers: bool | None,
    configured_headers: Mapping[str, object],
    default_query_params: Mapping[str, object] | None,
    policy: AccountHeaderCredential,
    methods: Sequence[str] | None = None,
) -> None:
    parsed: Final = urlsplit(target)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.port is not None and not 1 <= parsed.port <= 65535)
    ):
        raise ValueError("Account credential routes require an HTTP origin without user information")
    if parsed.query or parsed.fragment:
        raise ValueError("Account credential route targets cannot contain query parameters or fragments")
    if parsed.scheme == "http":
        try:
            loopback: Final = ip_address(parsed.hostname).is_loopback
        except ValueError:
            raise ValueError("Account credential routes require HTTPS outside literal loopback") from None
        if not loopback:
            raise ValueError("Account credential routes require HTTPS outside literal loopback")
    if not auth or forward_headers:
        raise ValueError("Account credential routes require gateway authentication and restricted forwarding")
    if default_query_params and not set(default_query_params).issubset(policy.allowed_query_parameters):
        raise ValueError("Default query parameters must be explicitly allowed")
    configured: Final = frozenset(name.lower() for name in configured_headers)
    if configured.intersection({"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}):
        raise ValueError("Account route headers cannot control HTTP routing or framing")
    if any(header.name.lower() in configured for header in policy.headers):
        raise ValueError("Inline headers cannot shadow a named account credential")
    if not methods or any(re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Z-]{1,32}", method) is None for method in methods):
        raise ValueError("Account credential routes require explicit HTTP methods")


def validate_account_request(
    policy: AccountHeaderCredential,
    headers: Mapping[str, str],
    query_names: tuple[str, ...],
) -> None:
    if any(name.lower().startswith("x-pass-") for name in headers):
        raise ValueError("Account credential routes do not accept client header overrides")
    if not set(query_names).issubset(policy.allowed_query_parameters):
        raise ValueError("Account credential route query parameter is not allowed")


def resolve_account_credential(
    policy: AccountHeaderCredential,
    credential: CredentialItem | None,
    target: str,
) -> ResolvedAccountCredential:
    if credential is None or credential.credential_name != policy.credential_name:
        raise ValueError("Named account credential is unavailable")
    info: Final = CredentialAccountInfo.model_validate(cast(Mapping[str, object], credential.credential_info))
    if info.cube_account_id != policy.account_id:
        raise ValueError("Named credential account does not match route account")
    origin: Final = credential_origin(target)
    if origin not in tuple(credential_origin(value, origin_only=True) for value in info.cube_allowed_origins):
        raise ValueError("Named account credential is not allowed for this origin")
    values: Final = MappingProxyType(dict(cast(Mapping[str, object], credential.credential_values)))

    def header_value(header: CredentialHeader) -> str:
        raw: Final = values.get(header.field)
        text: Final = raw.get_secret_value() if isinstance(raw, SecretStr) else raw
        from litellm.secret_managers.main import get_secret_str

        value: Final = get_secret_str(text) if isinstance(text, str) and text.startswith("os.environ/") else text
        if (
            not isinstance(value, str)
            or not value
            or any(ord(character) < 32 or ord(character) > 126 for character in value)
        ):
            raise ValueError("Named account credential field is unavailable or invalid")
        return header.prefix + value

    return ResolvedAccountCredential(
        binding=AccountRouteBinding(
            account_id=policy.account_id,
            credential_name=credential.credential_name,
            origin=origin,
            provider=info.cube_provider_id,
        ),
        headers=MappingProxyType({header.name: header_value(header) for header in policy.headers}),
    )
