import fcntl
import json
import os
import secrets
import stat
from collections.abc import Generator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, JsonValue, SecretStr, field_validator, model_validator

from cube_control.application_accounts import (
    AccountCapabilities,
    AccountRouteScope,
    ensure_account_grants,
    inspect_account_routes,
)
from cube_control.application_gateway import request_gateway as _request
from cube_control.models import FrozenModel, Identifier

InferenceRoute = Literal[
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/embeddings",
    "/v1/images/generations",
    "/v1/audio/transcriptions",
    "/v1/audio/speech",
    "/v1/messages",
    "/v1/models",
]


class ApplicationAccessSpec(FrozenModel):
    application_id: Identifier
    team_id: Identifier
    gateway_origin: str
    models: Annotated[tuple[Identifier, ...], Field(max_length=100)] = ()
    routes: Annotated[tuple[InferenceRoute, ...], Field(max_length=8)] = ()
    account_routes: Annotated[tuple[AccountRouteScope, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        origin: Final = urlsplit(self.gateway_origin)
        if (
            origin.scheme != "http"
            or origin.hostname not in ("127.0.0.1", "::1")
            or origin.username is not None
            or origin.password is not None
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
            or origin.port is None
        ):
            raise ValueError("Administrative gateway access requires an explicit loopback HTTP origin and port")
        if len(set(self.models)) != len(self.models) or len(set(self.routes)) != len(self.routes):
            raise ValueError("Application models and routes must be unique")
        if "all-proxy-models" in self.models:
            raise ValueError("Application models must be explicit model aliases")
        if bool(self.models) != bool(self.routes) or not (self.routes or self.account_routes):
            raise ValueError("Application scope requires paired inference models/routes or account routes")
        if len(set(self.account_paths)) != len(self.account_paths):
            raise ValueError("Account route paths must be unique")
        return self

    @property
    def account_paths(self) -> tuple[str, ...]:
        return tuple(scope.path for scope in self.account_routes)

    @property
    def key_routes(self) -> tuple[str, ...]:
        return (*self.routes, *("exact:" + path for path in self.account_paths))

    @property
    def user_id(self) -> str:
        return "cubellm-app-" + sha256(self.application_id.encode()).hexdigest()[:32]


class ApplicationAccessState(FrozenModel):
    spec: ApplicationAccessSpec
    operation_id: str
    token: SecretStr
    user_attempted: bool = False
    key_attempted: bool = False
    account_grants_attempted: bool = False
    revocation_requested: bool = False
    block_attempted: bool = False

    @property
    def key_hash(self) -> str:
        return sha256(self.token.get_secret_value().encode()).hexdigest()

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "cube_application_id": self.spec.application_id,
            "cube_access_operation": self.operation_id,
            "cube_access_manager": "cubellm-v1",
        }

    @property
    def key_metadata(self) -> dict[str, JsonValue]:
        return {
            **self.metadata,
            **({"allowed_passthrough_routes": list(self.spec.account_paths)} if self.spec.account_routes else {}),
        }


class ApplicationAccessResult(FrozenModel):
    application_id: str
    status: Literal["prepared", "provisioned", "delivered", "uncertain", "error"]
    enforcement: Literal["not_verified"] = "not_verified"
    message: str


class ApplicationRevocationResult(FrozenModel):
    application_id: str
    status: Literal["blocked", "deleted", "uncertain", "error"]
    enforcement: Literal["not_verified"] = "not_verified"
    message: str


def _require_private(descriptor: int, mode: int, directory: bool = False) -> None:
    info: Final = os.fstat(descriptor)
    correct_type: Final = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        raise ValueError("Application state must be private, owned by the current user and of the expected type")


@contextmanager
def _locked_directory(path: Path) -> Generator[int]:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    descriptor: Final = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _require_private(descriptor, 0o700, directory=True)
        lock: Final = os.open(".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=descriptor)
        try:
            _require_private(lock, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield descriptor
        finally:
            os.close(lock)
    finally:
        os.close(descriptor)


def _load(descriptor: int) -> ApplicationAccessState | None:
    try:
        state_fd: Final = os.open("state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    except FileNotFoundError:
        return None
    with os.fdopen(state_fd, encoding="utf-8") as stream:
        _require_private(stream.fileno(), 0o600)
        raw: Final = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Application state exceeds size limit")
    return ApplicationAccessState.model_validate_json(raw)


def _save(descriptor: int, state: ApplicationAccessState) -> None:
    name: Final = ".state-" + secrets.token_hex(16)
    raw: Final = json.dumps(
        state.model_dump(mode="json") | {"token": state.token.get_secret_value()}, separators=(",", ":")
    )
    if len(raw) > 65536:
        raise ValueError("Application state exceeds size limit")
    state_fd: Final = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
    try:
        with os.fdopen(state_fd, "w", encoding="utf-8") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, "state.json", src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        try:
            os.unlink(name, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def prepare_application_access(spec: ApplicationAccessSpec, directory: Path) -> ApplicationAccessResult:
    try:
        with _locked_directory(directory) as descriptor:
            existing: Final = _load(descriptor)
            if existing is not None and existing.spec != spec:
                return ApplicationAccessResult(
                    application_id=spec.application_id, status="error", message="State scope conflicts"
                )
            if existing is not None and existing.revocation_requested:
                return ApplicationAccessResult(
                    application_id=spec.application_id,
                    status="error",
                    message="Application revocation was requested",
                )
            if existing is None:
                _save(
                    descriptor,
                    ApplicationAccessState(
                        spec=spec, operation_id=str(uuid4()), token=SecretStr("sk-" + secrets.token_urlsafe(32))
                    ),
                )
        return ApplicationAccessResult(
            application_id=spec.application_id, status="prepared", message="Prepared privately; no gateway change made"
        )
    except (OSError, ValueError):
        return ApplicationAccessResult(
            application_id=spec.application_id, status="error", message="Private application state unavailable"
        )


class _Capabilities(BaseModel):
    schema_version: Literal[1]
    application_identity: Literal["authenticated_object_v1"]
    virtual_key_log_redaction: Literal[True]
    custom_keys_allowed: Literal[True]
    client_side_credentials_allowed: Literal[False]


class _Principal(BaseModel):
    user_id: str
    user_role: Literal["internal_user"]
    models: tuple[str, ...]
    teams: tuple[str, ...]
    metadata: dict[str, str]
    user_email: None = None
    sso_user_id: None = None
    object_permission_id: None = None


class _UserInfo(BaseModel):
    user_info: _Principal


class _Member(BaseModel):
    user_id: str | None = None
    role: str


class _Team(BaseModel):
    team_id: str
    models: tuple[str, ...]
    blocked: Literal[False] = False
    members_with_roles: tuple[_Member, ...]

    @field_validator("members_with_roles", mode="before")
    @classmethod
    def empty_members(cls, value: object) -> object:
        return () if value == {} else value


class _TeamInfo(BaseModel):
    team_info: _Team


class _ModelParameters(BaseModel):
    configurable_clientside_auth_params: tuple[object, ...] | None = None


class _Model(BaseModel):
    model_name: str
    litellm_params: _ModelParameters


class _ModelInfo(BaseModel):
    data: tuple[_Model, ...]


class _Key(BaseModel):
    user_id: str
    team_id: str
    models: tuple[str, ...]
    allowed_routes: tuple[str, ...]
    metadata: dict[str, JsonValue]
    status: Literal["active"]
    blocked: Literal[False] | None = None
    expires: None = None
    auto_rotate: Literal[False] = False
    allowed_passthrough_routes: tuple[str, ...] | None = None
    aliases: dict[str, str] = Field(default_factory=dict)
    config: dict[str, object] = Field(default_factory=dict)
    object_permission_id: None = None


class _KeyInfo(BaseModel):
    info: _Key


def _matches_user(state: ApplicationAccessState, raw: bytes) -> bool:
    user: Final = _UserInfo.model_validate_json(raw).user_info
    return (
        user.user_id == state.spec.user_id
        and user.models == state.spec.models
        and user.teams == (state.spec.team_id,)
        and user.metadata == state.metadata
    )


def _matches_key(state: ApplicationAccessState, raw: bytes) -> bool:
    key: Final = _KeyInfo.model_validate_json(raw).info
    return (
        key.user_id == state.spec.user_id
        and key.team_id == state.spec.team_id
        and key.models == state.spec.models
        and key.allowed_routes == state.spec.key_routes
        and key.metadata == state.key_metadata
        and key.allowed_passthrough_routes in (None, state.spec.account_paths)
        and not key.aliases
        and not key.config
    )


def _provision(
    client: httpx.Client,
    descriptor: int,
    state: ApplicationAccessState,
    allow_creation: bool = True,
) -> ApplicationAccessResult:
    def result(status: Literal["provisioned", "uncertain", "error"], message: str) -> ApplicationAccessResult:
        return ApplicationAccessResult(application_id=state.spec.application_id, status=status, message=message)

    if state.revocation_requested:
        return result("error", "Application revocation was requested; provisioning and delivery are disabled")
    caps_code, caps_raw = _request(client, "GET", "/cube_control/capabilities")
    if caps_code != 200:
        return result("error", "Gateway provisioning capabilities unavailable")
    _Capabilities.model_validate_json(caps_raw)
    if state.spec.account_routes:
        AccountCapabilities.model_validate_json(caps_raw)
        account_preflight: Final = inspect_account_routes(
            client, state.spec.account_routes, state.spec.application_id, state.key_hash
        )
        if account_preflight is not None:
            return result(account_preflight.status, account_preflight.message)
    team_code, team_raw = _request(client, "GET", "/team/info", params={"team_id": state.spec.team_id})
    if team_code != 200:
        return result("error", "Application team is unavailable")
    team: Final = _TeamInfo.model_validate_json(team_raw).team_info
    if team.team_id != state.spec.team_id or (team.models and not set(state.spec.models).issubset(team.models)):
        return result("error", "Application models are outside the team's explicit scope")
    if state.spec.models:
        models_code, models_raw = _request(client, "GET", "/model/info")
        if models_code != 200:
            return result("error", "Configured application models cannot be inspected")
        model_info: Final = _ModelInfo.model_validate_json(models_raw)
        configured: Final = frozenset(model.model_name for model in model_info.data)
        if not set(state.spec.models).issubset(configured):
            return result("error", "Application models are not configured gateway aliases")
        if any(
            bool(model.litellm_params.configurable_clientside_auth_params)
            for model in model_info.data
            if model.model_name in state.spec.models
        ):
            return result("error", "Application models allow client credential overrides")
    user_code, user_raw = _request(client, "GET", "/user/info", params={"user_id": state.spec.user_id})
    if user_code == 404:
        if not allow_creation:
            return result("error", "Application principal is missing; delivery cannot create it")
        if state.user_attempted:
            return result("uncertain", "Previous user creation requires reconciliation; no mutation repeated")
        attempted_user: Final = state.model_copy(update={"user_attempted": True})
        _save(descriptor, attempted_user)
        code, _ = _request(
            client,
            "POST",
            "/user/new",
            {
                "user_id": state.spec.user_id,
                "user_role": "internal_user",
                "teams": [state.spec.team_id],
                "models": list(state.spec.models),
                "metadata": state.metadata,
                "auto_create_key": False,
                "send_invite_email": False,
            },
        )
        if code not in (200, 201):
            return result("uncertain", "User creation requires reconciliation")
        return _provision(client, descriptor, attempted_user)
    if user_code != 200 or not _matches_user(state, user_raw):
        return result("error", "Application principal conflicts with the prepared scope")
    member_roles: Final = tuple(
        member.role for member in team.members_with_roles if member.user_id == state.spec.user_id
    )
    if member_roles != ("user",):
        return result("uncertain", "Application team membership requires reconciliation; no key created or changed")
    key_code, key_raw = _request(client, "GET", "/key/info", params={"key": state.key_hash})
    if key_code == 200:
        if not _matches_key(state, key_raw):
            return result("error", "Application key conflicts with the prepared scope")
        if state.spec.account_routes:
            if allow_creation and not state.account_grants_attempted:
                _save(descriptor, state.model_copy(update={"account_grants_attempted": True}))
            account_result: Final = ensure_account_grants(
                client, state.spec.account_routes, state.spec.application_id, state.key_hash, allow_creation
            )
            if account_result is not None:
                return result(account_result.status, account_result.message)
            return result(
                "provisioned", "Application credential and account grants stored; runtime enforcement unverified"
            )
        return result("provisioned", "Application credential and scope verified")
    if key_code != 404:
        return result("error", "Application key cannot be inspected")
    if not allow_creation:
        return result("error", "Application key is missing; delivery cannot create it")
    if state.key_attempted:
        return result("uncertain", "Previous key creation requires reconciliation; no mutation repeated")
    attempted_key: Final = state.model_copy(update={"key_attempted": True})
    _save(descriptor, attempted_key)
    code, _ = _request(
        client,
        "POST",
        "/key/generate",
        {
            "key": state.token.get_secret_value(),
            "user_id": state.spec.user_id,
            "team_id": state.spec.team_id,
            "models": list(state.spec.models),
            "allowed_routes": list(state.spec.key_routes),
            "key_type": "default",
            "metadata": state.key_metadata,
            "send_invite_email": False,
            "auto_rotate": False,
        },
    )
    if code not in (200, 201):
        return result("uncertain", "Key creation requires reconciliation")
    return _provision(client, descriptor, attempted_key)


def provision_application_access(
    directory: Path,
    master_key: SecretStr,
    transport: httpx.BaseTransport | None = None,
) -> ApplicationAccessResult:
    try:
        with _locked_directory(directory) as descriptor:
            state: Final = _load(descriptor)
            if state is None:
                return ApplicationAccessResult(
                    application_id="unknown", status="error", message="Prepare application state first"
                )
            with httpx.Client(
                base_url=state.spec.gateway_origin,
                headers={"Authorization": "Bearer " + master_key.get_secret_value()},
                timeout=20,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                return _provision(client, descriptor, state)
    except httpx.HTTPError:
        return ApplicationAccessResult(
            application_id="unknown", status="uncertain", message="Gateway request failed; reconcile before any retry"
        )
    except (OSError, ValueError):
        return ApplicationAccessResult(
            application_id="unknown", status="error", message="Application state or gateway response rejected"
        )


def _deliver_key(descriptor: int, token: SecretStr) -> None:
    content: Final = token.get_secret_value() + "\n"
    try:
        existing_fd: Final = os.open("api-key", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    except FileNotFoundError:
        temporary: Final = ".key-" + secrets.token_hex(16)
        temporary_fd: Final = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        try:
            with os.fdopen(temporary_fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, "api-key", src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
            os.fsync(descriptor)
        finally:
            os.unlink(temporary, dir_fd=descriptor)
            os.fsync(descriptor)
        return
    with os.fdopen(existing_fd, encoding="utf-8") as stream:
        _require_private(stream.fileno(), 0o600)
        if stream.read(8193) != content:
            raise ValueError("Destination contains a different credential")


def deliver_application_access(
    directory: Path,
    destination_directory: Path,
    master_key: SecretStr,
    transport: httpx.BaseTransport | None = None,
) -> ApplicationAccessResult:
    try:
        with _locked_directory(directory) as descriptor:
            state: Final = _load(descriptor)
            if state is None:
                return ApplicationAccessResult(
                    application_id="unknown",
                    status="error",
                    message="Prepare and provision application state first",
                )
            with httpx.Client(
                base_url=state.spec.gateway_origin,
                headers={"Authorization": "Bearer " + master_key.get_secret_value()},
                timeout=20,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                verified: Final = _provision(client, descriptor, state, allow_creation=False)
            if verified.status != "provisioned":
                return verified
            with _locked_directory(destination_directory) as destination:
                _deliver_key(destination, state.token)
            return ApplicationAccessResult(
                application_id=state.spec.application_id,
                status="delivered",
                message="Verified application credential delivered to private api-key file",
            )
    except httpx.HTTPError:
        return ApplicationAccessResult(
            application_id="unknown",
            status="uncertain",
            message="Gateway verification failed; no credential delivered",
        )
    except (OSError, ValueError):
        return ApplicationAccessResult(
            application_id="unknown",
            status="error",
            message="Application verification or private delivery failed",
        )


class _RevocationCapabilities(BaseModel):
    schema_version: Literal[1]
    application_identity: Literal["authenticated_object_v1"]
    virtual_key_log_redaction: Literal[True]


class _OwnedKey(BaseModel):
    user_id: str
    team_id: str
    metadata: dict[str, JsonValue]
    status: Literal["active", "expired", "revoked", "deleted"]
    blocked: bool | None = None


class _OwnedKeyInfo(BaseModel):
    info: _OwnedKey


def _revoke(client: httpx.Client, descriptor: int, state: ApplicationAccessState) -> ApplicationRevocationResult:
    def result(
        status: Literal["blocked", "deleted", "uncertain", "error"], message: str
    ) -> ApplicationRevocationResult:
        return ApplicationRevocationResult(application_id=state.spec.application_id, status=status, message=message)

    capabilities_code, capabilities_raw = _request(client, "GET", "/cube_control/capabilities")
    if capabilities_code != 200:
        return result("error", "Gateway capabilities unavailable; revocation intent remains recorded")
    _RevocationCapabilities.model_validate_json(capabilities_raw)
    key_code, key_raw = _request(client, "GET", "/key/info", params={"key": state.key_hash})
    if key_code != 200:
        return result("uncertain", "Application key cannot be inspected; runtime denial is unverified")
    key: Final = _OwnedKeyInfo.model_validate_json(key_raw).info
    if key.user_id != state.spec.user_id or key.team_id != state.spec.team_id or key.metadata != state.key_metadata:
        return result("error", "Key ownership conflicts with the prepared application; no key changed")
    if key.status == "deleted":
        return result("deleted", "Key deletion is recorded; cached authentication denial is unverified")
    if key.status == "revoked" and key.blocked is True:
        return result("blocked", "Key block is recorded; cached authentication denial is unverified")
    if key.blocked is True or key.status == "revoked":
        return result("error", "Gateway key status is inconsistent")
    if state.block_attempted:
        return result("uncertain", "Previous block requires reconciliation; no mutation repeated")
    attempted: Final = state.model_copy(update={"block_attempted": True})
    _save(descriptor, attempted)
    code, _ = _request(client, "POST", "/key/block", {"key": state.key_hash})
    if code not in (200, 201):
        return result("uncertain", "Key block requires reconciliation; runtime denial is unverified")
    return _revoke(client, descriptor, attempted)


def revoke_application_access(
    directory: Path,
    master_key: SecretStr,
    transport: httpx.BaseTransport | None = None,
) -> ApplicationRevocationResult:
    try:
        with _locked_directory(directory) as descriptor:
            existing: Final = _load(descriptor)
            if existing is None:
                return ApplicationRevocationResult(
                    application_id="unknown",
                    status="error",
                    message="Application state is missing",
                )
            requested: Final = existing.model_copy(update={"revocation_requested": True})
            _save(descriptor, requested)
            with httpx.Client(
                base_url=requested.spec.gateway_origin,
                headers={"Authorization": "Bearer " + master_key.get_secret_value()},
                timeout=20,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                return _revoke(client, descriptor, requested)
    except httpx.HTTPError:
        return ApplicationRevocationResult(
            application_id="unknown",
            status="uncertain",
            message="Gateway request failed; reconcile the recorded revocation",
        )
    except (OSError, ValueError):
        return ApplicationRevocationResult(
            application_id="unknown",
            status="error",
            message="Application state or gateway revocation response rejected",
        )
