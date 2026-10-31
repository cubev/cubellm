import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

import httpx
from pydantic import SecretStr, TypeAdapter, ValidationError

from cube_control.application_access import (
    ApplicationAccessSpec,
    deliver_application_access,
    prepare_application_access,
    provision_application_access,
)

SPEC: Final = ApplicationAccessSpec(
    application_id="fixture-app",
    team_id="fixture-team",
    gateway_origin="http://127.0.0.1:4000",
    models=("fixture-model",),
    routes=("/v1/chat/completions",),
)


class GatewayFixture:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.user: dict[str, object] | None = None
        self.key: dict[str, object] | None = None
        self.timeout_path: str | None = None
        self.custom_keys: bool = True
        self.override_credentials: bool = False
        self.fail_creation: bool = False
        self.lookups: list[str] = []
        self.member_role: str | None = "user"
        self.model_client_overrides: tuple[str, ...] = ()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path: Final = request.url.path
        if request.method == "POST":
            self.posts.append(path)
            body: Final = TypeAdapter(dict[str, object]).validate_json(request.content)
            if not self.fail_creation:
                if path == "/user/new":
                    if body.get("auto_create_key") is not False or body.get("send_invite_email") is not False:
                        return httpx.Response(400)
                    self.user = body
                elif path == "/key/generate":
                    self.key = body | {"status": "active"}
            if path == self.timeout_path:
                raise httpx.ReadTimeout("inert upstream error may contain secrets", request=request)
            return httpx.Response(200, json={})
        if path == "/cube_control/capabilities":
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "application_identity": "authenticated_object_v1",
                    "virtual_key_log_redaction": True,
                    "custom_keys_allowed": self.custom_keys,
                    "client_side_credentials_allowed": self.override_credentials,
                },
            )
        if path == "/team/info":
            return httpx.Response(
                200,
                json={
                    "team_info": {
                        "team_id": SPEC.team_id,
                        "models": list(SPEC.models),
                        "members_with_roles": [{"user_id": SPEC.user_id, "role": self.member_role}]
                        if self.user is not None and self.member_role is not None
                        else [],
                    }
                },
            )
        if path == "/model/info":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "model_name": name,
                            "litellm_params": {"configurable_clientside_auth_params": self.model_client_overrides},
                        }
                        for name in SPEC.models
                    ]
                },
            )
        if path == "/user/info":
            return httpx.Response(404) if self.user is None else httpx.Response(200, json={"user_info": self.user})
        if path == "/key/info":
            self.lookups.append(request.url.params["key"])
            return httpx.Response(404) if self.key is None else httpx.Response(200, json={"info": self.key})
        return httpx.Response(404)


class ApplicationAccessTests(unittest.TestCase):
    def test_scope_rejects_remote_management_wildcards_and_management_routes(self) -> None:
        for changes in (
            {"gateway_origin": "http://localhost:4000"},
            {"gateway_origin": "http://192.168.1.10:4000"},
            {"gateway_origin": "http://127.0.0.1:4000/key"},
            {"gateway_origin": "http://user@127.0.0.1:4000"},
            {"models": ["*"]},
            {"models": ["all-proxy-models"]},
            {"models": ["fixture-model", "fixture-model"]},
            {"routes": ["/key/generate"]},
            {"routes": ["/v1"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ApplicationAccessSpec.model_validate(SPEC.model_dump() | changes)

    def test_preparation_is_private_stable_and_rejects_scope_changes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepared: Final = prepare_application_access(SPEC, directory)
            self.assertEqual(prepared.status, "prepared")
            raw: Final = (directory / "state.json").read_bytes()
            self.assertEqual(prepare_application_access(SPEC, directory), prepared)
            self.assertEqual((directory / "state.json").read_bytes(), raw)
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(directory / "state.json").st_mode & 0o777, 0o600)
            secret: Final = TypeAdapter(dict[str, str]).validate_python(
                {"token": TypeAdapter(dict[str, object]).validate_json(raw)["token"]}
            )["token"]
            self.assertNotIn(secret, prepared.model_dump_json())
            self.assertEqual(
                prepare_application_access(SPEC.model_copy(update={"team_id": "other-team"}), directory).status, "error"
            )

    def test_provisioning_uses_separate_internal_identity_and_verifies_key_scope(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(result.status, "provisioned")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])
            self.assertIsNotNone(gateway.user)
            self.assertIsNotNone(gateway.key)
            assert gateway.user is not None and gateway.key is not None
            self.assertEqual(gateway.user["user_role"], "internal_user")
            self.assertEqual(gateway.key["user_id"], SPEC.user_id)
            self.assertEqual(gateway.key["allowed_routes"], list(SPEC.routes))
            self.assertEqual(gateway.key["models"], list(SPEC.models))
            self.assertEqual(gateway.key["key_type"], "default")
            self.assertTrue(all(len(value) == 64 and not value.startswith("sk-") for value in gateway.lookups))
            self.assertNotIn(str(gateway.key["key"]), result.model_dump_json())
            self.assertNotIn("inert-master", (directory / "state.json").read_text())
            second: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(second.status, "provisioned")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])

    def test_timeout_after_commit_reconciles_without_repeating_mutations(self) -> None:
        for timeout_path in ("/user/new", "/key/generate"):
            with self.subTest(path=timeout_path):
                self._assert_timeout_reconciles(timeout_path)

    def _assert_timeout_reconciles(self, timeout_path: str) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            gateway.timeout_path = timeout_path
            first: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(first.status, "uncertain")
            gateway.timeout_path = None
            second: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(second.status, "provisioned")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])

    def test_unknown_outcome_never_reposts_even_after_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            gateway.timeout_path = "/user/new"
            gateway.fail_creation = True
            first: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(first.status, "uncertain")
            second: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(second.status, "uncertain")
            self.assertEqual(gateway.posts, ["/user/new"])

    def test_unsafe_gateway_policies_cause_no_mutations(self) -> None:
        for custom_keys, overrides in ((False, False), (True, True)):
            with self.subTest(custom_keys=custom_keys, overrides=overrides):
                self._assert_unsafe_policy(custom_keys, overrides)

    def _assert_unsafe_policy(self, custom_keys: bool, overrides: bool) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            gateway.custom_keys = custom_keys
            gateway.override_credentials = overrides
            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(result.status, "error")
            self.assertEqual(gateway.posts, [])

    def test_privileged_principal_is_never_reused_or_modified(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            gateway.user = {
                "user_id": SPEC.user_id,
                "user_role": "proxy_admin",
                "models": [],
                "teams": [],
                "metadata": {},
            }
            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(result.status, "error")
            self.assertEqual(gateway.posts, [])

    def test_model_level_credential_override_prevents_issuance(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            gateway.model_client_overrides = ("api_key",)
            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(result.status, "error")
            self.assertEqual(gateway.posts, [])

    def test_state_symlinks_and_public_permissions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            state: Final = directory / "state.json"
            state.chmod(0o644)
            self.assertEqual(prepare_application_access(SPEC, directory).status, "error")
            state.unlink()
            state.symlink_to(Path(root) / "outside")
            self.assertEqual(prepare_application_access(SPEC, directory).status, "error")
            alias: Final = Path(root) / "alias"
            alias.symlink_to(directory)
            self.assertEqual(prepare_application_access(SPEC, alias).status, "error")

    def test_second_controller_cannot_mutate_a_locked_application(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            with (directory / ".lock").open("r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                gateway: Final = GatewayFixture()
                result: Final = provision_application_access(
                    directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
                )
            self.assertEqual(result.status, "error")
            self.assertEqual(gateway.posts, [])

    def test_changed_key_permissions_are_rejected_without_overwriting_them(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            assert gateway.key is not None
            gateway.key["allowed_routes"] = ["/key/generate"]
            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(result.status, "error")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])
            self.assertEqual(gateway.key["allowed_routes"], ["/key/generate"])

    def test_partial_user_creation_never_mints_key_without_confirmed_team_membership(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            gateway.member_role = None
            first: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(first.status, "uncertain")
            self.assertEqual(gateway.posts, ["/user/new"])
            gateway.member_role = "admin"
            privileged: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(privileged.status, "uncertain")
            self.assertEqual(gateway.posts, ["/user/new"])
            gateway.member_role = "user"
            repaired: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(repaired.status, "provisioned")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])

    def test_unavailable_capabilities_or_models_cause_no_changes(self) -> None:
        for missing_path in ("/cube_control/capabilities", "/team/info", "/model/info"):
            with self.subTest(missing_path=missing_path):
                self._assert_missing_preflight(missing_path)

    def _assert_missing_preflight(self, missing_path: str) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()

            def missing(request: httpx.Request) -> httpx.Response:
                return httpx.Response(404) if request.url.path == missing_path else gateway(request)

            result: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(missing)
            )
            self.assertEqual(result.status, "error")
            self.assertEqual(gateway.posts, [])

    def test_prepare_cli_runs_without_database_and_never_prints_the_credential(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            result: Final = subprocess.run(
                [sys.executable, "-m", "cube_control", "prepare-application", "--state-directory", str(directory)],
                input=SPEC.model_dump_json(),
                text=True,
                capture_output=True,
                check=False,
                env={name: value for name, value in os.environ.items() if name != "CUBE_CONTROL_DATABASE_URL"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"status":"prepared"', result.stdout)
            raw: Final = TypeAdapter(dict[str, object]).validate_json((directory / "state.json").read_bytes())
            self.assertNotIn(str(raw["token"]), result.stdout + result.stderr)

    def test_delivery_verifies_scope_and_writes_only_the_app_credential_privately(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            destination: Final = Path(root) / "consumer"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            result: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(result.status, "delivered")
            assert gateway.key is not None
            content: Final = (destination / "api-key").read_text()
            self.assertEqual(content, str(gateway.key["key"]) + "\n")
            self.assertNotIn("inert-master", content)
            self.assertNotIn(str(gateway.key["key"]), result.model_dump_json())
            self.assertEqual(os.stat(destination).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(destination / "api-key").st_mode & 0o777, 0o600)
            inode: Final = os.stat(destination / "api-key").st_ino
            again: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(again.status, "delivered")
            self.assertEqual(os.stat(destination / "api-key").st_ino, inode)
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])

    def test_delivery_never_provisions_missing_users_or_keys(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            destination: Final = Path(root) / "consumer"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            before: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(before.status, "error")
            self.assertEqual(gateway.posts, [])
            self.assertFalse(destination.exists())
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            gateway.key = None
            after: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(after.status, "error")
            self.assertFalse(destination.exists())
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])

    def test_delivery_rejects_later_scope_changes_and_never_exposes_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            destination: Final = Path(root) / "consumer"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            assert gateway.key is not None
            gateway.key["allowed_routes"] = ["/key/generate"]
            result: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(result.status, "error")
            self.assertFalse(destination.exists())
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])

    def test_delivery_does_not_replace_existing_credentials_or_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            destination: Final = Path(root) / "consumer"
            destination.mkdir(mode=0o700)
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            credential: Final = destination / "api-key"
            credential.write_text("existing-unrelated-credential\n")
            credential.chmod(0o600)
            result: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(result.status, "error")
            self.assertEqual(credential.read_text(), "existing-unrelated-credential\n")
            credential.unlink()
            outside: Final = Path(root) / "outside"
            credential.symlink_to(outside)
            symlink_result: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(symlink_result.status, "error")
            self.assertTrue(credential.is_symlink())
            self.assertFalse(outside.exists())

    def test_delivery_rejects_a_public_destination_and_busy_destination(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            destination: Final = Path(root) / "consumer"
            destination.mkdir(mode=0o755)
            destination.chmod(0o755)
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            public: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(public.status, "error")
            self.assertFalse((destination / "api-key").exists())
            destination.chmod(0o700)
            lock_path: Final = destination / ".lock"
            lock_path.touch(mode=0o600)
            with lock_path.open("r+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy: Final = deliver_application_access(
                    directory,
                    destination,
                    SecretStr("inert-master"),
                    httpx.MockTransport(gateway),
                )
            self.assertEqual(busy.status, "error")
            self.assertFalse((destination / "api-key").exists())

    def test_delivery_never_exports_after_a_failed_gateway_verification(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            destination: Final = Path(root) / "consumer"
            prepare_application_access(SPEC, directory)

            def unavailable(request: httpx.Request) -> httpx.Response:
                raise httpx.ReadTimeout("inert-secret-in-upstream-diagnostic", request=request)

            result: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(unavailable),
            )
            self.assertEqual(result.status, "uncertain")
            self.assertNotIn("inert-secret-in-upstream-diagnostic", result.model_dump_json())
            self.assertFalse(destination.exists())

    def test_delivery_rejects_a_matching_credential_file_with_public_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            destination: Final = Path(root) / "consumer"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            delivered: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(delivered.status, "delivered")
            credential: Final = destination / "api-key"
            credential.chmod(0o644)
            rejected: Final = deliver_application_access(
                directory,
                destination,
                SecretStr("inert-master"),
                httpx.MockTransport(gateway),
            )
            self.assertEqual(rejected.status, "error")
            self.assertEqual(os.stat(credential).st_mode & 0o777, 0o644)

    def test_delivery_cli_uses_read_only_gateway_checks_and_never_prints_secrets(self) -> None:
        gateway: Final = GatewayFixture()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.headers.get("Authorization") != "Bearer inert-master":
                    self.send_error(403)
                    return
                response: Final = gateway(httpx.Request("GET", "http://localhost" + self.path))
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            def log_message(self, format: str, *args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as root, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread: Final = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                directory: Final = Path(root) / "application"
                destination: Final = Path(root) / "consumer"
                spec: Final = ApplicationAccessSpec.model_validate(
                    SPEC.model_dump()
                    | {
                        "gateway_origin": f"http://127.0.0.1:{server.server_port}",
                    }
                )
                prepare_application_access(spec, directory)
                provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
                admin: Final = Path(root) / "admin.env"
                admin.write_text("LITELLM_MASTER_KEY=inert-master\n")
                admin.chmod(0o600)
                result: Final = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "cube_control",
                        "deliver-application",
                        "--state-directory",
                        str(directory),
                        "--destination-directory",
                        str(destination),
                        "--config",
                        str(admin),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=20,
                    env={name: value for name, value in os.environ.items() if name != "CUBE_CONTROL_DATABASE_URL"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('"status":"delivered"', result.stdout)
                assert gateway.key is not None
                self.assertNotIn(str(gateway.key["key"]), result.stdout + result.stderr)
                self.assertNotIn("inert-master", result.stdout + result.stderr)
                self.assertEqual((destination / "api-key").read_text(), str(gateway.key["key"]) + "\n")
                self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
