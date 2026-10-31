import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from fastapi.testclient import TestClient

from cube_control.api import create_app
from cube_control.models import (
    CATALOG,
    Account,
    GatewayRecord,
    GatewayScan,
    GatewayScanView,
    ImportBatch,
    Observation,
    Provider,
)
from cube_control.store import ImportResult, StoredState


@dataclass(frozen=True)
class FixedRepository:
    available: bool = True
    snapshot: StoredState | None = None

    def state(self) -> StoredState:
        return self.snapshot if self.snapshot is not None else StoredState(CATALOG, (), (), ())

    def healthy(self) -> bool:
        return self.available

    def import_batch(self, batch: ImportBatch) -> ImportResult:
        return ImportResult(False, message="Read only fixture")


class ApiTests(unittest.TestCase):
    def test_explicit_account_attribution_is_never_copied_to_other_provider_accounts(self) -> None:
        now: Final = datetime(2026, 1, 5, tzinfo=UTC)
        accounts: Final = tuple(
            Account(id=f"account-{index}", provider=Provider.OPENROUTER, name=f"Account {index}", kind="api")
            for index in range(2)
        )
        assigned: Final = GatewayRecord(
            source_key="assigned",
            provider=Provider.OPENROUTER,
            account_id=accounts[0].id,
            model="test",
            occurred_at=now,
            status="succeeded",
            amount_usd=Decimal("0.30"),
        )
        unassigned: Final = assigned.model_copy(update={"source_key": "unassigned", "account_id": None})
        state: Final = StoredState(accounts, (), (), (), gateway_records=(assigned, unassigned))
        client: Final = TestClient(create_app(FixedRepository(snapshot=state), clock=lambda: now))
        rows: Final = client.get("/api/v1/summary").json()["accounts"]
        self.assertEqual(rows[0]["data"]["gateway"]["calls"], 1)
        self.assertEqual(rows[0]["data"]["gateway"]["reported_usd"], "0.30")
        self.assertIsNone(rows[1]["data"]["gateway"])
        self.assertTrue(all(row["data"]["selected_spend"] is None for row in rows))

    def test_summary_excludes_outage_masked_spend_until_provider_recovers(self) -> None:
        now: Final = datetime(2026, 1, 5, tzinfo=UTC)
        account: Final = Account(id="example", provider=Provider.OPENAI, name="Example", kind="api")
        report: Final = Observation(
            account_id=account.id,
            external_id="report",
            metric="spend",
            window="month",
            period=now.strftime("%Y-%m"),
            status="known",
            source="provider_api",
            value="12.50",
            unit="USD",
            observed_at=now - timedelta(seconds=10),
            collected_at=now,
        )
        outage: Final = report.model_copy(
            update={"external_id": "outage", "window": "all", "status": "error", "value": None, "observed_at": now}
        )
        recovered: Final = report.model_copy(
            update={"external_id": "recovered", "observed_at": now + timedelta(seconds=1)}
        )
        for observations, expected in (((report, outage), None), ((report, outage, recovered), recovered)):
            with self.subTest(recovered=expected is not None):
                state: Final = StoredState((account,), observations, (), ())
                client: Final = TestClient(create_app(FixedRepository(snapshot=state), clock=lambda: now))
                response: Final = client.get("/api/v1/summary")
                self.assertEqual(response.status_code, 200)
                item: Final = response.json()["accounts"][0]
                self.assertEqual(item["data"]["selected_spend"], expected.model_dump(mode="json") if expected else None)
                self.assertEqual(
                    [view["reading"] for view in item["readings"] if view["current"]],
                    [expected.model_dump(mode="json")] if expected else [],
                )

    def test_gateway_totals_never_imply_billing_account_identity(self) -> None:
        now: Final = datetime(2026, 1, 5, tzinfo=UTC)
        record: Final = GatewayRecord(
            source_key="request", provider=Provider.OPENROUTER, model="test", occurred_at=now, status="succeeded"
        )
        scan: Final = GatewayScanView(
            sequence=1,
            scan=GatewayScan(
                scan_id="scan",
                source_id="source",
                expected_sequence=0,
                checked_at=now,
                state="ok",
                available_rows=1,
                scanned_rows=1,
            ),
        )
        for count in (1, 2):
            accounts: Final = tuple(
                Account(id=f"account-{index}", provider=Provider.OPENROUTER, name=f"Account {index}", kind="api")
                for index in range(count)
            )
            state: Final = StoredState(accounts, (), (), (), gateway_records=(record,), gateway_scans=(scan,))
            client: Final = TestClient(create_app(FixedRepository(snapshot=state), clock=lambda: now))
            data: Final = client.get("/api/v1/summary").json()
            self.assertTrue(all(row["data"]["gateway"] is None for row in data["accounts"]))
            self.assertEqual(len(data["gateway_providers"]), 1)
            self.assertEqual(data["gateway_providers"][0]["provider"], record.provider)
            self.assertEqual(data["gateway_providers"][0]["metrics"]["calls"], 1)
            self.assertTrue(all(row["data"]["selected_spend"] is None for row in data["accounts"]))

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

    def test_capabilities_advertise_supported_gateway_projection_without_writes(self) -> None:
        client: Final = TestClient(create_app(FixedRepository()))
        response: Final = client.get("/api/v1/capabilities")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schema_version"], 1)
        self.assertEqual(response.json()["gateway_native_account_projection"], "credential_snapshot_v1")
        self.assertTrue(
            {"application_id", "deployment_id", "call_type", "completed_at", "duration_ms"}.issubset(
                response.json()["gateway_projection_fields"]
            )
        )
        self.assertEqual(client.post("/api/v1/capabilities", json={}).status_code, 405)

    def test_health_fails_when_database_is_unavailable(self) -> None:
        client: Final[TestClient] = TestClient(create_app(FixedRepository(available=False)))
        self.assertEqual(client.get("/healthz").status_code, 503)

    def test_summary_uses_injected_reporting_clock(self) -> None:
        instant: Final[datetime] = datetime(2027, 2, 1, tzinfo=UTC)
        client: Final[TestClient] = TestClient(create_app(FixedRepository(), lambda: instant))
        self.assertEqual(client.get("/api/v1/summary").json()["period"], instant.strftime("%Y-%m"))
