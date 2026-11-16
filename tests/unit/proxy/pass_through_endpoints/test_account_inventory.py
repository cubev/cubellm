import json
from datetime import datetime, timezone
from typing import Final

import pytest

from litellm.models.credentials import CredentialItem
from litellm.proxy.pass_through_endpoints.account_inventory import (
    GatewayAccountInventory,
    InventoryCredential,
    InventoryRejection,
    InventoryRoute,
    InventorySource,
    StoredRows,
    StoredUnavailable,
    build_inventory,
)

NOW: Final = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
SECRET: Final = "sk-inventory-canary-value"
POLICY: Final = {
    "account_id": "media-account",
    "credential_name": "media credential",
    "headers": [{"name": "X-Api-Key", "field": "api_key"}],
    "resource_types": [{"name": "video-job", "item_paths": ["/cube_api/media/jobs/{resource_id}"]}],
    "application_grants": [
        {
            "application_id": "media-app",
            "key_hash": "a" * 64,
            "account_id": "media-account",
            "path": "/cube_api/media/jobs/{resource_id}",
            "methods": ["GET"],
        }
    ],
}
NO_DATABASE: Final = StoredUnavailable("database_unavailable")


def credential(name: str, info: dict[str, object]) -> CredentialItem:
    return CredentialItem(credential_name=name, credential_info=info, credential_values={"api_key": SECRET})


def inventory(
    credentials: tuple[object, ...] = (),
    routes: object = None,
    deployments: object = None,
    stored: StoredRows | StoredUnavailable = NO_DATABASE,
) -> GatewayAccountInventory:
    return build_inventory(NOW, credentials, routes, deployments, stored)


def source(result: GatewayAccountInventory, name: str, origin: str) -> InventorySource:
    return next(item for item in result.sources if (item.name, item.origin) == (name, origin))


def test_credentials_report_their_account_binding_without_values() -> None:
    result: Final = inventory(
        (
            credential(
                "bound",
                {
                    "cube_account_id": "media-account",
                    "cube_provider_id": "heygen",
                    "cube_allowed_origins": ["https://api.example"],
                    "custom_llm_provider": "openai",
                },
            ),
            credential("plain", {"custom_llm_provider": "anthropic"}),
            credential("broken", {"cube_account_id": "media-account", "cube_allowed_origins": []}),
        )
    )
    assert result.credentials == (
        InventoryCredential(
            origin="runtime",
            credential_name="bound",
            account_id="media-account",
            provider_id="heygen",
            llm_provider="openai",
            allowed_origins=("https://api.example",),
            binding="bound",
        ),
        InventoryCredential(origin="runtime", credential_name="plain", llm_provider="anthropic", binding="unbound"),
        InventoryCredential(origin="runtime", credential_name="broken", account_id="media-account", binding="invalid"),
    )
    assert source(result, "credentials", "runtime") == InventorySource(
        name="credentials", origin="runtime", status="complete", records=3
    )
    assert SECRET not in result.model_dump_json()


def test_routes_report_account_bindings_resources_and_only_inline_header_names() -> None:
    result: Final = inventory(
        routes=[
            {"id": "media", "path": "/cube_api/media", "methods": ["GET", "POST"], "account_credential": POLICY},
            {"id": "legacy", "path": "/legacy", "headers": {"Authorization": f"Bearer {SECRET}"}},
            {"id": "damaged", "path": "/damaged", "account_credential": {"account_id": "media-account"}},
        ]
    )
    assert result.routes == (
        InventoryRoute(
            origin="runtime",
            route_id="media",
            path="/cube_api/media",
            account_id="media-account",
            credential_name="media credential",
            methods=("GET", "POST"),
            resource_types=("video-job",),
            applications=("media-app",),
            grant_count=1,
            binding="account",
        ),
        InventoryRoute(
            origin="runtime",
            route_id="legacy",
            path="/legacy",
            inline_header_names=("Authorization",),
            binding="unbound",
        ),
        InventoryRoute(
            origin="runtime", route_id="damaged", path="/damaged", account_id="media-account", binding="invalid"
        ),
    )
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize(
    ("parameters", "auth"),
    (
        ({"model": "openai/gpt", "litellm_credential_name": "shared", "api_key": SECRET}, "credential_reference"),
        ({"model": "openai/gpt", "api_key": "os.environ/OPENAI_KEY"}, "environment_reference"),
        ({"model": "openai/gpt", "api_key": SECRET}, "inline_api_key"),
        ({"model": "bedrock/claude", "aws_role_name": "arn:aws:iam::1:role/r"}, "cloud_identity"),
        ({"model": "ollama/llama"}, "unknown"),
    ),
)
def test_deployments_classify_their_authentication_without_values(parameters: dict[str, str], auth: str) -> None:
    result: Final = inventory(deployments=[{"model_name": "alias", "litellm_params": parameters}])
    assert [(item.auth, item.provider, item.provider_hint) for item in result.deployments] == [
        (auth, None, parameters["model"].split("/", 1)[0])
    ]
    assert SECRET not in result.model_dump_json()
    assert "OPENAI_KEY" not in result.model_dump_json()


def test_explicit_provider_metadata_is_separate_from_the_model_name_hint() -> None:
    result: Final = inventory(
        deployments=[{"model_name": "alias", "litellm_params": {"model": "openai/x", "custom_llm_provider": "azure"}}]
    )
    assert (result.deployments[0].provider, result.deployments[0].provider_hint) == ("azure", "openai")


def test_one_malformed_record_is_reported_while_the_others_remain() -> None:
    result: Final = inventory(
        routes=[{"id": "first", "path": "/first"}, "not a route", {"id": "third", "path": "/third"}],
        deployments=[{"model_name": "kept", "litellm_params": {"model": "x"}}, {"model_name": "broken"}],
    )
    assert tuple(route.route_id for route in result.routes) == ("first", "third")
    assert tuple(item.model_name for item in result.deployments) == ("kept",)
    assert result.rejected == (
        InventoryRejection(source="routes", origin="runtime", index=1, reason="not_an_object"),
        InventoryRejection(source="deployments", origin="runtime", index=1, name="broken", reason="unreadable_fields"),
    )
    assert source(result, "routes", "runtime") == InventorySource(
        name="routes", origin="runtime", status="partial", records=2, rejected=1
    )


def test_an_unreadable_source_is_unavailable_instead_of_empty() -> None:
    result: Final = inventory(routes={"id": "not-a-list"}, deployments=[])
    assert source(result, "routes", "runtime") == InventorySource(
        name="routes", origin="runtime", status="unavailable", detail="unreadable"
    )
    assert source(result, "deployments", "runtime").status == "complete"


def test_a_missing_database_marks_every_stored_source_unavailable() -> None:
    result: Final = inventory(stored=StoredUnavailable("database_read_failed"))
    assert tuple(
        (item.name, item.status, item.detail) for item in result.sources if item.origin == "database"
    ) == tuple((name, "unavailable", "database_read_failed") for name in ("credentials", "routes", "deployments"))


def test_stored_rows_report_persisted_routes_credentials_and_deployments() -> None:
    settings: Final = json.dumps({"pass_through_endpoints": [{"id": "stored", "path": "/stored"}], "other": SECRET})
    result: Final = inventory(
        stored=StoredRows(
            settings=({"param_value": settings},),
            credentials=(
                {
                    "credential_name": "db-only",
                    "credential_info": {"cube_account_id": "x", "cube_allowed_origins": ["https://a"]},
                },
            ),
            deployments=(
                {"model_id": "m1", "model_name": "stored-model", "parameter_names": ["model", "api_key"]},
                {
                    "model_id": "m2",
                    "model_name": "shared-model",
                    "parameter_names": ["model", "litellm_credential_name"],
                },
            ),
        )
    )
    assert tuple((route.origin, route.route_id) for route in result.routes) == (("database", "stored"),)
    assert tuple((item.origin, item.credential_name, item.binding) for item in result.credentials) == (
        ("database", "db-only", "bound"),
    )
    assert tuple((item.model_id, item.auth, item.provider) for item in result.deployments) == (
        ("m1", "stored_api_key", None),
        ("m2", "credential_reference", None),
    )
    assert SECRET not in result.model_dump_json()


@pytest.mark.parametrize(
    ("settings", "status", "records"),
    (
        ((), "complete", 0),
        (({"param_value": {}}, {"param_value": {}}), "unavailable", 0),
        (({"param_value": 7},), "unavailable", 0),
    ),
)
def test_stored_settings_are_complete_only_when_readable(settings: object, status: str, records: int) -> None:
    result: Final = inventory(stored=StoredRows(settings=settings, credentials=(), deployments=()))
    stored_routes: Final = source(result, "routes", "database")
    assert (stored_routes.status, stored_routes.records) == (status, records)


class TokenProvider:
    def __init__(self) -> None:
        self.calls: int = 0

    def __call__(self) -> str:
        self.calls += 1
        return SECRET

    def __repr__(self) -> str:
        return f"TokenProvider({SECRET})"


class OpaqueAuth:
    def __str__(self) -> str:
        return SECRET

    def __repr__(self) -> str:
        return SECRET


def test_deployments_with_opaque_auth_objects_stay_listed_without_calling_or_serialising_them() -> None:
    provider: Final = TokenProvider()
    result: Final = inventory(
        deployments=[
            {
                "model_name": "azure-entra",
                "litellm_params": {
                    "model": "azure/gpt",
                    "azure_ad_token_provider": provider,
                    "http_client": OpaqueAuth(),
                },
                "model_info": {"id": "deployment-1", "secret_object": OpaqueAuth()},
            }
        ]
    )
    assert [(item.model_name, item.model_id, item.auth, item.provider_hint) for item in result.deployments] == [
        ("azure-entra", "deployment-1", "cloud_identity", "azure")
    ]
    assert result.rejected == ()
    assert provider.calls == 0
    assert SECRET not in result.model_dump_json()
