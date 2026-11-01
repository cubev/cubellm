import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
from inventory_fixtures import bound, gateway_inventory
from pydantic import SecretStr, TypeAdapter

from fastapi.testclient import TestClient

from cube_control.api import create_app
from cube_control.gateway_inventory import (
    CLOCK_SKEW,
    GatewayInventory,
    GatewayInventoryAttempt,
    GatewayInventorySnapshot,
    collect_and_record_gateway_inventory,
    collect_gateway_inventory,
)
from cube_control.inventory import AccountInventoryReport
from cube_control.store import PostgresRepository

ORIGIN: Final = "http://127.0.0.1:4000"
DSN: Final = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")
MASTER_KEY: Final = "sk-master-canary"
NOW: Final = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
CAPABILITIES: Final = {"account_inventory": "gateway_bindings_v1"}
JSON_OBJECT: Final = TypeAdapter(dict[str, object])
SOURCES: Final = TypeAdapter(list[dict[str, object]])


def gateway(inventory: object, capabilities: object = CAPABILITIES) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {MASTER_KEY}"
        if request.url.path == "/cube_control/capabilities":
            return httpx.Response(200, json=capabilities)
        assert request.url.path == "/cube_control/account_inventory", request.url.path
        return httpx.Response(200, content=inventory if isinstance(inventory, bytes) else json.dumps(inventory))

    return handler


def payload(inventory: GatewayInventory) -> dict[str, object]:
    return JSON_OBJECT.validate_json(inventory.model_dump_json())


def collect(handler: Callable[[httpx.Request], httpx.Response], origin: str = ORIGIN):
    return collect_gateway_inventory(origin, SecretStr(MASTER_KEY), NOW, httpx.MockTransport(handler))


class GatewayInventoryCollectionTests(unittest.TestCase):
    def test_a_consistent_inventory_is_kept_with_its_gateway_and_collection_time(self) -> None:
        inventory: Final = gateway_inventory(NOW - timedelta(seconds=3), credentials=(bound("runtime", "c", "a"),))
        result: Final = collect(gateway(payload(inventory)))
        self.assertEqual((result.status, result.snapshot and result.snapshot.inventory), ("collected", inventory))
        assert result.snapshot is not None
        self.assertEqual((result.snapshot.gateway_origin, result.snapshot.checked_at), (ORIGIN, NOW))

    def test_gateways_without_the_inventory_capability_are_rejected(self) -> None:
        result: Final = collect(gateway(payload(gateway_inventory(NOW)), capabilities={}))
        self.assertEqual((result.status, result.snapshot), ("error", None))

    def test_inconsistent_source_reports_are_rejected(self) -> None:
        consistent: Final = payload(gateway_inventory(NOW, credentials=(bound("runtime", "c", "a"),)))
        sources: Final = SOURCES.validate_python(consistent["sources"])
        variants: Final[tuple[dict[str, object], ...]] = (
            {**consistent, "credentials": []},
            {**consistent, "sources": sources[1:]},
            {**consistent, "sources": [*sources[1:], sources[1]]},
            {**consistent, "sources": [*sources, sources[0]]},
            {**consistent, "sources": [sources[0], {**sources[1], "status": "unavailable"}, *sources[2:]]},
            {**consistent, "sources": [{**sources[0], "status": "partial"}, *sources[1:]]},
        )
        self.assertEqual(
            [(result.status, result.message) for result in (collect(gateway(item)) for item in variants)],
            [("error", "Gateway account inventory is inconsistent")] * len(variants),
        )

    def test_inventory_time_ahead_of_the_collector_beyond_the_clock_skew_is_rejected(self) -> None:
        within: Final = collect(gateway(payload(gateway_inventory(NOW + CLOCK_SKEW))))
        ahead: Final = collect(gateway(payload(gateway_inventory(NOW + CLOCK_SKEW + timedelta(seconds=1)))))
        self.assertEqual((within.status, ahead.status, ahead.snapshot), ("collected", "error", None))

    def test_oversized_responses_become_a_sanitised_error(self) -> None:
        result: Final = collect(gateway(b"x" * 1_000_001))
        self.assertEqual((result.status, result.message), ("error", "Gateway response exceeds size limit"))

    def test_transport_failures_are_uncertain_and_never_echo_the_master_key(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"refused {request.headers['Authorization']}")

        result: Final = collect(refuse)
        self.assertEqual(result.status, "uncertain")
        self.assertNotIn(MASTER_KEY, result.model_dump_json())

    def test_non_loopback_gateways_are_never_contacted(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"contacted {request.url}")

        result: Final = collect(unreachable, origin="http://gateway.example:4000")
        self.assertEqual((result.status, result.message), ("error", "Gateway origin rejected"))


class RecordedCollections:
    def __init__(self) -> None:
        self.calls: tuple[tuple[str, GatewayInventoryAttempt, GatewayInventorySnapshot | None], ...] = ()

    def record_gateway_inventory(
        self, gateway_origin: str, attempt: GatewayInventoryAttempt, snapshot: GatewayInventorySnapshot | None
    ) -> None:
        self.calls = (*self.calls, (gateway_origin, attempt, snapshot))


class GatewayInventoryRecordingTests(unittest.TestCase):
    def test_every_attempt_is_recorded_under_the_canonical_gateway_origin(self) -> None:
        recorder: Final = RecordedCollections()
        inventory: Final = gateway_inventory(NOW)
        collect_and_record_gateway_inventory(
            recorder, ORIGIN + "/", SecretStr(MASTER_KEY), NOW, httpx.MockTransport(gateway(payload(inventory)))
        )
        collect_and_record_gateway_inventory(
            recorder,
            ORIGIN,
            SecretStr(MASTER_KEY),
            NOW,
            httpx.MockTransport(gateway(payload(inventory), capabilities={})),
        )
        self.assertEqual(
            [
                (origin, attempt.status, snapshot.gateway_origin if snapshot else None)
                for origin, attempt, snapshot in recorder.calls
            ],
            [(ORIGIN, "collected", ORIGIN), (ORIGIN, "error", None)],
        )

    def test_rejected_origins_are_neither_contacted_nor_recorded(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"contacted {request.url}")

        recorder: Final = RecordedCollections()
        result: Final = collect_and_record_gateway_inventory(
            recorder, "http://gateway.example:4000", SecretStr(MASTER_KEY), NOW, httpx.MockTransport(unreachable)
        )
        self.assertEqual((result.status, recorder.calls), ("error", ()))


class OwnedGateway(BaseHTTPRequestHandler):
    inventory: bytes | None = None
    requests: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()

    def reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        handler: Final = type(self)
        handler.requests = (*handler.requests, self.path)
        if self.headers.get("Authorization") != f"Bearer {MASTER_KEY}":
            handler.rejected = (*handler.rejected, self.path)
            self.reply(401, b'{"error":"unauthorized"}')
        elif self.path == "/cube_control/capabilities":
            self.reply(200, json.dumps(CAPABILITIES).encode())
        elif self.path == "/cube_control/account_inventory" and self.inventory is not None:
            self.reply(200, self.inventory)
        elif self.path == "/cube_control/account_inventory":
            self.reply(500, b'{"error":"unavailable"}')
        else:
            self.reply(404, b'{"error":"not found"}')

    def log_message(self, format: str, *args: object) -> None:
        return


def owned_gateway(cleanup: ExitStack, inventory: bytes | None) -> tuple[type[OwnedGateway], str]:
    handler: Final = type("Gateway", (OwnedGateway,), {"inventory": inventory, "requests": (), "rejected": ()})
    server: Final = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), handler))
    thread: Final = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cleanup.callback(thread.join)
    cleanup.callback(server.shutdown)
    return handler, f"http://127.0.0.1:{server.server_port}"


def private_config(cleanup: ExitStack, mode: int = 0o600) -> Path:
    admin: Final = Path(cleanup.enter_context(tempfile.TemporaryDirectory())) / "admin.env"
    admin.write_text(f"LITELLM_MASTER_KEY={MASTER_KEY}\n")
    admin.chmod(mode)
    return admin


def collect_cli(admin: Path, origin: str, dsn: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "cube_control",
            "collect-gateway-inventory",
            "--config",
            str(admin),
            "--gateway-origin",
            origin,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env={**os.environ, "CUBE_CONTROL_DATABASE_URL": dsn},
    )


UNREACHABLE_DSN: Final = "dbname=cubecontrol_test host=/nonexistent-cubellm-socket-directory connect_timeout=2"


def current_inventory() -> bytes:
    return gateway_inventory(datetime.now(UTC) - timedelta(seconds=1)).model_dump_json().encode()


class GatewayInventoryCliFailureTests(unittest.TestCase):
    def test_a_shared_private_config_stops_before_any_gateway_request(self) -> None:
        with ExitStack() as cleanup:
            handler, origin = owned_gateway(cleanup, current_inventory())
            result: Final = collect_cli(private_config(cleanup, mode=0o640), origin, UNREACHABLE_DSN)
        self.assertEqual((result.returncode, result.stdout, handler.requests), (2, "", ()))
        self.assertNotIn(MASTER_KEY, result.stderr)

    def test_a_rejected_origin_is_neither_contacted_nor_recorded(self) -> None:
        with ExitStack() as cleanup:
            handler, origin = owned_gateway(cleanup, current_inventory())
            result: Final = collect_cli(
                private_config(cleanup), origin.replace("127.0.0.1", "localhost"), UNREACHABLE_DSN
            )
        self.assertEqual(
            (result.returncode, json.loads(result.stdout)["message"], result.stderr, handler.requests),
            (1, "Gateway origin rejected", "", ()),
        )

    def test_a_store_failure_never_reports_a_collected_inventory(self) -> None:
        with ExitStack() as cleanup:
            handler, origin = owned_gateway(cleanup, current_inventory())
            result: Final = collect_cli(private_config(cleanup), origin, UNREACHABLE_DSN)
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr, handler.rejected),
            (1, "", "Inventory store unavailable\n", ()),
        )
        self.assertEqual(handler.requests, ("/cube_control/capabilities", "/cube_control/account_inventory"))


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class GatewayInventoryWorkflowTests(unittest.TestCase):
    def test_cli_collections_persist_per_gateway_and_the_api_reports_the_failed_retry(self) -> None:
        repository: Final = PostgresRepository(DSN)
        with repository.connect() as connection:
            database: Final = connection.execute("SELECT current_database()").fetchone()
        if database is None or database[0] != "cubecontrol_test":
            raise RuntimeError("Integration tests require the dedicated cubecontrol_test database")
        repository.initialize()

        def forget(origin: str) -> None:
            with repository.connect() as connection:
                connection.execute("DELETE FROM cube_control.gateway_inventories WHERE gateway_origin=%s", (origin,))

        observed_at: Final = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=1)
        with ExitStack() as cleanup:
            handler, origin = owned_gateway(
                cleanup,
                gateway_inventory(observed_at, credentials=(bound("runtime", "kept", "workflow-account"),))
                .model_dump_json()
                .encode(),
            )
            cleanup.callback(forget, origin)
            admin: Final = private_config(cleanup)
            first: Final = collect_cli(admin, origin, DSN)
            handler.inventory = None
            second: Final = collect_cli(admin, origin, DSN)
            client: Final = cleanup.enter_context(TestClient(create_app(repository)))
            response: Final = client.get("/api/v1/inventory")
        self.assertEqual(handler.rejected, ())
        self.assertEqual(
            [(result.returncode, json.loads(result.stdout)["status"]) for result in (first, second)],
            [(0, "collected"), (1, "error")],
            first.stderr + second.stderr,
        )
        self.assertNotIn(MASTER_KEY, first.stdout + first.stderr + second.stdout + second.stderr)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(MASTER_KEY, response.text)
        report: Final = AccountInventoryReport.model_validate_json(response.content)
        self.assertEqual(
            [
                (item.state, item.last_attempt_status, item.last_good_observed_at)
                for item in report.gateways
                if item.gateway_origin == origin
            ],
            [("last_attempt_failed", "error", observed_at)],
        )
        account: Final = next(item for item in report.accounts if item.account_id == "workflow-account")
        self.assertIn(
            (origin, "kept"), [(item.gateway_origin, item.credential.credential_name) for item in account.credentials]
        )
