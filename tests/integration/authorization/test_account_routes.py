import uuid
from hashlib import sha256
from typing import Final

import httpx
from integration._support.upstream import delete_scenario, register_scenario
from integration.cost_calculation.cost_tracking_case import JsonResponse
from pydantic import JsonValue

from tests.integration._support.client import (
    JSON_OBJECT,
    Gateway,
    Scenario,
    eventually,
    object_value,
    string_value,
)
from tests.integration._support.database import read_rows


def _credential(gateway: Gateway, scenario: Scenario, account: str, provider_value: str) -> str:
    name: Final = f"integration-account-{uuid.uuid4().hex}"
    gateway.post(
        "/credentials",
        {
            "credential_name": name,
            "credential_values": {"api_key": provider_value},
            "credential_info": {"cube_account_id": account, "cube_allowed_origins": [gateway.upstream_url]},
        },
    )
    scenario.cleanups.callback(_delete_credential, gateway, name)
    return name


def _delete_credential(gateway: Gateway, name: str) -> None:
    response: Final = gateway.request("DELETE", f"/credentials/{name}")
    assert response.status_code == 200, response.text


def _delete_route(gateway: Gateway, route_id: str) -> None:
    response: Final = gateway.request("DELETE", "/config/pass_through_endpoint", params={"endpoint_id": route_id})
    assert response.status_code == 200, response.text
    assert gateway.request("GET", f"/cube_control/account_routes/{route_id}").status_code == 404


def _observed(gateway: Gateway) -> list[JsonValue]:
    with httpx.Client(base_url=gateway.upstream_url, timeout=15, trust_env=False) as upstream:
        response: Final = upstream.get("/__observations")
        response.raise_for_status()
        requests: Final = JSON_OBJECT.validate_json(response.content)["requests"]
        assert isinstance(requests, list)
        return requests


def test_created_account_route_serves_only_the_granted_application_key(gateway: Gateway) -> None:
    marker: Final = uuid.uuid4().hex
    account: Final = f"integration-account-{marker}"
    provider_value: Final = f"synthetic-provider-{marker}"
    path: Final = f"/cube_api/integration-{marker}/quota"
    route_id: Final = f"integration-route-{marker}"
    handle: Final = register_scenario(marker, JsonResponse(content_type="application/json", body={"quota": 42}))
    with gateway.scenario() as scenario:
        scenario.cleanups.callback(delete_scenario, handle)
        credential: Final = _credential(gateway, scenario, account, provider_value)
        command: Final[dict[str, JsonValue]] = {
            "route": {
                "id": route_id,
                "path": path,
                "target": f"{handle.api_base()}/v1/quota",
                "methods": ["POST"],
                "account_credential": {
                    "account_id": account,
                    "credential_name": credential,
                    "headers": [{"name": "Authorization", "field": "api_key", "prefix": "Bearer "}],
                },
            }
        }
        created: Final = gateway.post("/cube_control/account_routes", command)
        scenario.cleanups.callback(_delete_route, gateway, route_id)
        repeated: Final = gateway.post("/cube_control/account_routes", command)
        route: Final = object_value(created["route"])
        assert (created["created"], repeated["created"]) == (True, False)
        assert object_value(repeated["route"])["fingerprint"] == route["fingerprint"]
        assert provider_value not in str(created)

        team: Final = scenario.team()
        user: Final = scenario.user(teams=[team], user_role="internal_user")
        key: Final = scenario.key(
            user_id=user,
            team_id=team,
            allowed_routes=[f"exact:{path}"],
            metadata={"cube_application_id": f"integration-app-{marker}", "allowed_passthrough_routes": [path]},
        )
        other_key: Final = scenario.key(user_id=user, team_id=team, allowed_routes=[f"exact:{path}"])
        key_hash: Final = sha256(key.encode()).hexdigest()
        stored_key: Final = read_rows(
            'SELECT user_id, team_id, allowed_routes FROM "LiteLLM_VerificationToken" WHERE token = %s', (key_hash,)
        )
        assert stored_key == [{"user_id": user, "team_id": team, "allowed_routes": [f"exact:{path}"]}]
        assert read_rows('SELECT token FROM "LiteLLM_VerificationToken" WHERE token = %s', (key,)) == []

        gateway.post(
            f"/cube_control/account_routes/{route_id}/grants",
            {
                "expected_fingerprint": string_value(route["fingerprint"]),
                "grant": {
                    "application_id": f"integration-app-{marker}",
                    "key_hash": key_hash,
                    "account_id": account,
                    "path": path,
                    "methods": ["POST"],
                },
            },
        )
        _observed(gateway)
        served: Final = eventually(
            lambda: gateway.request("POST", path, {"marker": marker}, key=key),
            lambda response: response.status_code != 404,
            seconds=60,
        )
        assert (served.status_code, served.json()) == (200, {"quota": 42}), served.text
        observed: Final = [object_value(value) for value in _observed(gateway)]
        assert [(value["path"], value["authorization"], value["body"]) for value in observed] == [
            (f"/{marker}/v1/quota", f"Bearer {provider_value}", {"marker": marker})
        ]

        assert gateway.request("GET", path, key=key).status_code == 405
        assert gateway.request("POST", path, {"marker": marker}, key=other_key).status_code == 403
        assert gateway.request("POST", path, {"marker": marker}).status_code == 403
        assert gateway.request("POST", path + "/child", {"marker": marker}, key=key).status_code == 404
        assert _observed(gateway) == []

        gateway.post("/key/block", {"key": key})
        blocked: Final = eventually(
            lambda: gateway.request("POST", path, {"marker": marker}, key=key),
            lambda response: response.status_code in (401, 403),
            seconds=30,
        )
        assert blocked.status_code in (401, 403), blocked.text
        assert _observed(gateway) == []
