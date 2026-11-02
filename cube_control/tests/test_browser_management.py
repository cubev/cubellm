import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Final
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr, TypeAdapter

from cube_control.api import CapabilitiesResponse, create_app
from cube_control.browser_session import FAILED_LOGIN_LIMIT, MAX_SESSIONS, SESSION_LIFETIME, private_authority
from cube_control.credential_binding import CredentialBindingCommand
from cube_control.models import CATALOG, ImportBatch, ManualCommand
from cube_control.store import ImportResult, ManualResult, PostgresRepository, Repository, StoredState

ORIGIN: Final = "http://127.0.0.1:4001"
TOKEN: Final = "synthetic-management-token-" + "x" * 16
OPERATOR: Final = "local-owner"
NOW: Final = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
DSN: Final = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")
SESSION: Final = TypeAdapter(dict[str, str])
SOCKET_ADDRESS: Final = TypeAdapter(tuple[str, int])
MANAGEMENT_ENVIRONMENT: Final = frozenset(
    ("CUBE_CONTROL_ADMIN_TOKEN_FILE", "CUBE_CONTROL_MANAGEMENT_ORIGIN", "CUBE_CONTROL_OPERATOR")
)


class CommandReply(BaseModel):
    status: str
    version: int
    message: str


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


class RecordingRepository:
    def __init__(self) -> None:
        self.commands: tuple[tuple[ManualCommand, str], ...] = ()

    def state(self) -> StoredState:
        return StoredState(CATALOG, (), (), ())

    def healthy(self) -> bool:
        return True

    def import_batch(self, batch: ImportBatch) -> ImportResult:
        return ImportResult(False, message="Read only fixture")

    def manage(self, command: ManualCommand, actor: str, now: datetime) -> ManualResult:
        self.commands = (*self.commands, (command, actor))
        return ManualResult("applied", len(self.commands))

    def bind_credential(self, command: CredentialBindingCommand, actor: str, now: datetime) -> ManualResult:
        return ManualResult("rejected", message="Read only fixture")


def authority(origin: str = ORIGIN):
    configured: Final = private_authority(SecretStr(TOKEN), origin, OPERATOR)
    assert configured is not None
    return configured


def browser(repository: Repository, clock: Clock, origin: str = ORIGIN) -> TestClient:
    return TestClient(create_app(repository, clock=clock, authority=authority(origin)), base_url=origin)


def headers(csrf: str = "", origin: str = ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "Content-Type": "application/json", **({"X-CSRF-Token": csrf} if csrf else {})}


def login_status(client: TestClient, token: str = TOKEN, origin: str = ORIGIN) -> int:
    return client.post(
        "/api/v1/session", content=json.dumps({"token": token}), headers=headers(origin=origin)
    ).status_code


def csrf_after_login(client: TestClient) -> str:
    response: Final = client.post("/api/v1/session", content=json.dumps({"token": TOKEN}), headers=headers())
    assert response.status_code == 200, response.text
    return SESSION.validate_json(response.content)["csrf_token"]


def current_csrf(client: TestClient) -> str:
    return SESSION.validate_json(client.get("/api/v1/session").content)["csrf_token"]


def command_reply(client: TestClient, path: str, body: dict[str, object], csrf: str) -> tuple[int, CommandReply]:
    response: Final = client.post(path, json=body, headers=headers(csrf))
    return response.status_code, CommandReply.model_validate_json(response.content)


def edit(account_id: str = "openai-api", command_id: str = "browser-edit-1") -> dict[str, object]:
    return {
        "command_id": command_id,
        "account_id": account_id,
        "expected_version": 0,
        "changes": [{"field": "note", "action": "set", "value": "kept"}],
    }


class BrowserManagementTests(unittest.TestCase):
    def test_management_stays_read_only_unless_configured(self) -> None:
        client: Final = TestClient(create_app(RecordingRepository()), base_url=ORIGIN)
        self.assertEqual(client.get("/api/v1/capabilities").json()["browser_management"], "disabled")
        self.assertEqual(
            [
                client.get("/api/v1/session").status_code,
                client.post("/api/v1/session", json={"token": TOKEN}, headers=headers()).status_code,
                client.post("/api/v1/accounts/openai-api/commands", json=edit(), headers=headers()).status_code,
            ],
            [404, 404, 404],
        )

    def test_invalid_management_configuration_fails_closed(self) -> None:
        self.assertEqual(
            [
                private_authority(SecretStr(token), origin, operator) is None
                for origin, operator, token in (
                    ("http://192.168.1.10:4001", OPERATOR, TOKEN),
                    ("http://127.0.0.1:4001/path", OPERATOR, TOKEN),
                    ("ftp://127.0.0.1:4001", OPERATOR, TOKEN),
                    ("https://user@control.example", OPERATOR, TOKEN),
                    ("http://127.0.0.1:99999", OPERATOR, TOKEN),
                    ("http://127.0.0.1:port", OPERATOR, TOKEN),
                    ("http://[::1", OPERATOR, TOKEN),
                    ("https://", OPERATOR, TOKEN),
                    ("", OPERATOR, TOKEN),
                    (ORIGIN, "", TOKEN),
                    (ORIGIN, "bad operator", TOKEN),
                    (ORIGIN, OPERATOR, "short"),
                )
            ],
            [True] * 12,
        )
        self.assertIsNotNone(private_authority(SecretStr(TOKEN), "https://control.example", OPERATOR))
        self.assertIsNotNone(private_authority(SecretStr(TOKEN), "http://localhost:4001", OPERATOR))

    def test_login_requires_the_configured_origin_and_json_and_never_echoes_input(self) -> None:
        client: Final = browser(RecordingRepository(), Clock())
        rejected: Final = [
            client.post("/api/v1/session", content=json.dumps({"token": TOKEN}), headers=value)
            for value in (
                headers(origin="http://evil.example"),
                {"Content-Type": "application/json"},
                {**headers(), "Sec-Fetch-Site": "cross-site"},
                {**headers(), "Host": "evil.example"},
                {"Origin": ORIGIN, "Content-Type": "text/plain"},
            )
        ]
        self.assertEqual([response.status_code for response in rejected], [403, 403, 403, 403, 415])
        self.assertTrue(all("set-cookie" not in response.headers for response in rejected))
        wrong: Final = client.post(
            "/api/v1/session", content=json.dumps({"token": "wrong-synthetic-token-value"}), headers=headers()
        )
        self.assertEqual((wrong.status_code, "wrong-synthetic" in wrong.text), (401, False))
        malformed: Final = client.post("/api/v1/session", content=b'{"token": 7, "x": "echo-me"}', headers=headers())
        self.assertEqual((malformed.status_code, "echo-me" in malformed.text), (422, False))

    def test_a_session_cookie_is_http_only_strict_and_secure_only_for_https_origins(self) -> None:
        def cookie_flags(origin: str) -> tuple[int, bool, bool, bool, bool, bool, str]:
            response: Final = browser(RecordingRepository(), Clock(), origin).post(
                "/api/v1/session", content=json.dumps({"token": TOKEN}), headers=headers(origin=origin)
            )
            cookie: Final = response.headers.get("set-cookie", "")
            return (
                response.status_code,
                "HttpOnly" in cookie,
                "SameSite=strict" in cookie,
                "Path=/api/v1" in cookie,
                "Secure" in cookie,
                TOKEN in response.text + cookie,
                SESSION.validate_json(response.content)["actor"],
            )

        self.assertEqual(
            [cookie_flags(ORIGIN), cookie_flags("https://control.example")],
            [(200, True, True, True, False, False, OPERATOR), (200, True, True, True, True, False, OPERATOR)],
        )

    def test_repeated_failed_logins_lock_even_the_right_token_and_sessions_are_bounded(self) -> None:
        locked: Final = browser(RecordingRepository(), Clock())
        failures: Final = [login_status(locked, f"wrong-{index}") for index in range(FAILED_LOGIN_LIMIT)]
        self.assertEqual((failures, login_status(locked)), ([401] * FAILED_LOGIN_LIMIT, 429))
        crowded: Final = browser(RecordingRepository(), Clock())
        statuses: Final = [login_status(crowded) for _ in range(MAX_SESSIONS + 1)]
        self.assertEqual(statuses, [200] * MAX_SESSIONS + [503])

    def test_commands_require_session_csrf_and_origin_and_apply_as_the_configured_operator(self) -> None:
        repository: Final = RecordingRepository()
        client: Final = browser(repository, Clock())
        path: Final = "/api/v1/accounts/openai-api/commands"
        anonymous: Final = client.post(path, json=edit(), headers=headers("anything"))
        csrf: Final = csrf_after_login(client)
        statuses: Final = [
            anonymous.status_code,
            client.post(path, json=edit(), headers=headers()).status_code,
            client.post(path, json=edit(), headers=headers("x" * 43)).status_code,
            client.post(path, json=edit(), headers=headers(csrf, origin="http://evil.example")).status_code,
            client.post(path, json=edit(), headers={**headers(csrf), "Sec-Fetch-Site": "same-site"}).status_code,
            client.post("/api/v1/accounts/other/commands", json=edit(), headers=headers(csrf)).status_code,
        ]
        self.assertEqual(statuses, [401, 403, 403, 403, 403, 422])
        self.assertEqual(repository.commands, ())
        status, reply = command_reply(client, path, edit(), csrf)
        self.assertEqual((status, reply.status), (200, "applied"))
        self.assertEqual([actor for _, actor in repository.commands], [OPERATOR])

    def test_tampered_and_malformed_cookies_are_rejected_without_errors(self) -> None:
        client: Final = browser(RecordingRepository(), Clock())
        csrf: Final = csrf_after_login(client)
        for cookie in ("x" * 43, "9" * 400, "", "1.2.3", "a" * 10_000, "-" * 44):
            with self.subTest(cookie=cookie[:12]):
                client.cookies.set("cube_control_session", cookie, path="/api/v1")
                self.assertEqual(
                    client.post("/api/v1/accounts/openai-api/commands", json=edit(), headers=headers(csrf)).status_code,
                    401,
                )

    def test_logout_revokes_a_copied_cookie_and_sessions_expire(self) -> None:
        clock: Final = Clock()
        client: Final = browser(RecordingRepository(), clock)
        csrf: Final = csrf_after_login(client)
        copied: Final = client.cookies.get("cube_control_session")
        assert copied is not None
        self.assertEqual(client.request("DELETE", "/api/v1/session", headers=headers(csrf)).status_code, 200)
        client.cookies.set("cube_control_session", copied, path="/api/v1")
        self.assertEqual(client.get("/api/v1/session").status_code, 401)
        self.assertEqual(
            client.post("/api/v1/accounts/openai-api/commands", json=edit(), headers=headers(csrf)).status_code, 401
        )
        client.cookies.clear()
        fresh: Final = csrf_after_login(client)
        self.assertEqual(client.get("/api/v1/session").status_code, 200)
        clock.now = NOW + SESSION_LIFETIME
        self.assertEqual(
            client.post("/api/v1/accounts/openai-api/commands", json=edit(), headers=headers(fresh)).status_code, 401
        )

    def test_oversized_and_invalid_bodies_are_rejected_without_echo(self) -> None:
        repository: Final = RecordingRepository()
        client: Final = browser(repository, Clock())
        csrf: Final = csrf_after_login(client)
        path: Final = "/api/v1/accounts/openai-api/commands"

        def streamed() -> Iterator[bytes]:
            yield b'{"command_id":"'
            yield from (b"a" * 8192 for _ in range(16))

        declared: Final = client.post(path, content=b"{}", headers={**headers(csrf), "Content-Length": "999999999"})
        chunked: Final = client.post(path, content=streamed(), headers=headers(csrf))
        invalid: Final = client.post(path, content=b'{"echo-me": true}', headers=headers(csrf))
        self.assertEqual(
            [declared.status_code, chunked.status_code, invalid.status_code, "echo-me" in invalid.text],
            [413, 413, 422, False],
        )
        self.assertEqual(repository.commands, ())


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class BrowserManagementStoreTests(unittest.TestCase):
    def test_a_committed_enrollment_retried_after_signing_in_again_is_a_duplicate(self) -> None:
        repository: Final = PostgresRepository(DSN)
        with repository.connect() as connection:
            database: Final = connection.execute("SELECT current_database()").fetchone()
        if database is None or database[0] != "cubecontrol_test":
            raise RuntimeError("Integration tests require the dedicated cubecontrol_test database")
        repository.initialize()
        account: Final = f"browser-{uuid4().hex}"

        def forget() -> None:
            with repository.connect() as connection:
                connection.execute("DELETE FROM cube_control.manual_commands WHERE account_id=%s", (account,))
                connection.execute("DELETE FROM cube_control.accounts WHERE id=%s", (account,))

        self.addCleanup(forget)
        enrollment: Final[dict[str, object]] = {
            "command_id": f"enroll-{account}",
            "account_id": account,
            "expected_version": 0,
            "create_account": {"id": account, "provider": "custom-media-api", "name": "Media API", "kind": "api"},
        }
        path: Final = f"/api/v1/accounts/{account}/commands"
        with browser(repository, Clock(datetime.now(UTC))) as client:
            first: Final = command_reply(client, path, enrollment, csrf_after_login(client))
            client.request("DELETE", "/api/v1/session", headers=headers(current_csrf(client)))
            retried: Final = command_reply(client, path, enrollment, csrf_after_login(client))
            stale: Final = command_reply(client, path, edit(account, f"edit-{account}"), current_csrf(client))
        self.assertEqual(
            [(status, reply.status, reply.version) for status, reply in (first, retried, stale)],
            [(200, "applied", 1), (200, "duplicate", 1), (409, "conflict", 1)],
        )
        entries: Final = [entry for entry in repository.state().manual_entries if entry.command.account_id == account]
        self.assertEqual([(entry.revision, entry.actor) for entry in entries], [(1, OPERATOR)])
        self.assertIn(account, [item.id for item in repository.state().accounts])


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        _, port = SOCKET_ADDRESS.validate_python(probe.getsockname())
        return port


def serve(environment: dict[str, str], port: int, log: Path) -> subprocess.Popen[bytes]:
    with log.open("w") as output:
        return subprocess.Popen(
            [sys.executable, "-m", "cube_control", "serve", "--host", "127.0.0.1", "--port", str(port)],
            env={
                **{name: value for name, value in os.environ.items() if name not in MANAGEMENT_ENVIRONMENT},
                "CUBE_CONTROL_DATABASE_URL": DSN,
                **environment,
            },
            stdout=output,
            stderr=subprocess.STDOUT,
        )


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def ready(client: httpx.Client, process: subprocess.Popen[bytes]) -> None:
    deadline: Final = monotonic() + 60
    while monotonic() < deadline:
        assert process.poll() is None, "controller exited before it became ready"
        try:
            if client.get("/healthz").status_code == 200:
                return
        except httpx.TransportError:
            pass
        sleep(0.1)
    raise AssertionError("controller did not become ready")


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class BrowserManagementServeTests(unittest.TestCase):
    def setUp(self) -> None:
        repository: Final = PostgresRepository(DSN)
        with repository.connect() as connection:
            database: Final = connection.execute("SELECT current_database()").fetchone()
        if database is None or database[0] != "cubecontrol_test":
            raise RuntimeError("Integration tests require the dedicated cubecontrol_test database")
        self.repository = repository
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.token_file = self.root / "control.env"
        self.token_file.write_text(f"CUBE_CONTROL_ADMIN_TOKEN={TOKEN}\n")
        self.token_file.chmod(0o600)

    def test_serve_enables_session_management_only_from_a_private_token_and_configured_origin(self) -> None:
        port: Final = free_port()
        origin: Final = f"http://127.0.0.1:{port}"
        account: Final = f"serve-{uuid4().hex}"

        def forget() -> None:
            with self.repository.connect() as connection:
                connection.execute("DELETE FROM cube_control.manual_commands WHERE account_id=%s", (account,))
                connection.execute("DELETE FROM cube_control.accounts WHERE id=%s", (account,))

        self.addCleanup(forget)
        process: Final = serve(
            {
                "CUBE_CONTROL_ADMIN_TOKEN_FILE": str(self.token_file),
                "CUBE_CONTROL_MANAGEMENT_ORIGIN": origin,
                "CUBE_CONTROL_OPERATOR": "smoke-operator",
            },
            port,
            self.root / "serve.log",
        )
        self.addCleanup(stop, process)
        with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
            ready(client, process)
            capability: Final = CapabilitiesResponse.model_validate_json(client.get("/api/v1/capabilities").content)
            login: Final = client.post(
                "/api/v1/session", content=json.dumps({"token": TOKEN}), headers=headers(origin=origin)
            )
            csrf: Final = SESSION.validate_json(login.content)["csrf_token"]
            enrolled: Final = client.post(
                f"/api/v1/accounts/{account}/commands",
                json={
                    "command_id": f"enroll-{account}",
                    "account_id": account,
                    "expected_version": 0,
                    "create_account": {"id": account, "provider": "custom-api", "name": "Serve smoke", "kind": "api"},
                },
                headers=headers(csrf, origin=origin),
            )
        stop(process)
        self.assertEqual(capability.browser_management, "session_v1")
        self.assertEqual((login.status_code, enrolled.status_code), (200, 200), enrolled.text)
        self.assertEqual(
            [entry.actor for entry in self.repository.state().manual_entries if entry.command.account_id == account],
            ["smoke-operator"],
        )
        self.assertNotIn(TOKEN, (self.root / "serve.log").read_text())

    def test_serve_refuses_to_start_with_unsafe_management_configuration(self) -> None:
        shared: Final = self.root / "shared.env"
        shared.write_text(f"CUBE_CONTROL_ADMIN_TOKEN={TOKEN}\n")
        shared.chmod(0o640)
        port: Final = free_port()
        cases: Final = (
            {
                "CUBE_CONTROL_ADMIN_TOKEN_FILE": str(shared),
                "CUBE_CONTROL_MANAGEMENT_ORIGIN": f"http://127.0.0.1:{port}",
            },
            {"CUBE_CONTROL_ADMIN_TOKEN_FILE": str(self.token_file)},
            {
                "CUBE_CONTROL_ADMIN_TOKEN_FILE": str(self.token_file),
                "CUBE_CONTROL_MANAGEMENT_ORIGIN": "http://192.168.1.10:4001",
            },
        )

        def refused(index: int, environment: dict[str, str]) -> tuple[int, bool]:
            log: Final = self.root / f"refused-{index}.log"
            process: Final = serve(environment, port, log)
            try:
                code: Final = process.wait(timeout=60)
            finally:
                if process.poll() is None:
                    stop(process)
            return code, TOKEN in log.read_text()

        self.assertEqual(
            [refused(index, environment) for index, environment in enumerate(cases)], [(2, False)] * len(cases)
        )

    def test_serve_without_a_token_file_stays_read_only(self) -> None:
        port: Final = free_port()
        origin: Final = f"http://127.0.0.1:{port}"
        process: Final = serve({}, port, self.root / "read-only.log")
        self.addCleanup(stop, process)
        with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
            ready(client, process)
            self.assertEqual(
                [
                    client.get("/api/v1/session").status_code,
                    client.post(
                        "/api/v1/session", content=json.dumps({"token": TOKEN}), headers=headers(origin=origin)
                    ).status_code,
                ],
                [404, 404],
            )
