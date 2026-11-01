from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Final, Literal, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, JsonValue

from litellm.litellm_core_utils.credential_accessor import CredentialAccessor
from litellm.models.credentials import CredentialItem
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.config_resolvers.settings_rules import coerce_bool
from litellm.proxy.pass_through_endpoints.account_credentials import resolve_account_credential
from litellm.proxy.pass_through_endpoints.account_grants import (
    AccountGrantCommand,
    AccountGrantReceipt,
    AccountRouteDescriptor,
    GrantDatabase,
    GrantFailure,
    append_account_grant,
    read_account_route,
)
from litellm.proxy.pass_through_endpoints.account_routes import (
    AccountRouteCreateCommand,
    AccountRouteReceipt,
    create_account_route,
    serving_route_patterns,
)

router: Final = APIRouter()


class CubeControlCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    application_identity: Literal["authenticated_object_v1"] = "authenticated_object_v1"
    application_exact_routes: Literal["exact_path_v1"] = "exact_path_v1"
    passthrough_application_identity: Literal["authenticated_object_v1"] = "authenticated_object_v1"
    passthrough_header_isolation: Literal[True] = True
    passthrough_account_binding: Literal["configured_credential_v1"] = "configured_credential_v1"
    passthrough_account_provider: Literal["credential_metadata_v1"] = "credential_metadata_v1"
    passthrough_account_grants: Literal["application_account_path_method_v1"] = "application_account_path_method_v1"
    passthrough_account_grant_management: Literal["atomic_append_v1"] = "atomic_append_v1"
    passthrough_account_route_management: Literal["atomic_create_v1"] = "atomic_create_v1"
    credential_values_full_masking: Literal[True] = True
    virtual_key_log_redaction: Literal[True] = True
    custom_keys_allowed: bool
    client_side_credentials_allowed: bool


async def _require_account_admin(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> UserAPIKeyAuth:
    if user_api_key_dict.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(status_code=403, detail="Only proxy admins can manage account grants")
    return user_api_key_dict


async def account_grant_database(
    _admin: Annotated[UserAPIKeyAuth, Depends(_require_account_admin)],
) -> GrantDatabase:
    from litellm.proxy import proxy_server

    if proxy_server.prisma_client is None:
        raise HTTPException(status_code=503, detail="Account grant database is unavailable")
    settings: Final = cast(Mapping[str, JsonValue], proxy_server.general_settings)
    if proxy_server.store_model_in_db is not True and settings.get("store_model_in_db") is not True:
        raise HTTPException(status_code=409, detail="Database configuration updates are disabled")
    if proxy_server.proxy_config.settings.owned_by_config("pass_through_endpoints"):
        raise HTTPException(status_code=409, detail="Account routes are owned by the configuration file")
    return cast(GrantDatabase, proxy_server.prisma_client)


def account_credential_lookup() -> Callable[[str], CredentialItem | None]:
    return CredentialAccessor.find_credential


def account_config_invalidator() -> Callable[[str], Awaitable[None]]:
    from litellm.proxy.utils import invalidate_config_param

    return invalidate_config_param


def _raise_grant_failure(failure: GrantFailure) -> NoReturn:
    raise HTTPException(
        status_code={"not_found": 404, "conflict": 409, "invalid": 400}[failure.kind],
        detail="Account route or grant configuration was rejected",
    )


@router.get("/cube_control/account_routes/{route_id}", response_model=AccountRouteDescriptor)
async def cube_control_account_route(
    route_id: Annotated[str, Path(min_length=1, max_length=128)],
    _admin: Annotated[UserAPIKeyAuth, Depends(_require_account_admin)],
    database: Annotated[GrantDatabase, Depends(account_grant_database)],
) -> AccountRouteDescriptor:
    try:
        result: Final = await read_account_route(database, route_id)
    except Exception:
        raise HTTPException(status_code=503, detail="Account grant database request failed") from None
    if isinstance(result, GrantFailure):
        _raise_grant_failure(result)
    return result


@router.post("/cube_control/account_routes", response_model=AccountRouteReceipt)
async def cube_control_create_account_route(
    command: AccountRouteCreateCommand,
    request: Request,
    _admin: Annotated[UserAPIKeyAuth, Depends(_require_account_admin)],
    database: Annotated[GrantDatabase, Depends(account_grant_database)],
    find_credential: Annotated[Callable[[str], CredentialItem | None], Depends(account_credential_lookup)],
    invalidate: Annotated[Callable[[str], Awaitable[None]], Depends(account_config_invalidator)],
) -> AccountRouteReceipt:
    policy: Final = command.route.account_credential
    try:
        _ = resolve_account_credential(policy, find_credential(policy.credential_name), command.route.target)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="Named account credential is unavailable for this account and origin"
        ) from None
    try:
        result: Final = await create_account_route(database, command, serving_route_patterns(request.scope))
    except Exception:
        raise HTTPException(status_code=503, detail="Account route database request failed") from None
    if isinstance(result, GrantFailure):
        _raise_grant_failure(result)
    if result.created:
        await invalidate("general_settings")
    return result


@router.post("/cube_control/account_routes/{route_id}/grants", response_model=AccountGrantReceipt)
async def cube_control_append_account_grant(
    route_id: Annotated[str, Path(min_length=1, max_length=128)],
    command: AccountGrantCommand,
    _admin: Annotated[UserAPIKeyAuth, Depends(_require_account_admin)],
    database: Annotated[GrantDatabase, Depends(account_grant_database)],
) -> AccountGrantReceipt:
    from litellm.proxy.utils import invalidate_config_param

    try:
        result: Final = await append_account_grant(database, route_id, command)
    except Exception:
        raise HTTPException(status_code=503, detail="Account grant database request failed") from None
    if isinstance(result, GrantFailure):
        _raise_grant_failure(result)
    await invalidate_config_param("general_settings")
    return result


@router.get("/cube_control/capabilities", response_model=CubeControlCapabilities)
async def cube_control_capabilities(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> CubeControlCapabilities:
    if user_api_key_dict.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(status_code=403, detail="Only proxy admins can inspect Cube Control capabilities")

    from litellm.proxy import proxy_server

    settings: Final = cast(Mapping[str, JsonValue], proxy_server.general_settings)

    return CubeControlCapabilities(
        custom_keys_allowed=coerce_bool(settings.get("disable_custom_api_keys", False)) is not True,
        client_side_credentials_allowed=settings.get("allow_client_side_credentials") is True,
    )
