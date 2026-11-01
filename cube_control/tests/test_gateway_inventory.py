import json
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

import httpx
from inventory_fixtures import bound, gateway_inventory
from pydantic import SecretStr, TypeAdapter

from cube_control.gateway_inventory import CLOCK_SKEW, GatewayInventory, collect_gateway_inventory

ORIGIN: Final = "http://127.0.0.1:4000"
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
