import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hashlib import sha256
from pathlib import Path
from typing import Final

import httpx
from pydantic import SecretStr, TypeAdapter, ValidationError

from cube_control.application_access import (
    ApplicationAccessSpec,
    deliver_application_access,
    prepare_application_access,
    prepare_application_rotation,
    provision_application_access,
    revoke_application_access,
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
                elif path == "/key/block":
                    if self.key is None or body.get("key") != sha256(str(self.key["key"]).encode()).hexdigest():
                        return httpx.Response(404)
                    self.key = self.key | {"blocked": True, "status": "revoked"}
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


def gateway_handler(gateway: GatewayFixture) -> type[BaseHTTPRequestHandler]:
    """Serves the fixture over real HTTP for CLI subprocesses: reads with the master key, and no write but a block."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("Authorization") != "Bearer inert-master":
                self.send_error(403)
                return
            response: Final = gateway(httpx.Request("GET", "http://localhost" + self.path))
            self._reply(response)

        def do_POST(self) -> None:
            if self.headers.get("Authorization") != "Bearer inert-master" or self.path != "/key/block":
                self.send_error(403)
                return
            response: Final = gateway(
                httpx.Request(
                    "POST",
                    "http://localhost" + self.path,
                    content=self.rfile.read(int(self.headers.get("Content-Length", "0"))),
                )
            )
            self._reply(response)

        def _reply(self, response: httpx.Response) -> None:
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


class KeyedGateway(GatewayFixture):
    """Keeps every generated key under its hash, so a predecessor and its successor can exist side by side."""

    def __init__(self) -> None:
        super().__init__()
        self.keys: Final[dict[str, dict[str, object]]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path: Final = request.url.path
        if path == "/key/info":
            stored: Final = self.keys.get(request.url.params["key"])
            return httpx.Response(404) if stored is None else httpx.Response(200, json={"info": stored})
        if request.method != "POST" or path not in ("/key/generate", "/key/block"):
            return super().__call__(request)
        self.posts.append(path)
        body: Final = TypeAdapter(dict[str, object]).validate_json(request.content)
        if path == "/key/generate":
            self.keys[sha256(str(body["key"]).encode()).hexdigest()] = body | {"status": "active"}
            return httpx.Response(200, json={})
        blocked: Final = self.keys.get(str(body["key"]))
        if blocked is None:
            return httpx.Response(404)
        self.keys[str(body["key"])] = blocked | {"blocked": True, "status": "revoked"}
        return httpx.Response(200, json={})


def stored_token(directory: Path) -> str:
    return str(TypeAdapter(dict[str, object]).validate_json((directory / "state.json").read_bytes())["token"])


class ApplicationRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        root: Final = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.predecessor = Path(root.name) / "application"
        self.successor = Path(root.name) / "application-next"
        self.destination = Path(root.name) / "consumer"
        self.gateway = KeyedGateway()
        self.transport = httpx.MockTransport(self.gateway)
        self.master = SecretStr("inert-master")
        prepare_application_access(SPEC, self.predecessor)
        provision_application_access(self.predecessor, self.master, self.transport)

    def test_rotation_issues_a_successor_replaces_the_delivered_key_and_blocks_only_the_predecessor(self) -> None:
        deliver_application_access(self.predecessor, self.destination, self.master, self.transport)
        old_token: Final = stored_token(self.predecessor)
        prepared: Final = prepare_application_rotation(self.predecessor, self.successor)
        self.assertEqual(prepared.status, "prepared")
        new_token: Final = stored_token(self.successor)
        self.assertNotEqual(new_token, old_token)
        self.assertRegex(new_token, r"^sk-[A-Za-z0-9_-]{43}$")
        self.assertEqual(os.stat(self.successor).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.successor / "state.json").st_mode & 0o777, 0o600)
        self.assertEqual(prepare_application_rotation(self.predecessor, self.successor), prepared)
        self.assertEqual(stored_token(self.successor), new_token)
        self.assertEqual(self.gateway.posts, ["/user/new", "/key/generate"])

        provisioned: Final = provision_application_access(self.successor, self.master, self.transport)
        self.assertEqual(provisioned.status, "provisioned")
        old_hash, new_hash = (sha256(token.encode()).hexdigest() for token in (old_token, new_token))
        self.assertEqual(sorted(self.gateway.keys), sorted((old_hash, new_hash)))
        self.assertEqual(self.gateway.keys[new_hash]["metadata"], self.gateway.keys[old_hash]["metadata"])

        unreplaced: Final = deliver_application_access(self.successor, self.destination, self.master, self.transport)
        self.assertEqual(unreplaced.status, "error")
        self.assertEqual((self.destination / "api-key").read_text(), old_token + "\n")
        delivered: Final = deliver_application_access(
            self.successor, self.destination, self.master, self.transport, self.predecessor
        )
        self.assertEqual(delivered.status, "delivered")
        self.assertEqual((self.destination / "api-key").read_text(), new_token + "\n")
        self.assertEqual(os.stat(self.destination / "api-key").st_mode & 0o777, 0o600)
        self.assertEqual(sorted(os.listdir(self.destination)), [".lock", "api-key"])
        again: Final = deliver_application_access(
            self.successor, self.destination, self.master, self.transport, self.predecessor
        )
        self.assertEqual(again, delivered)

        revoked: Final = revoke_application_access(self.predecessor, self.master, self.transport)
        self.assertEqual(revoked.status, "blocked")
        self.assertEqual(
            (self.gateway.keys[old_hash].get("blocked"), self.gateway.keys[new_hash].get("blocked")), (True, None)
        )
        self.assertEqual(self.gateway.posts, ["/user/new", "/key/generate", "/key/generate", "/key/block"])
        self.assertEqual(provision_application_access(self.successor, self.master, self.transport), provisioned)
        for result in (prepared, provisioned, unreplaced, delivered, revoked):
            self.assertNotIn(old_token, result.model_dump_json())
            self.assertNotIn(new_token, result.model_dump_json())

    def test_rotation_never_starts_from_a_missing_or_revoked_application_or_into_a_foreign_state(self) -> None:
        missing: Final = prepare_application_rotation(self.predecessor.with_name("absent"), self.successor)
        self.assertEqual(missing.status, "error")
        self.assertFalse(self.successor.exists())
        same: Final = prepare_application_rotation(self.predecessor, self.predecessor)
        self.assertEqual(same.status, "error")

        other: Final = ApplicationAccessSpec.model_validate(SPEC.model_dump() | {"application_id": "other-app"})
        foreign: Final = self.successor.with_name("foreign")
        prepare_application_access(other, foreign)
        foreign_token: Final = stored_token(foreign)
        self.assertEqual(prepare_application_rotation(self.predecessor, foreign).status, "error")
        self.assertEqual(stored_token(foreign), foreign_token)
        rerolled: Final = self.successor.with_name("rerolled")
        prepare_application_access(SPEC, rerolled)
        self.assertEqual(prepare_application_rotation(self.predecessor, rerolled).status, "error")

        self.assertEqual(prepare_application_rotation(self.predecessor, self.successor).status, "prepared")
        provision_application_access(self.successor, self.master, self.transport)
        revoke_application_access(self.successor, self.master, self.transport)
        self.assertEqual(prepare_application_rotation(self.predecessor, self.successor).status, "error")
        shutil.rmtree(self.successor)

        revoke_application_access(self.predecessor, self.master, self.transport)
        after_revocation: Final = prepare_application_rotation(self.predecessor, self.successor)
        self.assertEqual(after_revocation.status, "error")
        self.assertFalse((self.successor / "state.json").exists())
        self.assertEqual(
            self.gateway.posts, ["/user/new", "/key/generate", "/key/generate", "/key/block", "/key/block"]
        )

    def test_a_delivered_key_is_only_replaced_when_it_is_the_named_predecessor(self) -> None:
        deliver_application_access(self.predecessor, self.destination, self.master, self.transport)
        old_token: Final = stored_token(self.predecessor)
        prepare_application_rotation(self.predecessor, self.successor)
        provision_application_access(self.successor, self.master, self.transport)
        unrelated: Final = self.successor.with_name("unrelated")
        prepare_application_access(SPEC, unrelated)
        credential: Final = self.destination / "api-key"

        not_a_predecessor: Final = deliver_application_access(
            self.successor, self.destination, self.master, self.transport, unrelated
        )
        self.assertEqual(not_a_predecessor.status, "error")
        self.assertEqual(credential.read_text(), old_token + "\n")

        other: Final = ApplicationAccessSpec.model_validate(SPEC.model_dump() | {"application_id": "other-app"})
        foreign: Final = self.successor.with_name("foreign")
        prepare_application_access(other, foreign)
        for state in (unrelated, foreign):
            with self.subTest(delivered_key_of=state.name):
                _ = credential.write_text(stored_token(state) + "\n")
                refused: Final = deliver_application_access(
                    self.successor, self.destination, self.master, self.transport, state
                )
                self.assertEqual(refused.status, "error")
                self.assertEqual(credential.read_text(), stored_token(state) + "\n")

        credential.write_text("existing-unrelated-credential\n")
        other_content: Final = deliver_application_access(
            self.successor, self.destination, self.master, self.transport, self.predecessor
        )
        self.assertEqual(other_content.status, "error")
        self.assertEqual(credential.read_text(), "existing-unrelated-credential\n")

        credential.write_text(old_token + "\n")
        credential.chmod(0o644)
        public: Final = deliver_application_access(
            self.successor, self.destination, self.master, self.transport, self.predecessor
        )
        self.assertEqual(public.status, "error")
        self.assertEqual(credential.read_text(), old_token + "\n")

    def test_rotation_cli_prepares_and_replaces_without_printing_a_credential(self) -> None:
        gateway: Final = KeyedGateway()
        transport: Final = httpx.MockTransport(gateway)
        with ThreadingHTTPServer(("127.0.0.1", 0), gateway_handler(gateway)) as server:
            thread: Final = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)
            served: Final = ApplicationAccessSpec.model_validate(
                SPEC.model_dump() | {"gateway_origin": f"http://127.0.0.1:{server.server_port}"}
            )
            predecessor: Final = self.predecessor.with_name("served")
            successor: Final = self.predecessor.with_name("served-next")
            prepare_application_access(served, predecessor)
            provision_application_access(predecessor, self.master, transport)
            deliver_application_access(predecessor, self.destination, self.master, transport)
            admin: Final = self.predecessor.with_name("admin.env")
            _ = admin.write_text("LITELLM_MASTER_KEY=inert-master\n")
            admin.chmod(0o600)

            def control(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, "-m", "cube_control", *arguments],
                    text=True,
                    capture_output=True,
                    timeout=20,
                    env={name: value for name, value in os.environ.items() if name != "CUBE_CONTROL_DATABASE_URL"},
                )

            prepared: Final = control(
                "prepare-application-rotation", "--state-directory", predecessor, "--successor-directory", successor
            )
            self.assertEqual(
                (prepared.returncode, '"status":"prepared"' in prepared.stdout), (0, True), prepared.stderr
            )
            provision_application_access(successor, self.master, transport)
            deliver: Final = ("deliver-application", "--state-directory", successor, "--config", admin)
            unreplaced: Final = control(*deliver, "--destination-directory", self.destination)
            self.assertEqual(unreplaced.returncode, 1)
            self.assertEqual((self.destination / "api-key").read_text(), stored_token(predecessor) + "\n")
            replaced: Final = control(
                *deliver, "--destination-directory", self.destination, "--replaces-state-directory", predecessor
            )
            self.assertEqual(
                (replaced.returncode, '"status":"delivered"' in replaced.stdout), (0, True), replaced.stderr
            )
            self.assertEqual((self.destination / "api-key").read_text(), stored_token(successor) + "\n")
            absent: Final = control(
                "prepare-application-rotation",
                "--state-directory",
                predecessor.with_name("absent"),
                "--successor-directory",
                predecessor.with_name("unused"),
            )
            self.assertEqual(absent.returncode, 1)
            printed: Final = "".join(run.stdout + run.stderr for run in (prepared, unreplaced, replaced, absent))
            for secret in (stored_token(predecessor), stored_token(successor), "inert-master"):
                self.assertNotIn(secret, printed)


class ApplicationAccessTests(unittest.TestCase):
    def test_revocation_blocks_owned_key_by_hash_and_disables_further_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            destination: Final = Path(root) / "consumer"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            result: Final = revoke_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.enforcement, "not_verified")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate", "/key/block"])
            assert gateway.key is not None
            self.assertNotIn(str(gateway.key["key"]), result.model_dump_json())
            again: Final = revoke_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            self.assertEqual(again, result)
            prepared: Final = prepare_application_access(SPEC, directory)
            self.assertEqual(prepared.status, "error")
            gateway.key["status"] = "active"
            gateway.key["blocked"] = False
            provision: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(provision.status, "error")
            delivered: Final = deliver_application_access(
                directory, destination, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(delivered.status, "error")
            self.assertFalse(destination.exists())
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate", "/key/block"])

    def test_revocation_timeout_after_commit_reconciles_without_repeated_block(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            gateway.timeout_path = "/key/block"
            first: Final = revoke_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            self.assertEqual(first.status, "uncertain")
            gateway.timeout_path = None
            after: Final = revoke_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            self.assertEqual(after.status, "blocked")
            self.assertEqual(after.enforcement, "not_verified")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate", "/key/block"])

    def test_revocation_unknown_outcome_never_repeats_a_block(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            gateway.fail_creation = True
            gateway.timeout_path = "/key/block"
            revoke_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            gateway.timeout_path = None
            result: Final = revoke_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(result.status, "uncertain")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate", "/key/block"])

    def test_revocation_requires_ownership_but_can_restrict_widened_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            provision_application_access(directory, SecretStr("inert-master"), httpx.MockTransport(gateway))
            assert gateway.key is not None
            gateway.key["user_id"] = "unrelated-owner"
            foreign: Final = revoke_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(foreign.status, "error")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate"])
            gateway.key["user_id"] = SPEC.user_id
            gateway.key["allowed_routes"] = ["/key/generate"]
            gateway.override_credentials = True
            gateway.custom_keys = False
            emergency: Final = revoke_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(emergency.status, "blocked")
            self.assertEqual(gateway.posts, ["/user/new", "/key/generate", "/key/block"])

    def test_revocation_records_intent_before_an_unavailable_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)

            def unavailable(request: httpx.Request) -> httpx.Response:
                raise httpx.ReadTimeout("inert-secret-marker", request=request)

            result: Final = revoke_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(unavailable)
            )
            self.assertEqual(result.status, "uncertain")
            self.assertNotIn("inert-secret-marker", result.model_dump_json())
            state: Final = TypeAdapter(dict[str, object]).validate_json((directory / "state.json").read_bytes())
            self.assertIs(state["revocation_requested"], True)
            self.assertIs(state["block_attempted"], False)
            blocked: Final = provision_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(unavailable)
            )
            self.assertEqual(blocked.status, "error")

    def test_missing_or_archived_keys_never_report_verified_enforcement(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "application"
            prepare_application_access(SPEC, directory)
            gateway: Final = GatewayFixture()
            missing: Final = revoke_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(missing.status, "uncertain")
            self.assertEqual(gateway.posts, [])
            state: Final = TypeAdapter(dict[str, object]).validate_json((directory / "state.json").read_bytes())
            gateway.key = {
                "user_id": SPEC.user_id,
                "team_id": SPEC.team_id,
                "metadata": {
                    "cube_application_id": SPEC.application_id,
                    "cube_access_operation": state["operation_id"],
                    "cube_access_manager": "cubellm-v1",
                },
                "status": "deleted",
            }
            deleted: Final = revoke_application_access(
                directory, SecretStr("inert-master"), httpx.MockTransport(gateway)
            )
            self.assertEqual(deleted.status, "deleted")
            self.assertEqual(deleted.enforcement, "not_verified")
            self.assertEqual(gateway.posts, [])

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

    def test_application_cli_delivers_read_only_then_records_revocation_without_secret_output(self) -> None:
        gateway: Final = GatewayFixture()

        Handler: Final = gateway_handler(gateway)

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
                revoked: Final = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "cube_control",
                        "revoke-application",
                        "--state-directory",
                        str(directory),
                        "--config",
                        str(admin),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=20,
                    env={name: value for name, value in os.environ.items() if name != "CUBE_CONTROL_DATABASE_URL"},
                )
                self.assertEqual(revoked.returncode, 0, revoked.stderr)
                self.assertIn('"status":"blocked"', revoked.stdout)
                self.assertIn('"enforcement":"not_verified"', revoked.stdout)
                self.assertNotIn(str(gateway.key["key"]), revoked.stdout + revoked.stderr)
                self.assertNotIn("inert-master", revoked.stdout + revoked.stderr)
                self.assertEqual(gateway.posts, ["/user/new", "/key/generate", "/key/block"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
