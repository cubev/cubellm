import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

import httpx
from pydantic import JsonValue, SecretStr, TypeAdapter, ValidationError

from cube_control.account_routes import (
    AccountResourceAdoptionSpec,
    AccountResourceCreation,
    AccountResourceType,
    AccountRouteHeader,
    AccountRouteResult,
    AccountRouteSpec,
    adopt_account_resource,
    ensure_account_route,
)

SPEC: Final = AccountRouteSpec(
    gateway_origin="http://127.0.0.1:4000",
    route_id="fixture-route",
    account_id="fixture-account",
    credential_name="fixture-credential",
    headers=(AccountRouteHeader(name="X-Api-Key", field="api_key"),),
    target="https://vendor.example/v1/quota",
    path="/cube_api/fixture/quota",
    methods=("GET", "POST"),
)
CAPABILITIES: Final = {
    "application_exact_routes": "exact_path_v1",
    "passthrough_account_grants": "application_account_path_method_v1",
    "passthrough_account_grant_management": "atomic_append_v1",
    "passthrough_account_binding": "configured_credential_v1",
    "passthrough_header_isolation": True,
    "credential_values_full_masking": True,
    "passthrough_account_route_management": "atomic_create_v1",
}


class RouteGateway:
    def __init__(self) -> None:
        self.bodies: list[dict[str, JsonValue]] = []
        self.capabilities: dict[str, JsonValue] = dict(CAPABILITIES)
        self.status: int = 200
        self.created: bool = True
        self.route_changes: Mapping[str, JsonValue] = {}
        self.timeout: bool = False

    def route(self) -> dict[str, JsonValue]:
        return {
            "route_id": SPEC.route_id,
            "account_id": SPEC.account_id,
            "path": SPEC.path,
            "methods": list(SPEC.methods),
            "include_subpath": False,
            "origin": "https://vendor.example",
            "fingerprint": "f" * 64,
            "grants": [],
            **self.route_changes,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cube_control/capabilities":
            return httpx.Response(200, json=self.capabilities)
        assert (request.method, request.url.path) == ("POST", "/cube_control/account_routes")
        assert request.headers["authorization"] == "Bearer inert-master"
        self.bodies.append(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
        if self.timeout:
            raise httpx.ReadTimeout("inert timeout", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "rejected"})
        return httpx.Response(
            200, json={"stored": True, "created": self.created, "enforcement": "not_verified", "route": self.route()}
        )


def ensure(gateway: RouteGateway, spec: AccountRouteSpec = SPEC) -> AccountRouteResult:
    return ensure_account_route(spec, SecretStr("inert-master"), httpx.MockTransport(gateway))


class AccountRouteTests(unittest.TestCase):
    def test_creation_sends_one_grantless_definition_and_returns_the_pinned_fingerprint(self) -> None:
        gateway: Final = RouteGateway()
        result: Final = ensure(gateway)
        self.assertEqual((result.status, result.fingerprint, result.enforcement), ("created", "f" * 64, "not_verified"))
        self.assertEqual(
            gateway.bodies,
            [
                {
                    "route": {
                        "id": "fixture-route",
                        "path": "/cube_api/fixture/quota",
                        "target": "https://vendor.example/v1/quota",
                        "methods": ["GET", "POST"],
                        "include_subpath": False,
                        "account_credential": {
                            "account_id": "fixture-account",
                            "credential_name": "fixture-credential",
                            "headers": [{"name": "X-Api-Key", "field": "api_key", "prefix": ""}],
                            "allowed_query_parameters": [],
                        },
                    }
                }
            ],
        )
        self.assertNotIn("inert-master", result.model_dump_json())

    def test_existing_identical_route_is_reported_as_existing(self) -> None:
        gateway: Final = RouteGateway()
        gateway.created = False
        gateway.route_changes = {
            "grants": [
                {
                    "application_id": "fixture-app",
                    "key_hash": "a" * 64,
                    "account_id": "fixture-account",
                    "path": SPEC.path,
                    "methods": ["GET"],
                }
            ]
        }
        self.assertEqual(ensure(gateway).status, "existing")

    def test_resource_types_are_sent_only_to_gateways_with_resource_ownership(self) -> None:
        resources: Final = (
            AccountResourceType(
                name="video-job",
                item_paths=("/cube_api/fixture/quota/videos/{resource_id}",),
                creation=AccountResourceCreation(
                    path="/cube_api/fixture/quota/videos", methods=("POST",), id_selector=("data", 0, "id")
                ),
            ),
            AccountResourceType(name="file", item_paths=("/cube_api/fixture/quota/{resource_id}/content",)),
        )
        spec: Final = SPEC.model_copy(update={"include_subpath": True, "resource_types": resources})
        old: Final = RouteGateway()
        self.assertEqual(ensure(old, spec).status, "error")
        self.assertEqual(old.bodies, [])
        gateway: Final = RouteGateway()
        gateway.capabilities["passthrough_account_resources"] = "application_ownership_v1"
        gateway.route_changes = {"include_subpath": True}
        self.assertEqual(ensure(gateway, spec).status, "created")
        self.assertEqual(
            gateway.bodies,
            [
                {
                    "route": {
                        "id": "fixture-route",
                        "path": "/cube_api/fixture/quota",
                        "target": "https://vendor.example/v1/quota",
                        "methods": ["GET", "POST"],
                        "include_subpath": True,
                        "account_credential": {
                            "account_id": "fixture-account",
                            "credential_name": "fixture-credential",
                            "headers": [{"name": "X-Api-Key", "field": "api_key", "prefix": ""}],
                            "allowed_query_parameters": [],
                            "resource_types": [
                                {
                                    "name": "video-job",
                                    "item_paths": ["/cube_api/fixture/quota/videos/{resource_id}"],
                                    "creation": {
                                        "path": "/cube_api/fixture/quota/videos",
                                        "methods": ["POST"],
                                        "id_selector": ["data", 0, "id"],
                                    },
                                },
                                {"name": "file", "item_paths": ["/cube_api/fixture/quota/{resource_id}/content"]},
                            ],
                        },
                    }
                }
            ],
        )

    def test_route_bases_and_creation_paths_stay_concrete_and_templates_follow_native_grammar(self) -> None:
        with self.assertRaises(ValidationError):
            AccountRouteSpec.model_validate(SPEC.model_dump() | {"path": "/cube_api/fixture/{resource_id}"})
        for path in (
            "/cube_api/fixture/jobs/{resource_id}/",
            "/cube_api/fixture/jobs//{resource_id}",
            "/cube_api/fixture/jobs/x{resource_id}",
            "/cube_api/fixture/{resource_id}/{resource_id}",
            "/cube_api/fixture/jobs/{id}",
            "/cube_api/fixture/jobs",
        ):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                AccountResourceType(name="job", item_paths=(path,))
        for path in ("/cube_api/fixture/jobs/{resource_id}", "/cube_api/fixture/{resource_id}/cancel"):
            with self.subTest(path=path):
                self.assertEqual(AccountResourceType(name="job", item_paths=(path,)).item_paths, (path,))
        with self.assertRaises(ValidationError):
            AccountResourceCreation(path="/cube_api/fixture/jobs/{resource_id}", methods=("POST",), id_selector=("id",))
        with self.assertRaises(ValidationError):
            AccountRouteSpec.model_validate(
                SPEC.model_dump()
                | {"resource_types": [{"name": "job", "item_paths": ["/cube_api/fixture/quota/{resource_id}"]}]}
            )

    def test_gateway_without_route_management_is_never_asked_to_store(self) -> None:
        gateway: Final = RouteGateway()
        del gateway.capabilities["passthrough_account_route_management"]
        self.assertEqual(ensure(gateway).status, "error")
        self.assertEqual(gateway.bodies, [])

    def test_definite_rejections_are_errors_and_unknown_outcomes_are_uncertain(self) -> None:
        for status, expected in (
            (400, "error"),
            (409, "error"),
            (422, "error"),
            (500, "uncertain"),
            (503, "uncertain"),
        ):
            with self.subTest(status=status):
                gateway = RouteGateway()
                gateway.status = status
                result = ensure(gateway)
                self.assertEqual(result.status, expected)
                self.assertIsNone(result.fingerprint)
        timed_out: Final = RouteGateway()
        timed_out.timeout = True
        self.assertEqual(ensure(timed_out).status, "uncertain")

    def test_receipt_that_differs_from_the_request_is_rejected(self) -> None:
        receipts: Final[tuple[Mapping[str, JsonValue], ...]] = (
            {"route_id": "other-route"},
            {"account_id": "other-account"},
            {"path": "/cube_api/fixture/other"},
            {"methods": ["GET"]},
            {"include_subpath": True},
            {"origin": "https://other.example"},
            {
                "grants": [
                    {
                        "application_id": "fixture-app",
                        "key_hash": "a" * 64,
                        "account_id": "other-account",
                        "path": SPEC.path,
                        "methods": ["GET"],
                    }
                ]
            },
        )
        for changes in receipts:
            with self.subTest(changes=changes):
                gateway = RouteGateway()
                gateway.route_changes = changes
                result = ensure(gateway)
                self.assertEqual((result.status, result.fingerprint), ("error", None))

    def test_specification_rejects_remote_admin_origins_and_unmanaged_paths(self) -> None:
        for changes in (
            {"gateway_origin": "http://192.168.1.2:4000"},
            {"gateway_origin": "http://127.0.0.1"},
            {"path": "/vendor/quota"},
            {"path": "/cube_api/"},
            {"path": "/cube_api/fixture/"},
            {"path": "/cube_api/a/../b"},
            {"methods": ["GET", "GET"]},
            {"target": "https://user:secret@vendor.example/quota"},
            {"target": "ftp://vendor.example/quota"},
            {"target": "https://vendor.example:invalid/quota"},
            {"target": "https://[::1/quota"},
            {"target": "https://vendor.example:99999/quota"},
            {"target": "https://vendor.example/quota?key=value"},
            {"target": "https://vendor.example/quota#fragment"},
            {"target": "https://vendor example/quota"},
            {"target": "https://vendor%20example/quota"},
            {"target": "https://vendor%0aexample/quota"},
            {"target": "https://vendor\texample/quota"},
            {"target": "https://vendor.example/a b"},
            {"target": "https://vendor.example/a\x7fb"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                AccountRouteSpec.model_validate(SPEC.model_dump() | changes)

    def test_specification_keeps_internal_hosts_zones_and_encoded_paths(self) -> None:
        for target, origin in (
            ("https://api_service.local/quota", "https://api_service.local"),
            ("https://[fe80::1%25eth0]/quota", "https://[fe80::1%25eth0]"),
            ("https://xn--bcher-kva.example/quota", "https://xn--bcher-kva.example"),
            ("https://vendor.example:8443/a%20b", "https://vendor.example:8443"),
            ("http://127.0.0.1:9000/quota", "http://127.0.0.1:9000"),
        ):
            with self.subTest(target=target):
                spec = AccountRouteSpec.model_validate(SPEC.model_dump() | {"target": target})
                self.assertEqual(spec.origin, origin)
                gateway = RouteGateway()
                gateway.route_changes = {"origin": origin}
                self.assertEqual(ensure(gateway, spec).status, "created")


class AccountRouteCliTests(unittest.TestCase):
    def run_cli(self, gateway: RouteGateway, raw: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        requests: Final[list[str]] = []

        class Handler(BaseHTTPRequestHandler):
            def handle_request(self) -> None:
                requests.append(self.command + " " + self.path)
                body: Final = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                response: Final = gateway(
                    httpx.Request(
                        self.command,
                        "http://localhost" + self.path,
                        headers={"Authorization": self.headers.get("Authorization", "")},
                        content=body,
                    )
                )
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            def do_GET(self) -> None:
                self.handle_request()

            def do_POST(self) -> None:
                self.handle_request()

            def log_message(self, format: str, *args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as root, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread: Final = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                admin: Final = Path(root) / "admin.env"
                admin.write_text("LITELLM_MASTER_KEY=inert-master\n")
                admin.chmod(0o600)
                result: Final = subprocess.run(
                    [sys.executable, "-m", "cube_control", "provision-account-route", "--config", str(admin)],
                    input=raw.replace("GATEWAY_PORT", str(server.server_port)),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=20,
                    env={name: value for name, value in os.environ.items() if name != "CUBE_CONTROL_DATABASE_URL"},
                )
            finally:
                server.shutdown()
                thread.join()
        return result, requests

    def spec_json(self, **changes: str) -> str:
        return json.dumps(SPEC.model_dump(mode="json") | {"gateway_origin": "http://127.0.0.1:GATEWAY_PORT"} | changes)

    def test_valid_specification_reaches_the_gateway_once_and_prints_the_fingerprint(self) -> None:
        gateway: Final = RouteGateway()
        result, requests = self.run_cli(gateway, self.spec_json())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["fingerprint"], "f" * 64)
        self.assertEqual(requests, ["GET /cube_control/capabilities", "POST /cube_control/account_routes"])
        self.assertNotIn("inert-master", result.stdout + result.stderr)

    def test_invalid_input_is_rejected_before_credentials_or_gateway_are_used(self) -> None:
        for raw in (
            self.spec_json(target="https://vendor.example:invalid/quota"),
            self.spec_json(target="https://[::1/quota"),
            self.spec_json(target="https://vendor.example:99999/quota"),
            self.spec_json(path="/cube_api/../key/generate"),
            "{not json",
            " " * 65537,
            self.spec_json() + " " * 70000,
        ):
            with self.subTest(raw=raw[:80]):
                gateway = RouteGateway()
                result, requests = self.run_cli(gateway, raw)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "Account route specification rejected; input values are not logged\n")
                self.assertEqual(requests, [])
                self.assertEqual(gateway.bodies, [])


if __name__ == "__main__":
    unittest.main()


ADOPTION: Final = AccountResourceAdoptionSpec(
    gateway_origin="http://127.0.0.1:4000",
    route_id="fixture-route",
    expected_fingerprint="f" * 64,
    resource_type="file",
    resource_id="file_1",
    application_id="fixture-app",
)


class AdoptionGateway:
    def __init__(self, status: int = 200, inserted: bool = True, owner: str = "fixture-app") -> None:
        self.status: Final = status
        self.inserted: Final = inserted
        self.owner: Final = owner
        self.capabilities: dict[str, JsonValue] = {"passthrough_account_resources": "application_ownership_v1"}
        self.bodies: list[dict[str, JsonValue]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cube_control/capabilities":
            return httpx.Response(200, json=self.capabilities)
        assert (request.method, request.url.path) == ("POST", "/cube_control/account_routes/fixture-route/resources")
        self.bodies.append(TypeAdapter(dict[str, JsonValue]).validate_json(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "rejected"})
        return httpx.Response(
            200,
            json={
                "stored": True,
                "inserted": self.inserted,
                "enforcement": "not_verified",
                "ownership": {
                    "route_id": "fixture-route",
                    "owner_application_id": self.owner,
                    "provenance": "adopted",
                    "creator_key_hash": None,
                },
            },
        )


def adopt(gateway: AdoptionGateway) -> str:
    return adopt_account_resource(ADOPTION, SecretStr("inert-master"), httpx.MockTransport(gateway)).status


class AccountResourceAdoptionTests(unittest.TestCase):
    def test_adoption_sends_the_pinned_fingerprint_and_reports_new_and_existing_ownership(self) -> None:
        gateway: Final = AdoptionGateway()
        self.assertEqual(adopt(gateway), "adopted")
        self.assertEqual(
            gateway.bodies,
            [
                {
                    "expected_fingerprint": "f" * 64,
                    "resource_type": "file",
                    "resource_id": "file_1",
                    "application_id": "fixture-app",
                }
            ],
        )
        self.assertEqual(adopt(AdoptionGateway(inserted=False)), "existing")

    def test_conflicts_rejections_and_foreign_receipts_never_report_ownership(self) -> None:
        for gateway, expected in (
            (AdoptionGateway(status=409), "conflict"),
            (AdoptionGateway(status=400), "error"),
            (AdoptionGateway(status=404), "error"),
            (AdoptionGateway(status=502), "uncertain"),
            (AdoptionGateway(owner="other-app"), "error"),
        ):
            with self.subTest(status=gateway.status, owner=gateway.owner):
                self.assertEqual(adopt(gateway), expected)

    def test_gateways_without_resource_ownership_are_never_asked_to_adopt(self) -> None:
        gateway: Final = AdoptionGateway()
        gateway.capabilities = {}
        self.assertEqual(adopt(gateway), "error")
        self.assertEqual(gateway.bodies, [])

    def test_adoption_specification_rejects_non_canonical_ids_and_remote_origins(self) -> None:
        for changes in (
            {"resource_id": "a/b"},
            {"resource_id": ".."},
            {"resource_id": "a b"},
            {"expected_fingerprint": "f" * 63},
            {"gateway_origin": "https://remote.example"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                AccountResourceAdoptionSpec.model_validate(ADOPTION.model_dump() | changes)
