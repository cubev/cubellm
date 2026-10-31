from typing import Final
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_endpoints.cube_control_endpoints import router


@pytest.mark.parametrize(
    "role",
    (LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY),
)
async def test_capabilities_rejects_non_admins(role: LitellmUserRoles) -> None:
    app: Final = FastAPI()
    app.include_router(router)

    async def identity() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_role=role)

    app.dependency_overrides[user_api_key_auth] = identity
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.get("/cube_control/capabilities")
    assert response.status_code == 403


@pytest.mark.parametrize("disable_custom_keys", (False, True, "true", "false"))
@pytest.mark.parametrize("allow_client_credentials", (False, True))
async def test_capabilities_reports_running_policy_without_configuration_secrets(
    disable_custom_keys: bool | str,
    allow_client_credentials: bool,
) -> None:
    app: Final = FastAPI()
    app.include_router(router)

    async def identity() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN)

    app.dependency_overrides[user_api_key_auth] = identity
    with (
        patch(
            "litellm.proxy.proxy_server.general_settings",
            {
                "disable_custom_api_keys": disable_custom_keys,
                "allow_client_side_credentials": allow_client_credentials,
                "master_key": "inert-master-key-canary",
            },
        ),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response: Final = await client.get("/cube_control/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "application_identity": "authenticated_object_v1",
        "passthrough_application_identity": "authenticated_object_v1",
        "passthrough_header_isolation": True,
        "passthrough_account_binding": "configured_credential_v1",
        "credential_values_full_masking": True,
        "passthrough_account_provider": "credential_metadata_v1",
        "passthrough_account_grants": "application_account_path_method_v1",
        "virtual_key_log_redaction": True,
        "custom_keys_allowed": disable_custom_keys not in (True, "true"),
        "client_side_credentials_allowed": allow_client_credentials,
    }
    assert "inert-master-key-canary" not in response.text
