from collections.abc import Mapping
from typing import Final, Literal, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, JsonValue

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.config_resolvers.settings_rules import coerce_bool

router: Final = APIRouter()


class CubeControlCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    application_identity: Literal["authenticated_object_v1"] = "authenticated_object_v1"
    passthrough_application_identity: Literal["authenticated_object_v1"] = "authenticated_object_v1"
    passthrough_header_isolation: Literal[True] = True
    passthrough_account_binding: Literal["configured_credential_v1"] = "configured_credential_v1"
    passthrough_account_provider: Literal["credential_metadata_v1"] = "credential_metadata_v1"
    credential_values_full_masking: Literal[True] = True
    virtual_key_log_redaction: Literal[True] = True
    custom_keys_allowed: bool
    client_side_credentials_allowed: bool


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
