import json
import os
import socket
import subprocess
import sys
import uuid
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from typing import Final

import httpx
import pytest
from integration._support.upstream import delete_scenario, register_scenario
from integration.cost_calculation.cost_tracking_case import JsonResponse, RoutedResponse
from pydantic import JsonValue, TypeAdapter

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
        adopted_row: Final[dict[str, JsonValue]] = {
            "resource_type": "file",
            "resource_id": adopted_id,
            "route_id": route_id,
            "owner_application_id": owner_app,
            "provenance": "adopted",
            "creator_key_hash": None,
        }
        assert _ownership_rows(account) == [created_rows[0], adopted_row, created_rows[1]]

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


CUBE_CONTROL_SOURCE: Final = Path(__file__).resolve().parents[3] / "cube_control" / "src"


def _admin_origin(gateway: Gateway) -> str | None:
    url: Final = gateway.client.base_url
    loopback: Final = url.scheme == "http" and url.host in ("127.0.0.1", "::1") and url.port is not None
    host: Final = f"[{url.host}]" if ":" in url.host else url.host
    return f"http://{host}:{url.port}" if loopback else None


def _control(arguments: list[str], payload: str | None, secrets: tuple[str, ...]) -> dict[str, JsonValue]:
    result: Final = subprocess.run(
        [sys.executable, "-m", "cube_control", *arguments],
        input=payload,
        text=True,
        capture_output=True,
        env=os.environ | {"PYTHONPATH": os.pathsep.join((str(CUBE_CONTROL_SOURCE), *sys.path))},
        timeout=120,
        check=False,
    )
    assert not any(secret in result.stdout + result.stderr for secret in secrets), "controller output leaked a secret"
    return JSON_OBJECT.validate_json(result.stdout or "{}")


def _delete_application_access(gateway: Gateway, application: str) -> None:
    keys: Final = read_rows(
        "SELECT token FROM \"LiteLLM_VerificationToken\" WHERE metadata->>'cube_application_id' = %s", (application,)
    )
    if keys:
        gateway.post("/key/delete", {"keys": [string_value(row["token"]) for row in keys]})
    assert (
        read_rows(
            "SELECT token FROM \"LiteLLM_VerificationToken\" WHERE metadata->>'cube_application_id' = %s",
            (application,),
        )
        == []
    )
    user: Final = "cubellm-app-" + sha256(application.encode()).hexdigest()[:32]
    if read_rows('SELECT user_id FROM "LiteLLM_UserTable" WHERE user_id = %s', (user,)):
        response: Final = gateway.request("POST", "/user/delete", {"user_ids": [user]})
        assert response.status_code == 200, response.text
    assert read_rows('SELECT user_id FROM "LiteLLM_UserTable" WHERE user_id = %s', (user,)) == []


def test_controller_provisions_resource_access_adopts_resources_and_revokes_keys(
    gateway: Gateway, tmp_path: Path
) -> None:
    origin: Final = _admin_origin(gateway)
    if origin is None:
        pytest.skip("The controller only administers a gateway reached over a loopback HTTP origin with a port")
    marker: Final = uuid.uuid4().hex
    account: Final = f"integration-account-{marker}"
    base: Final = f"/cube_api/integration-{marker}/media"
    route_id: Final = f"integration-controller-{marker}"
    provider_value: Final = f"synthetic-provider-{marker}"
    video_id: Final = f"vid-{marker}"
    file_id: Final = f"file-{marker}"
    video_template: Final = base + "/videos/{resource_id}"
    file_template: Final = base + "/files/{resource_id}"
    items: Final[dict[str, dict[str, JsonValue]]] = {
        f"{base}/videos/{video_id}": {"kind": "video", "id": video_id},
        f"{base}/files/{file_id}": {"kind": "file", "id": file_id},
    }
    with gateway.scenario() as scenario:
        handle: Final = register_scenario(
            marker,
            RoutedResponse(
                content_type="application/x-routed",
                routes={
                    "POST /v1/media/jobs": JsonResponse(
                        content_type="application/json", body={"video": {"id": video_id}}
                    ),
                    **{
                        "GET /v1/media" + path.removeprefix(base): JsonResponse(
                            content_type="application/json", body=body
                        )
                        for path, body in items.items()
                    },
                },
            ),
        )
        scenario.cleanups.callback(delete_scenario, handle)
        scenario.cleanups.callback(_delete_ownership, account)
        admin: Final = tmp_path / "admin.env"
        admin.write_text(f"LITELLM_MASTER_KEY={gateway.key}\n")
        admin.chmod(0o600)
        credential: Final = _credential(gateway, scenario, account, provider_value)
        team: Final = scenario.team()
        route_spec: Final = json.dumps(
            {
                "gateway_origin": origin,
                "route_id": route_id,
                "account_id": account,
                "credential_name": credential,
                "headers": [{"name": "Authorization", "field": "api_key", "prefix": "Bearer "}],
                "target": f"{handle.api_base()}/v1/media",
                "path": base,
                "methods": ["GET", "POST"],
                "include_subpath": True,
                "resource_types": [
                    {
                        "name": "video-job",
                        "item_paths": [video_template],
                        "creation": {"path": base + "/jobs", "methods": ["POST"], "id_selector": ["video", "id"]},
                    },
                    {"name": "file", "item_paths": [file_template]},
                ],
            }
        )
        secrets: Final = (gateway.key, provider_value)
        created: Final = _control(["provision-account-route", "--config", str(admin)], route_spec, secrets)
        scenario.cleanups.callback(_delete_route, gateway, route_id)
        repeated: Final = _control(["provision-account-route", "--config", str(admin)], route_spec, secrets)
        assert created["status"] == "created", created
        assert (repeated["status"], repeated["fingerprint"]) == ("existing", created["fingerprint"]), repeated
        fingerprint: Final = string_value(created["fingerprint"])

        scopes: Final[tuple[tuple[str, str], ...]] = (
            (base + "/jobs", "POST"),
            (video_template, "GET"),
            (file_template, "GET"),
        )
        delivered_keys: Final[dict[str, str]] = {}
        states: Final[dict[str, Path]] = {}
        for name in ("owner", "other"):
            application: str = f"integration-{name}-{marker}"
            state: Path = tmp_path / f"state-{name}"
            consumer: Path = tmp_path / f"consumer-{name}"
            spec: dict[str, JsonValue] = {
                "application_id": application,
                "team_id": team,
                "gateway_origin": origin,
                "account_routes": [
                    {
                        "route_id": route_id,
                        "account_id": account,
                        "expected_fingerprint": fingerprint,
                        "path": path,
                        "methods": [method],
                    }
                    for path, method in scopes
                ],
            }
            prepared: dict[str, JsonValue] = _control(
                ["prepare-application", "--state-directory", str(state)], json.dumps(spec), secrets
            )
            assert prepared["status"] == "prepared", prepared
            scenario.cleanups.callback(_delete_application_access, gateway, application)
            provisioned: dict[str, JsonValue] = _control(
                ["provision-application", "--state-directory", str(state), "--config", str(admin)], None, secrets
            )
            assert provisioned["status"] == "provisioned", provisioned
            delivered: dict[str, JsonValue] = _control(
                [
                    "deliver-application",
                    "--state-directory",
                    str(state),
                    "--destination-directory",
                    str(consumer),
                    "--config",
                    str(admin),
                ],
                None,
                secrets,
            )
            assert delivered["status"] == "delivered", delivered
            key: str = (consumer / "api-key").read_text().strip()
            delivered_keys[name] = key
            states[name] = state
        owner_app: Final = f"integration-owner-{marker}"
        other_app: Final = f"integration-other-{marker}"
        owner_hash: Final = sha256(delivered_keys["owner"].encode()).hexdigest()
        other_hash: Final = sha256(delivered_keys["other"].encode()).hexdigest()
        all_secrets: Final = (*secrets, delivered_keys["owner"], delivered_keys["other"])

        key_routes: Final[list[JsonValue]] = [
            f"exact:{base}/jobs",
            f"resource:{video_template}",
            f"resource:{file_template}",
        ]
        passthrough: Final[list[JsonValue]] = [
            f"{base}/jobs",
            f"resource:{video_template}",
            f"resource:{file_template}",
        ]
        stored_keys: Final = read_rows(
            "SELECT token, allowed_routes, metadata->'allowed_passthrough_routes' AS passthrough,"
            " metadata->>'cube_application_id' AS application"
            ' FROM "LiteLLM_VerificationToken" WHERE token IN (%s, %s) ORDER BY application',
            (owner_hash, other_hash),
        )
        assert stored_keys == [
            {"token": other_hash, "allowed_routes": key_routes, "passthrough": passthrough, "application": other_app},
            {"token": owner_hash, "allowed_routes": key_routes, "passthrough": passthrough, "application": owner_app},
        ], stored_keys
        raw_keys: Final = read_rows(
            'SELECT token FROM "LiteLLM_VerificationToken" WHERE token IN (%s, %s)',
            (delivered_keys["owner"], delivered_keys["other"]),
        )
        assert raw_keys == []
        route: Final = gateway.get(f"/cube_control/account_routes/{route_id}")
        stored_grants: Final = route["grants"]
        assert isinstance(stored_grants, list), route
        grants: Final = sorted(
            (
                string_value(grant["application_id"]),
                string_value(grant["key_hash"]),
                string_value(grant["account_id"]),
                string_value(grant["path"]),
                json.dumps(grant["methods"]),
            )
            for grant in map(object_value, stored_grants)
        )
        assert grants == sorted(
            (application, key_hash, account, path, json.dumps([method]))
            for application, key_hash in ((owner_app, owner_hash), (other_app, other_hash))
            for path, method in scopes
        ), grants
        assert provider_value not in json.dumps(route)

        _observed(gateway)
        creation: Final = eventually(
            lambda: gateway.request("POST", base + "/jobs", {"marker": marker}, key=delivered_keys["owner"]),
            lambda response: response.status_code != 404,
            seconds=60,
        )
        assert (creation.status_code, creation.json()) == (200, {"video": {"id": video_id}}), creation.text

        def adoption(application: str) -> str:
            return json.dumps(
                {
                    "gateway_origin": origin,
                    "route_id": route_id,
                    "expected_fingerprint": fingerprint,
                    "resource_type": "file",
                    "resource_id": file_id,
                    "application_id": application,
                }
            )

        adopted: Final = _control(["adopt-account-resource", "--config", str(admin)], adoption(owner_app), all_secrets)
        readopted: Final = _control(
            ["adopt-account-resource", "--config", str(admin)], adoption(owner_app), all_secrets
        )
        foreign: Final = _control(["adopt-account-resource", "--config", str(admin)], adoption(other_app), all_secrets)
        assert (adopted["status"], readopted["status"], foreign["status"]) == ("adopted", "existing", "conflict")
        assert _ownership_rows(account) == [
            {
                "resource_type": "file",
                "resource_id": file_id,
                "route_id": route_id,
                "owner_application_id": owner_app,
                "provenance": "adopted",
                "creator_key_hash": None,
            },
            {
                "resource_type": "video-job",
                "resource_id": video_id,
                "route_id": route_id,
                "owner_application_id": owner_app,
                "provenance": "created",
                "creator_key_hash": owner_hash,
            },
        ]

        owner_reads: Final = {path: gateway.request("GET", path, key=delivered_keys["owner"]) for path in items}
        assert {path: (response.status_code, response.json()) for path, response in owner_reads.items()} == {
            path: (200, body) for path, body in items.items()
        }
        assert [gateway.request("GET", path, key=delivered_keys["other"]).status_code for path in items] == [403, 403]
        assert gateway.request("GET", f"{base}/videos/{file_id}", key=delivered_keys["owner"]).status_code == 403
        assert gateway.request("GET", f"{base}/videos/unowned-{marker}", key=delivered_keys["owner"]).status_code == 403
        assert gateway.request("GET", f"{base}/videos/{video_id}", key=gateway.key).status_code == 403

        revoked: Final = _control(
            ["revoke-application", "--state-directory", str(states["owner"]), "--config", str(admin)], None, all_secrets
        )
        assert revoked["status"] == "blocked", revoked
        after: Final = eventually(
            lambda: gateway.request("GET", f"{base}/videos/{video_id}", key=delivered_keys["owner"]),
            lambda response: response.status_code in (401, 403),
            seconds=90,
        )
        assert after.status_code in (401, 403), after.text
        denied_creation: Final = gateway.request(
            "POST", base + "/jobs", {"marker": marker}, key=delivered_keys["owner"]
        )
        assert denied_creation.status_code in (401, 403), denied_creation.text
        assert gateway.request("GET", f"{base}/videos/{video_id}", key=delivered_keys["other"]).status_code == 403
        observed_posts: Final = [object_value(value) for value in _observed(gateway)]
        assert [(value["path"], value["body"]) for value in observed_posts] == [
            (f"/{marker}/v1/media/jobs", {"marker": marker})
        ]


def _delete_route_if_present(gateway: Gateway, route_id: str) -> None:
    if gateway.request("GET", f"/cube_control/account_routes/{route_id}").status_code != 404:
        _delete_route(gateway, route_id)


def _stored_route_ids() -> list[JsonValue]:
    rows: Final = read_rows(
        "SELECT jsonb_path_query_array(param_value, '$.pass_through_endpoints[*].id') AS ids"
        ' FROM "LiteLLM_Config" WHERE param_name = %s',
        ("general_settings",),
    )
    ids: Final = rows[0]["ids"] if rows else []
    assert isinstance(ids, list), rows
    return ids


def test_deleting_a_route_never_drops_a_route_created_right_before(gateway: Gateway) -> None:
    marker: Final = uuid.uuid4().hex
    account: Final = f"integration-account-{marker}"
    handle: Final = register_scenario(marker, JsonResponse(content_type="application/json", body={"quota": 7}))
    with gateway.scenario() as scenario:
        scenario.cleanups.callback(delete_scenario, handle)
        credential: Final = _credential(gateway, scenario, account, f"synthetic-provider-{marker}")

        def command(name: str) -> dict[str, JsonValue]:
            return {
                "route": {
                    "id": f"integration-{name}-{marker}",
                    "path": f"/cube_api/integration-{marker}/{name}",
                    "target": f"{handle.api_base()}/v1/{name}",
                    "methods": ["GET"],
                    "account_credential": {
                        "account_id": account,
                        "credential_name": credential,
                        "headers": [{"name": "Authorization", "field": "api_key", "prefix": "Bearer "}],
                    },
                }
            }

        older_id: Final = f"integration-older-{marker}"
        gateway.post("/cube_control/account_routes", command("older"))
        scenario.cleanups.callback(_delete_route_if_present, gateway, older_id)
        view: Final = eventually(
            lambda: gateway.request("GET", "/config/field/info", params={"field_name": "pass_through_endpoints"}),
            lambda response: response.status_code == 200 and older_id in response.text,
            seconds=60,
        )
        assert view.status_code == 200 and older_id in view.text, view.text
        kept: Final = gateway.post("/cube_control/account_routes", command("newer"))
        scenario.cleanups.callback(_delete_route_if_present, gateway, f"integration-newer-{marker}")
        assert kept["created"] is True
        deleted: Final = gateway.request("DELETE", "/config/pass_through_endpoint", params={"endpoint_id": older_id})
        assert deleted.status_code == 200, deleted.text
        ids: Final = _stored_route_ids()
        assert f"integration-newer-{marker}" in ids, ids
        assert older_id not in ids, ids
        assert gateway.get(f"/cube_control/account_routes/integration-newer-{marker}")["route_id"] == (
            f"integration-newer-{marker}"
        )


def _inventory_records(body: dict[str, JsonValue], collection: str, origin: str) -> list[dict[str, JsonValue]]:
    records: Final = body[collection]
    assert isinstance(records, list), body
    return [record for record in map(object_value, records) if record["origin"] == origin]


def test_account_inventory_reports_stored_bindings_to_admins_only_without_secrets(gateway: Gateway) -> None:
    marker: Final = uuid.uuid4().hex
    account: Final = f"integration-inventory-{marker}"
    canary: Final = f"sk-inventory-canary-{marker}"
    with gateway.scenario() as scenario:
        handle: Final = register_scenario(marker, JsonResponse(content_type="application/json", body={"ok": True}))
        scenario.cleanups.callback(delete_scenario, handle)
        bound: Final = _credential(gateway, scenario, account, canary)
        plain: Final = f"integration-plain-{marker}"
        gateway.post(
            "/credentials",
            {
                "credential_name": plain,
                "credential_values": {"api_key": canary},
                "credential_info": {"custom_llm_provider": "openai"},
            },
        )
        scenario.cleanups.callback(_delete_credential, gateway, plain)
        route_id: Final = f"integration-inventory-route-{marker}"
        gateway.post(
            "/cube_control/account_routes",
            {
                "route": {
                    "id": route_id,
                    "path": f"/cube_api/integration-{marker}/inventory",
                    "target": f"{handle.api_base()}/v1/inventory",
                    "methods": ["GET"],
                    "account_credential": {
                        "account_id": account,
                        "credential_name": bound,
                        "headers": [{"name": "Authorization", "field": "api_key", "prefix": "Bearer "}],
                    },
                }
            },
        )
        scenario.cleanups.callback(_delete_route_if_present, gateway, route_id)
        legacy_id: Final = f"integration-legacy-{marker}"
        legacy: Final = gateway.request(
            "POST",
            "/config/pass_through_endpoint",
            {
                "id": legacy_id,
                "path": f"/integration-legacy-{marker}",
                "target": f"{handle.api_base()}/v1/legacy?api_key={canary}",
                "headers": {"X-Inventory-Canary": canary},
                "auth": True,
            },
        )
        assert legacy.status_code == 200, legacy.text
        scenario.cleanups.callback(_delete_route_if_present, gateway, legacy_id)
        model: Final = scenario.model(api_key=canary)
        team: Final = scenario.team()
        user: Final = scenario.user(teams=[team], user_role="internal_user")
        internal_key: Final = scenario.key(user_id=user, team_id=team)
        application_key: Final = scenario.key(
            user_id=user, team_id=team, metadata={"cube_application_id": f"integration-app-{marker}"}
        )

        response: Final = gateway.request("GET", "/cube_control/account_inventory")
        assert response.status_code == 200, response.text
        assert canary not in response.text
        body: Final = object_value(JSON_OBJECT.validate_json(response.content))
        sources: Final = {
            (string_value(item["name"]), string_value(item["origin"])): string_value(item["status"])
            for item in map(object_value, body["sources"] if isinstance(body["sources"], list) else [])
        }
        assert {key: status for key, status in sources.items() if key[1] == "database"} == {
            ("credentials", "database"): "complete",
            ("routes", "database"): "complete",
            ("deployments", "database"): "complete",
        }, sources
        runtime_sources: Final = {key[0]: status for key, status in sources.items() if key[1] == "runtime"}
        assert set(runtime_sources) == {"credentials", "routes", "deployments"}, sources
        assert set(runtime_sources.values()) <= {"complete", "partial"}, sources
        stored_credentials: Final = {
            string_value(item["credential_name"]): (item["account_id"], item["binding"])
            for item in _inventory_records(body, "credentials", "database")
        }
        assert stored_credentials[bound] == (account, "bound")
        assert stored_credentials[plain] == (None, "unbound")
        stored_routes: Final = {
            item["route_id"]: item
            for item in _inventory_records(body, "routes", "database")
            if isinstance(item["route_id"], str)
        }
        assert (stored_routes[route_id]["account_id"], stored_routes[route_id]["binding"]) == (account, "account")
        assert (stored_routes[legacy_id]["binding"], stored_routes[legacy_id]["inline_header_names"]) == (
            "unbound",
            ["X-Inventory-Canary"],
        )
        stored_models: Final = [
            (item["auth"], item["provider"])
            for item in _inventory_records(body, "deployments", "database")
            if item["model_name"] == model
        ]
        assert stored_models == [("stored_api_key", None)], stored_models
        keys: Final = (internal_key, application_key)
        allowed: Final = [gateway.request("GET", "/v1/models", key=key).status_code for key in keys]
        assert allowed == [200, 200]
        denials: Final = [
            (
                gateway.request("GET", "/cube_control/account_inventory", key=key),
                gateway.request("GET", f"/cube_control/account_routes/{route_id}", key=key),
            )
            for key in keys
        ]
        assert [
            (
                (inventory.status_code, object_value(object_value(inventory.json())["error"])["type"]),
                (sibling.status_code, object_value(object_value(sibling.json())["error"])["type"]),
                key in inventory.text,
            )
            for (inventory, sibling), key in zip(denials, keys, strict=True)
        ] == [((401, "auth_error"), (401, "auth_error"), False)] * 2, [item.text for pair in denials for item in pair]


def _control_process(arguments: list[str], database_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cube_control", *arguments],
        text=True,
        capture_output=True,
        env=os.environ
        | {
            "PYTHONPATH": os.pathsep.join((str(CUBE_CONTROL_SOURCE), *sys.path)),
            "CUBE_CONTROL_DATABASE_URL": database_url,
        },
        timeout=120,
        check=False,
    )


def _inventory_rows(origin: str) -> list[dict[str, JsonValue]]:
    return read_rows(
        "SELECT attempt::text AS attempt, coalesce(last_good::text, '') AS last_good "
        "FROM cube_control.gateway_inventories WHERE gateway_origin = %s",
        (origin,),
    )


def _restore_inventory_row(origin: str, prior: list[dict[str, JsonValue]]) -> None:
    if prior:
        write_rows(
            "INSERT INTO cube_control.gateway_inventories (gateway_origin, attempt, last_good) "
            "VALUES (%s, %s::jsonb, NULLIF(%s, '')::jsonb) ON CONFLICT (gateway_origin) DO UPDATE "
            "SET attempt = EXCLUDED.attempt, last_good = EXCLUDED.last_good",
            (origin, string_value(prior[0]["attempt"]), string_value(prior[0]["last_good"])),
        )
    else:
        write_rows("DELETE FROM cube_control.gateway_inventories WHERE gateway_origin = %s", (origin,))
    assert _inventory_rows(origin) == prior


SOCKET_ADDRESS: Final = TypeAdapter(tuple[str, int])


def _stop(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def _serve_control(cleanup: ExitStack, database_url: str, log: Path) -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        _, port = SOCKET_ADDRESS.validate_python(probe.getsockname())
    stderr: Final = cleanup.enter_context(log.open("w"))
    process: Final = subprocess.Popen(
        [sys.executable, "-m", "cube_control", "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=os.environ
        | {
            "PYTHONPATH": os.pathsep.join((str(CUBE_CONTROL_SOURCE), *sys.path)),
            "CUBE_CONTROL_DATABASE_URL": database_url,
        },
        stdout=subprocess.DEVNULL,
        stderr=stderr,
    )
    cleanup.callback(_stop, process)
    base: Final = f"http://127.0.0.1:{port}"

    def health() -> int:
        assert process.poll() is None, "controller exited before it became ready"
        try:
            return httpx.get(f"{base}/healthz", timeout=2, trust_env=False).status_code
        except httpx.TransportError:
            return 0

    eventually(health, lambda status: status == 200, seconds=60)
    return base


def test_controller_collects_the_native_inventory_and_reports_the_failed_retry(
    gateway: Gateway, tmp_path: Path
) -> None:
    origin: Final = _admin_origin(gateway)
    if origin is None:
        pytest.skip("The controller only administers a gateway reached over a loopback HTTP origin with a port")
    database_url: Final = os.environ["DATABASE_URL"]
    if read_rows("SELECT current_database() AS name", ()) != [{"name": "cubecontrol_test"}]:
        pytest.skip("The controller acceptance only writes to the dedicated cubecontrol_test database")
    marker: Final = uuid.uuid4().hex
    account: Final = f"integration-collected-{marker}"
    canary: Final = f"sk-collected-canary-{marker}"
    wrong_key: Final = f"sk-wrong-{marker}"
    initialized: Final = _control_process(["init"], database_url)
    assert initialized.returncode == 0, initialized.stderr
    prior: Final = _inventory_rows(origin)
    with gateway.scenario() as scenario, ExitStack() as cleanup:
        cleanup.callback(_restore_inventory_row, origin, prior)
        handle: Final = register_scenario(marker, JsonResponse(content_type="application/json", body={"ok": True}))
        scenario.cleanups.callback(delete_scenario, handle)
        bound: Final = _credential(gateway, scenario, account, canary)
        route_id: Final = f"integration-collected-route-{marker}"
        gateway.post(
            "/cube_control/account_routes",
            {
                "route": {
                    "id": route_id,
                    "path": f"/cube_api/integration-{marker}/collected",
                    "target": f"{handle.api_base()}/v1/collected",
                    "methods": ["GET"],
                    "account_credential": {
                        "account_id": account,
                        "credential_name": bound,
                        "headers": [{"name": "Authorization", "field": "api_key", "prefix": "Bearer "}],
                    },
                }
            },
        )
        scenario.cleanups.callback(_delete_route_if_present, gateway, route_id)
        admin: Final = tmp_path / "admin.env"
        admin.write_text(f"LITELLM_MASTER_KEY={gateway.key}\n")
        admin.chmod(0o600)
        wrong: Final = tmp_path / "wrong.env"
        wrong.write_text(f"LITELLM_MASTER_KEY={wrong_key}\n")
        wrong.chmod(0o600)
        arguments: Final = ["collect-gateway-inventory", "--gateway-origin", origin, "--config"]
        collected: Final = _control_process([*arguments, str(admin)], database_url)
        retried: Final = _control_process([*arguments, str(wrong)], database_url)
        control: Final = _serve_control(cleanup, database_url, tmp_path / "control.log")
        response: Final = httpx.get(f"{control}/api/v1/inventory", timeout=30, trust_env=False)

    outputs: Final = (
        collected.stdout
        + collected.stderr
        + retried.stdout
        + retried.stderr
        + response.text
        + (tmp_path / "control.log").read_text()
    )
    assert not any(secret in outputs for secret in (canary, gateway.key, wrong_key)), (
        "controller output leaked a secret"
    )
    assert [
        (result.returncode, object_value(JSON_OBJECT.validate_json(result.stdout))["status"])
        for result in (collected, retried)
    ] == [(0, "collected"), (1, "error")], collected.stderr + retried.stderr
    assert response.status_code == 200, response.text
    report: Final = object_value(JSON_OBJECT.validate_json(response.content))
    gateways: Final = [
        item
        for item in map(object_value, report["gateways"] if isinstance(report["gateways"], list) else [])
        if item["gateway_origin"] == origin
    ]
    assert [
        (item["state"], item["last_attempt_status"], item["last_good_observed_at"] is not None) for item in gateways
    ] == [("last_attempt_failed", "error", True)], gateways
    entry: Final = next(
        item
        for item in map(object_value, report["accounts"] if isinstance(report["accounts"], list) else [])
        if item["account_id"] == account
    )
    credentials: Final = [
        object_value(item)
        for item in (entry["credentials"] if isinstance(entry["credentials"], list) else [])
        if object_value(item)["gateway_origin"] == origin
    ]
    assert ("database", bound, "bound") in [
        (
            object_value(item["credential"])["origin"],
            object_value(item["credential"])["credential_name"],
            object_value(item["credential"])["binding"],
        )
        for item in credentials
    ], credentials
    routes: Final = entry["routes"] if isinstance(entry["routes"], list) else []
    assert ("database", route_id) in [
        (object_value(object_value(item)["route"])["origin"], object_value(object_value(item)["route"])["route_id"])
        for item in routes
    ], routes
