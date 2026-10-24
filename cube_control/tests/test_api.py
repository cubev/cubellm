import unittest
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from fastapi.testclient import TestClient

from cube_control.api import create_app
from cube_control.models import CATALOG, ImportBatch
from cube_control.store import ImportResult, StoredState


@dataclass(frozen=True)
class FixedRepository:
    available: bool = True

    def state(self) -> StoredState:
        return StoredState(CATALOG, (), (), ())

    def healthy(self) -> bool:
        return self.available

    def import_batch(self, batch: ImportBatch) -> ImportResult:
        return ImportResult(False, message="Read only fixture")


class ApiTests(unittest.TestCase):
    def test_all_provider_accounts_are_present_without_fabricated_values(self) -> None:
        client: Final[TestClient] = TestClient(create_app(FixedRepository()))
        response: Final = client.get("/api/v1/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["accounts"]), len(CATALOG))
        self.assertEqual(
            {a["data"]["account"]["provider"] for a in response.json()["accounts"]}, {a.provider.value for a in CATALOG}
        )
        self.assertTrue(all(a["data"]["selected_spend"] is None for a in response.json()["accounts"]))

    def test_api_has_no_mutating_http_routes_and_does_not_allow_framing(self) -> None:
        client: Final[TestClient] = TestClient(create_app(FixedRepository()))
        self.assertEqual(client.post("/api/v1/summary", json={}).status_code, 405)
        self.assertEqual(client.post("/api/v1/import", json={}).status_code, 404)
        response: Final = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_health_fails_when_database_is_unavailable(self) -> None:
        client: Final[TestClient] = TestClient(create_app(FixedRepository(available=False)))
        self.assertEqual(client.get("/healthz").status_code, 503)

    def test_summary_uses_injected_reporting_clock(self) -> None:
        instant: Final[datetime] = datetime(2027, 2, 1, tzinfo=UTC)
        client: Final[TestClient] = TestClient(create_app(FixedRepository(), lambda: instant))
        self.assertEqual(client.get("/api/v1/summary").json()["period"], instant.strftime("%Y-%m"))
