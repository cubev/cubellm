from collections.abc import Callable
from typing import Final, NoReturn
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from litellm.models.credentials import CredentialItem
from litellm.proxy._types import LiteLLM_JWTAuth, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.auth_checks import allowed_routes_check
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_endpoints.cube_control_endpoints import (
    account_credential_lookup,
    account_grant_database,
    router,
)
from litellm.proxy.pass_through_endpoints.account_grants import GrantDatabase


class ForbiddenGrantDatabase:
    def tx(self) -> NoReturn:
        raise AssertionError("Unauthorized management request reached the database")


@pytest.mark.parametrize(
    ("path", "allowed"),
    (
        ("/cube_control/account_routes", True),
        ("/cube_control/account_routes/fixture-route", True),
        ("/cube_control/account_routes/fixture-route/grants", True),
        ("/cube_control/account_routes/fixture-route/resources", True),
        ("/cube_control/account_routes/fixture-route/resources/video-job/vid_1", True),
        ("/cube_control/account_routes/fixture-route/resources/video-job/vid_1/extra", False),
        ("/cube_control/account_routes/fixture-route/unrelated", False),
    ),
)
def test_account_grant_paths_use_native_management_route_permissions(path: str, allowed: bool) -> None:
    policy: Final = LiteLLM_JWTAuth(
        admin_allowed_routes=["management_routes"], team_allowed_routes=["openai_routes", "info_routes"]
    )
    assert allowed_routes_check(LitellmUserRoles.PROXY_ADMIN, path, policy) is allowed
    assert not allowed_routes_check(LitellmUserRoles.TEAM, path, policy)


@pytest.mark.parametrize(
    ("connected", "database_updates", "file_owned", "expected"),
    ((False, True, False, 503), (True, False, False, 409), (True, True, True, 409)),
)
async def test_account_grant_management_requires_database_backed_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
    connected: bool,
    database_updates: bool,
    file_owned: bool,
    expected: int,
) -> None:
    from litellm.proxy import proxy_server

    configuration: Final = proxy_server.ProxyConfig()
    if file_owned:
        configuration.settings.load_yaml({"pass_through_endpoints": []})
    monkeypatch.setattr(proxy_server, "proxy_config", configuration)
    monkeypatch.setattr(proxy_server, "prisma_client", ForbiddenGrantDatabase() if connected else None)
    monkeypatch.setattr(proxy_server, "store_model_in_db", False)
    monkeypatch.setattr(proxy_server, "general_settings", {"store_model_in_db": database_updates})
    app: Final = FastAPI()
    app.include_router(router)

    async def admin() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN)

    app.dependency_overrides[user_api_key_auth] = admin
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.get("/cube_control/account_routes/fixture-route")
    assert response.status_code == expected


@pytest.mark.parametrize("role", (LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY, None))
@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/cube_control/account_routes/fixture-route"),
        ("POST", "/cube_control/account_routes/fixture-route/grants"),
        ("POST", "/cube_control/account_routes"),
        ("POST", "/cube_control/account_routes/fixture-route/resources"),
        ("GET", "/cube_control/account_routes/fixture-route/resources/video-job/vid_1"),
    ),
)
async def test_account_grant_management_rejects_non_admins_before_database_access(
    role: LitellmUserRoles | None, method: str, path: str
) -> None:
    app: Final = FastAPI()
    app.include_router(router)

    async def identity() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_role=role)

    def database() -> GrantDatabase:
        return ForbiddenGrantDatabase()

    app.dependency_overrides[user_api_key_auth] = identity
    app.dependency_overrides[account_grant_database] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.request(
            method,
            path,
            json={
                "expected_fingerprint": "a" * 64,
                "grant": {
                    "application_id": "fixture-app",
                    "key_hash": "b" * 64,
                    "account_id": "fixture-account",
                    "path": "/fixture/quota",
                    "methods": ["GET"],
                },
            },
        )
    assert response.status_code == 403


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
        "application_exact_routes": "exact_path_v1",
        "passthrough_application_identity": "authenticated_object_v1",
        "passthrough_header_isolation": True,
        "passthrough_account_binding": "configured_credential_v1",
        "credential_values_full_masking": True,
        "passthrough_account_provider": "credential_metadata_v1",
        "passthrough_account_grants": "application_account_path_method_v1",
        "passthrough_account_grant_management": "atomic_append_v1",
        "passthrough_account_route_management": "atomic_create_v1",
        "passthrough_account_resources": "application_ownership_v1",
        "virtual_key_log_redaction": True,
        "custom_keys_allowed": disable_custom_keys not in (True, "true"),
        "client_side_credentials_allowed": allow_client_credentials,
    }
    assert "inert-master-key-canary" not in response.text


ROUTE_COMMAND: Final = {
    "route": {
        "id": "fixture-route",
        "path": "/cube_api/fixture/quota",
        "target": "https://vendor.example/v1/quota",
        "methods": ["GET"],
        "account_credential": {
            "account_id": "fixture-account",
            "credential_name": "fixture-credential",
            "headers": [{"name": "X-Api-Key", "field": "api_key"}],
        },
    }
}


@pytest.mark.parametrize(
    "credential",
    (
        None,
        CredentialItem(
            credential_name="fixture-credential",
            credential_values={"api_key": "inert"},
            credential_info={"cube_account_id": "other-account", "cube_allowed_origins": ["https://vendor.example"]},
        ),
        CredentialItem(
            credential_name="fixture-credential",
            credential_values={"api_key": "inert"},
            credential_info={"cube_account_id": "fixture-account", "cube_allowed_origins": ["https://other.example"]},
        ),
        CredentialItem(
            credential_name="fixture-credential",
            credential_values={},
            credential_info={"cube_account_id": "fixture-account", "cube_allowed_origins": ["https://vendor.example"]},
        ),
    ),
)
async def test_route_creation_requires_a_usable_credential_for_the_account_and_origin(
    credential: CredentialItem | None,
) -> None:
    app: Final = FastAPI()
    app.include_router(router)

    async def admin() -> UserAPIKeyAuth:
        return UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN)

    def database() -> GrantDatabase:
        return ForbiddenGrantDatabase()

    def lookup() -> Callable[[str], CredentialItem | None]:
        return lambda name: credential if name == "fixture-credential" else None

    app.dependency_overrides[user_api_key_auth] = admin
    app.dependency_overrides[account_grant_database] = database
    app.dependency_overrides[account_credential_lookup] = lookup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response: Final = await client.post("/cube_control/account_routes", json=ROUTE_COMMAND)
    assert response.status_code == 422
    assert "inert" not in response.text
