"""Revocation and rotation reach every gateway worker within the documented bounds.

With several workers each process caches keys and route definitions on its own. A key deletion, a credential
rotation and a route deletion count as complete only when a run of consecutive requests, spread over all
workers by the shared listening socket, shows the new state. INTEGRATION_REVOCATION_REPORT names a file for the
measured durations.
"""

import os
import time
import uuid
from collections.abc import Callable
from hashlib import sha256
from typing import Final

import httpx
from integration._support.upstream import delete_scenario, register_scenario
from integration.cost_calculation.cost_tracking_case import JsonResponse
from pydantic import JsonValue

from tests.integration._support.client import (
    JSON_OBJECT,
    Gateway,
    delete_key_if_present,
    object_value,
    string_value,
)

CONSECUTIVE: Final = 30
BOUND_SECONDS: Final = 90.0


def _observed(gateway: Gateway) -> list[dict[str, JsonValue]]:
    with httpx.Client(base_url=gateway.upstream_url, timeout=15, trust_env=False) as upstream:
        response: Final = upstream.get("/__observations")
        response.raise_for_status()
        requests: Final = JSON_OBJECT.validate_json(response.content)["requests"]
        assert isinstance(requests, list)
        return [object_value(value) for value in requests]


def _settled(label: str, probe: Callable[[], bool]) -> float:
    """Seconds until CONSECUTIVE probes in a row succeed; fails after BOUND_SECONDS."""
    started: Final = time.monotonic()
    streak = 0  # rebind-ok: consecutive successes so far
    while streak < CONSECUTIVE:
        assert time.monotonic() - started < BOUND_SECONDS, f"{label} not settled within {BOUND_SECONDS}s"
        streak = streak + 1 if probe() else 0  # rebind-ok: see above
        if streak == 0:
            time.sleep(0.2)
    elapsed: Final = time.monotonic() - started
    report: Final = os.environ.get("INTEGRATION_REVOCATION_REPORT")
    if report:
        with open(report, "a", encoding="utf-8") as output:
            _ = output.write(f"{label}: {elapsed:.2f}s\n")
    return elapsed


def _key(gateway: Gateway, application: str, path: str) -> tuple[str, str]:
    metadata: Final[dict[str, JsonValue]] = {"cube_application_id": application, "allowed_passthrough_routes": [path]}
    created: Final = gateway.post("/key/generate", {"allowed_routes": [path], "metadata": metadata})
    key: Final = string_value(created["key"])
    return key, sha256(key.encode()).hexdigest()


def test_key_deletion_credential_rotation_and_route_deletion_reach_every_worker(gateway: Gateway) -> None:
    marker: Final = uuid.uuid4().hex
    account: Final = f"integration-account-{marker}"
    path: Final = f"/cube_api/integration-{marker}/revocation"
    route_id: Final = f"integration-revocation-{marker}"
    credential: Final = f"integration-revocation-{marker}"
    handle: Final = register_scenario(marker, JsonResponse(content_type="application/json", body={"ok": True}))
    keys: Final[list[str]] = []  # mutable-ok: created keys, removed in the finally block
    try:
        gateway.post(
            "/credentials",
            {
                "credential_name": credential,
                "credential_values": {"api_key": f"first-{marker}"},
                "credential_info": {"cube_account_id": account, "cube_allowed_origins": [gateway.upstream_url]},
            },
        )
        created: Final = gateway.post(
            "/cube_control/account_routes",
            {
                "route": {
                    "id": route_id,
                    "path": path,
                    "target": f"{handle.api_base()}/v1/check",
                    "methods": ["POST"],
                    "account_credential": {
                        "account_id": account,
                        "credential_name": credential,
                        "headers": [{"name": "Authorization", "field": "api_key", "prefix": "Bearer "}],
                    },
                }
            },
        )
        fingerprint: Final = string_value(object_value(created["route"])["fingerprint"])

        def granted_key(application: str) -> str:
            key, key_hash = _key(gateway, application, path)
            keys.append(key)
            gateway.post(
                f"/cube_control/account_routes/{route_id}/grants",
                {
                    "expected_fingerprint": fingerprint,
                    "grant": {
                        "application_id": application,
                        "key_hash": key_hash,
                        "account_id": account,
                        "path": path,
                        "methods": ["POST"],
                    },
                },
            )
            return key

        first: Final = granted_key(f"integration-app-{marker}")
        second: Final = granted_key(f"integration-app2-{marker}")

        def status(key: str) -> int:
            # A fresh connection per probe: a kept-alive connection would pin every probe to one worker.
            with httpx.Client(base_url=gateway.client.base_url, timeout=15, trust_env=False) as fresh:
                return fresh.post(
                    path, json={"marker": marker}, headers={"Authorization": f"Bearer {key}", "Connection": "close"}
                ).status_code

        # Every worker serves both keys and caches them.
        _ = _settled("route and grants loaded", lambda: status(first) == 200 and status(second) == 200)

        gateway.post("/key/delete", {"keys": [first]})
        key_window: Final = _settled("deleted key refused", lambda: status(first) in (401, 403))
        assert status(second) == 200

        _ = _observed(gateway)
        gateway.request(
            "PATCH",
            f"/credentials/{credential}",
            {
                "credential_name": credential,
                "credential_values": {"api_key": f"second-{marker}"},
                "credential_info": {"cube_account_id": account, "cube_allowed_origins": [gateway.upstream_url]},
            },
        ).raise_for_status()

        def rotated() -> bool:
            if status(second) != 200:
                return False
            seen: Final = _observed(gateway)
            return bool(seen) and seen[-1]["authorization"] == f"Bearer second-{marker}"

        rotation_window: Final = _settled("rotated credential used", rotated)

        gateway.request("DELETE", "/config/pass_through_endpoint", params={"endpoint_id": route_id}).raise_for_status()
        route_window: Final = _settled("deleted route refused", lambda: status(second) == 404)
        _ = _observed(gateway)
        assert status(second) == 404 and _observed(gateway) == []
        assert max(key_window, rotation_window, route_window) < BOUND_SECONDS
    finally:
        if gateway.request("GET", f"/cube_control/account_routes/{route_id}").status_code == 200:
            gateway.request("DELETE", "/config/pass_through_endpoint", params={"endpoint_id": route_id})
        gateway.request("DELETE", f"/credentials/{credential}")
        for key in keys:
            delete_key_if_present(gateway, key)
        delete_scenario(handle)
