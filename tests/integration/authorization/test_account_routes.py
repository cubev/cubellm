import uuid
from hashlib import sha256
from typing import Final

import httpx
from integration._support.upstream import delete_scenario, register_scenario
from integration.cost_calculation.cost_tracking_case import JsonResponse, RoutedResponse
from pydantic import JsonValue

from tests.integration._support.client import (
    JSON_OBJECT,
    Gateway,
    Scenario,
    eventually,
    object_value,
    string_value,
)
from tests.integration._support.database import read_rows, write_rows


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


def _grant(gateway: Gateway, route_id: str, fingerprint: str, grant: dict[str, JsonValue]) -> None:
    gateway.post(
        f"/cube_control/account_routes/{route_id}/grants", {"expected_fingerprint": fingerprint, "grant": grant}
    )


def _application_key(scenario: Scenario, application: str, entries: list[JsonValue]) -> tuple[str, str]:
    team: Final = scenario.team()
    user: Final = scenario.user(teams=[team], user_role="internal_user")
    metadata: Final[dict[str, JsonValue]] = {"cube_application_id": application, "allowed_passthrough_routes": entries}
    key: Final = scenario.key(user_id=user, team_id=team, allowed_routes=entries, metadata=metadata)
    return key, sha256(key.encode()).hexdigest()


def _delete_ownership(account: str) -> None:
    write_rows('DELETE FROM "LiteLLM_AccountResourceOwnership" WHERE account_id = %s', (account,))
    remaining: Final = read_rows(
        'SELECT resource_id FROM "LiteLLM_AccountResourceOwnership" WHERE account_id = %s', (account,)
    )
    assert remaining == [], remaining


def _ownership_rows(account: str) -> list[dict[str, JsonValue]]:
    return read_rows(
        "SELECT resource_type, resource_id, route_id, owner_application_id, provenance, creator_key_hash"
        ' FROM "LiteLLM_AccountResourceOwnership" WHERE account_id = %s ORDER BY resource_type',
        (account,),
    )


def test_created_and_adopted_resources_are_reachable_only_by_the_owning_application(gateway: Gateway) -> None:
    marker: Final = uuid.uuid4().hex
    account: Final = f"integration-account-{marker}"
    base: Final = f"/cube_api/integration-{marker}/media"
    route_id: Final = f"integration-media-{marker}"
    video_id: Final = f"vid-{marker}"
    audio_id: Final = f"aud-{marker}"
    adopted_id: Final = f"file-{marker}"
    created_body: Final[dict[str, JsonValue]] = {"video": {"id": video_id}, "audio": [{"id": audio_id}]}
    item_bodies: Final[dict[str, dict[str, JsonValue]]] = {
        f"{base}/video/{video_id}": {"kind": "video", "id": video_id},
        f"{base}/audio/{audio_id}": {"kind": "audio", "id": audio_id},
        f"{base}/files/{adopted_id}": {"kind": "file", "id": adopted_id},
    }
    templates: Final = {kind: f"{base}/{kind}/{{resource_id}}" for kind in ("video", "audio", "files")}
    handle: Final = register_scenario(
        marker,
        RoutedResponse(
            content_type="application/x-routed",
            routes={
                "POST /v1/media/jobs": JsonResponse(content_type="application/json", body=created_body),
                **{
                    "GET /v1/media" + path.removeprefix(base): JsonResponse(content_type="application/json", body=body)
                    for path, body in item_bodies.items()
                },
            },
        ),
    )
    with gateway.scenario() as scenario:
        scenario.cleanups.callback(delete_scenario, handle)
        scenario.cleanups.callback(_delete_ownership, account)
        credential: Final = _credential(gateway, scenario, account, f"synthetic-provider-{marker}")
        creation_rule: Final[dict[str, JsonValue]] = {"path": f"{base}/jobs", "methods": ["POST"]}
        created: Final = gateway.post(
            "/cube_control/account_routes",
            {
                "route": {
                    "id": route_id,
                    "path": base,
                    "target": f"{handle.api_base()}/v1/media",
                    "methods": ["GET", "POST"],
                    "include_subpath": True,
                    "account_credential": {
                        "account_id": account,
                        "credential_name": credential,
                        "headers": [{"name": "Authorization", "field": "api_key", "prefix": "Bearer "}],
                        "resource_types": [
                            {
                                "name": "video-job",
                                "item_paths": [templates["video"]],
                                "creation": {**creation_rule, "id_selector": ["video", "id"]},
                            },
                            {
                                "name": "audio-job",
                                "item_paths": [templates["audio"]],
                                "creation": {**creation_rule, "id_selector": ["audio", 0, "id"]},
                            },
                            {"name": "file", "item_paths": [templates["files"]]},
                        ],
                    },
                }
            },
        )
        scenario.cleanups.callback(_delete_route, gateway, route_id)
        fingerprint: Final = string_value(object_value(created["route"])["fingerprint"])
        entries: Final[list[JsonValue]] = [
            f"exact:{base}/jobs",
            *(f"resource:{template}" for template in templates.values()),
        ]
        owner_app: Final = f"integration-owner-{marker}"
        other_app: Final = f"integration-other-{marker}"
        owner_key, owner_hash = _application_key(scenario, owner_app, entries)
        other_key, other_hash = _application_key(scenario, other_app, entries)
        for application, key_hash in ((owner_app, owner_hash), (other_app, other_hash)):
            for path, method in ((f"{base}/jobs", "POST"), *((template, "GET") for template in templates.values())):
                _grant(
                    gateway,
                    route_id,
                    fingerprint,
                    {
                        "application_id": application,
                        "key_hash": key_hash,
                        "account_id": account,
                        "path": path,
                        "methods": [method],
                    },
                )
        _observed(gateway)

        creation: Final = eventually(
            lambda: gateway.request("POST", f"{base}/jobs", {"marker": marker}, key=owner_key),
            lambda response: response.status_code != 404,
            seconds=60,
        )
        assert (creation.status_code, creation.json()) == (200, created_body), creation.text
        created_rows: Final = [
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "route_id": route_id,
                "owner_application_id": owner_app,
                "provenance": "created",
                "creator_key_hash": owner_hash,
            }
            for resource_type, resource_id in (("audio-job", audio_id), ("video-job", video_id))
        ]
        assert _ownership_rows(account) == created_rows

        adoption: Final = gateway.post(
            f"/cube_control/account_routes/{route_id}/resources",
            {
                "expected_fingerprint": fingerprint,
                "resource_type": "file",
                "resource_id": adopted_id,
                "application_id": owner_app,
            },
        )
        assert (adoption["inserted"], object_value(adoption["ownership"])["provenance"]) == (True, "adopted")
        assert _ownership_rows(account) == [
            *created_rows,
            {
                "resource_type": "file",
                "resource_id": adopted_id,
                "route_id": route_id,
                "owner_application_id": owner_app,
                "provenance": "adopted",
                "creator_key_hash": None,
            },
        ]

        owner_reads: Final = {path: gateway.request("GET", path, key=owner_key) for path in item_bodies}
        assert {path: (response.status_code, response.json()) for path, response in owner_reads.items()} == {
            path: (200, body) for path, body in item_bodies.items()
        }
        assert [gateway.request("GET", path, key=other_key).status_code for path in item_bodies] == [403, 403, 403]
        assert gateway.request("GET", f"{base}/video/unowned-{marker}", key=owner_key).status_code == 403
        assert gateway.request("GET", f"{base}/video/{audio_id}", key=owner_key).status_code == 403
        assert gateway.request("GET", f"{base}/video/{video_id}").status_code in (401, 403)
        observed: Final = [object_value(value) for value in _observed(gateway)]
        assert [(value["path"], value["body"]) for value in observed] == [
            (f"/{marker}/v1/media/jobs", {"marker": marker})
        ]
    assert _ownership_rows(account) == []
