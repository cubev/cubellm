import os
import unittest
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Final
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

from cube_control.api import create_app
from cube_control.gateway_store import GatewayRepository
from cube_control.gateway import gateway_metrics
from cube_control.models import Account, GatewayBatch, GatewayRecord, GatewayScan, ImportBatch
from cube_control.store import PostgresRepository
from cube_control.usage import PostgresUsageReader, UsageFailure, UsageHistory, UsagePage, UsageQuery

DSN: Final = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")
NOW: Final = datetime(2026, 1, 15, tzinfo=UTC)


class UsageQueryTests(unittest.TestCase):
    def test_invalid_period_and_unbounded_pages_are_rejected(self) -> None:
        for changes in ({"period": "0000-01"}, {"period": "2026-13"}, {"limit": 0}, {"limit": 201}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                UsageQuery.model_validate({"period": "2026-01"} | changes)


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class UsageStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = PostgresRepository(DSN)
        with cls.repository.connect() as connection:
            row: Final = connection.execute("SELECT current_database()").fetchone()
            if row is None or row[0] != "cubecontrol_test":
                raise RuntimeError("Dedicated test database required")
        cls.repository.initialize()
        cls.gateway = GatewayRepository(DSN)
        cls.reader = PostgresUsageReader(DSN)

    def setUp(self) -> None:
        self.source = "usage-" + uuid4().hex
        registered: Final = self.repository.import_batch(
            ImportBatch(
                accounts=(
                    Account(id="account-one", provider="custom-provider", name="First fixture", kind="api"),
                    Account(id="account-two", provider="other-provider", name="Second fixture", kind="api"),
                )
            )
        )
        self.assertTrue(registered.ok)
        self.record = GatewayRecord(
            source_key="request-1",
            provider="custom-provider",
            model="original-model",
            occurred_at=NOW,
            status="succeeded",
            amount_usd=Decimal("0.123456789123456789"),
            input_tokens=17,
            output_tokens=3,
            application_id="app-one",
            account_id="account-one",
            deployment_id="deployment-one",
            call_type="responses",
            completed_at=NOW + timedelta(seconds=2),
            duration_ms=2000,
        )

    def import_records(self, records: tuple[GatewayRecord, ...], sequence: int = 0, checked_at: datetime = NOW) -> None:
        result: Final = self.gateway.import_batch(
            GatewayBatch(
                scan=GatewayScan(
                    scan_id=uuid4().hex,
                    source_id=self.source,
                    expected_sequence=sequence,
                    checked_at=checked_at,
                    state="ok",
                    available_rows=len(records),
                    scanned_rows=len(records),
                ),
                records=records,
            )
        )
        self.assertEqual(result.status, "applied")

    def page(self, **changes: object) -> UsagePage:
        result: Final = self.reader.page(
            UsageQuery.model_validate(
                {
                    "period": NOW.strftime("%Y-%m"),
                    "source_id": self.source,
                }
                | changes
            )
        )
        self.assertIsInstance(result, UsagePage)
        if not isinstance(result, UsagePage):
            self.fail("Usage page unavailable")
        return result

    def test_summary_sum_matches_exact_database_audit_with_widely_scaled_costs(self) -> None:
        occurred_at: Final = datetime.now(UTC)
        period: Final = occurred_at.strftime("%Y-%m")
        account: Final = Account(
            id="precision-" + uuid4().hex, provider="precision-" + uuid4().hex, name="Exact sum fixture", kind="api"
        )
        self.assertTrue(self.repository.import_batch(ImportBatch(accounts=(account,))).ok)
        original: Final = self.record.model_copy(
            update={
                "account_id": account.id,
                "provider": account.provider,
                "occurred_at": occurred_at,
                "completed_at": occurred_at + timedelta(seconds=2),
            }
        )
        records: Final = (
            original.model_copy(update={"amount_usd": Decimal("9007199254740993.12345678901234567890123456789")}),
            original.model_copy(
                update={"source_key": "tiny", "amount_usd": Decimal("0.00000000000000000000000000001")}
            ),
            original.model_copy(update={"source_key": "missing", "amount_usd": None}),
        )
        self.import_records(records, checked_at=occurred_at)
        page: Final = self.page(period=period)
        expected: Final = Decimal("9007199254740993.12345678901234567890123456790")
        self.assertEqual(page.metrics.reported_usd, expected)
        summary: Final = gateway_metrics(records, account.provider, period)
        self.assertEqual(summary.reported_usd, page.metrics.reported_usd)
        self.assertEqual(summary.unknown_cost_calls, 1)
        client: Final = TestClient(create_app(self.repository, clock=lambda: occurred_at, usage_reader=self.reader))
        accounts: Final = client.get("/api/v1/summary").json()["accounts"]
        summary_value: Final = next(
            item["data"]["gateway"]["reported_usd"] for item in accounts if item["data"]["account"]["id"] == account.id
        )
        audit_value: Final = client.get(f"/api/v1/usage?period={period}&source_id={self.source}").json()["metrics"][
            "reported_usd"
        ]
        self.assertEqual(Decimal(summary_value), expected)
        self.assertEqual(summary_value, audit_value)

    def test_exact_counters_and_unknowns_remain_separate_from_billing(self) -> None:
        missing: Final = self.record.model_copy(
            update={
                "source_key": "missing",
                "amount_usd": None,
                "input_tokens": None,
                "output_tokens": None,
                "application_id": None,
                "account_id": None,
                "status": "unknown",
            }
        )
        self.import_records((self.record, missing))
        page: Final = self.page()
        self.assertEqual(page.total, 2)
        self.assertEqual(page.metrics.reported_usd, self.record.amount_usd)
        self.assertEqual((page.metrics.input_tokens, page.metrics.output_tokens), (17, 3))
        self.assertEqual((page.metrics.unknown_input_calls, page.metrics.unknown_cost_calls), (1, 1))
        self.assertEqual((page.unknown_account_calls, page.unknown_application_calls), (1, 1))
        self.assertEqual(page.cost_basis, "gateway_reported")
        self.assertEqual(page.metrics.unknown_cache_read_calls, page.total)
        self.assertEqual({row.record.source_key for row in page.rows}, {"request-1", "missing"})

    def test_management_requests_are_distinguished_from_provider_calls_and_unknowns(self) -> None:
        management: Final = self.record.model_copy(update={"source_key": "management", "call_type": "/key/list"})
        unknown: Final = self.record.model_copy(update={"source_key": "unknown", "call_type": None})
        future: Final = self.record.model_copy(update={"source_key": "future", "call_type": "future-endpoint"})
        self.import_records((self.record, management, unknown, future))
        page: Final = self.page()
        self.assertEqual((page.provider_calls, page.management_calls, page.unknown_traffic_calls), (1, 1, 2))
        self.assertEqual(page.total, page.provider_calls + page.management_calls + page.unknown_traffic_calls)
        for kind, expected in (("provider_request", 1), ("gateway_management", 1), ("unknown", 2)):
            with self.subTest(kind=kind):
                filtered: Final = self.page(traffic_kind=kind)
                self.assertEqual(filtered.total, expected)
                self.assertEqual({row.traffic_kind for row in filtered.rows}, {kind})

    def test_historical_month_uses_utc_even_for_offset_timestamps(self) -> None:
        boundary: Final = self.record.model_copy(
            update={
                "source_key": "boundary",
                "occurred_at": datetime(2026, 2, 1, 0, 30, tzinfo=timezone(timedelta(hours=1))),
            }
        )
        february: Final = self.record.model_copy(
            update={
                "source_key": "february",
                "occurred_at": datetime(2026, 2, 1, tzinfo=UTC),
            }
        )
        self.import_records((self.record, boundary, february))
        self.assertEqual({row.record.source_key for row in self.page().rows}, {"request-1", "boundary"})
        self.assertEqual([row.record.source_key for row in self.page(period="2026-02").rows], ["february"])

    def test_all_filters_are_applied_to_rows_and_totals(self) -> None:
        other: Final = self.record.model_copy(
            update={
                "source_key": "other",
                "provider": "other-provider",
                "account_id": "account-two",
                "application_id": "app-two",
                "model": "another-model",
                "status": "failed",
            }
        )
        self.import_records((self.record, other))
        for field, value in (
            ("provider", self.record.provider),
            ("account_id", self.record.account_id),
            ("application_id", self.record.application_id),
            ("model", self.record.model),
            ("status", self.record.status),
        ):
            with self.subTest(field=field):
                page: Final = self.page(**{field: value})
                self.assertEqual(page.total, 1)
                self.assertEqual(page.metrics.calls, 1)
                self.assertEqual(page.rows[0].record, self.record)
        self.assertEqual(self.page(model="' OR true --").total, 0)

    def test_pages_are_stable_and_changed_import_requires_restart(self) -> None:
        records: Final = tuple(self.record.model_copy(update={"source_key": f"request-{index}"}) for index in range(3))
        self.import_records(records)
        query: Final = UsageQuery(period="2026-01", source_id=self.source, limit=1)
        first: Final = self.reader.page(query)
        self.assertIsInstance(first, UsagePage)
        if not isinstance(first, UsagePage):
            self.fail("First page unavailable")
        second: Final = self.reader.page(query, first.next_cursor)
        self.assertIsInstance(second, UsagePage)
        if not isinstance(second, UsagePage):
            self.fail("Second page unavailable")
        self.assertNotEqual(first.rows[0].record.source_key, second.rows[0].record.source_key)
        self.assertEqual(first.total, second.total)
        self.import_records(records, sequence=1)
        changed: Final = self.reader.page(query, second.next_cursor)
        self.assertIsInstance(changed, UsageFailure)
        if isinstance(changed, UsageFailure):
            self.assertEqual(changed.code, "changed")

    def test_cursors_reject_invalid_data_and_different_filters(self) -> None:
        self.import_records((self.record, self.record.model_copy(update={"source_key": "second"})))
        first: Final = self.page(limit=1)
        for cursor in ("invalid", "{}", first.next_cursor):
            result: Final = self.reader.page(UsageQuery(period="2026-02", source_id=self.source, limit=1), cursor)
            self.assertIsInstance(result, UsageFailure)
            if isinstance(result, UsageFailure):
                self.assertEqual(result.code, "invalid_cursor")

    def test_corrections_and_reverts_are_inspectable_without_double_counting(self) -> None:
        corrected: Final = self.record.model_copy(update={"amount_usd": Decimal("0.2")})
        for sequence, record in enumerate((self.record, corrected, self.record)):
            self.import_records((record,), sequence)
        page: Final = self.page()
        self.assertEqual(page.total, 1)
        self.assertEqual(page.metrics.reported_usd, self.record.amount_usd)
        history: Final = self.reader.history(self.source, self.record.source_key, limit=2)
        self.assertIsInstance(history, UsageHistory)
        if not isinstance(history, UsageHistory):
            self.fail("History unavailable")
        self.assertEqual(tuple(change.record for change in history.changes), (self.record, corrected))
        self.assertEqual(history.next_after, 2)
        self.assertEqual(history.changes[0].content_hash, history.changes[1].previous_hash)
        following: Final = self.reader.history(self.source, self.record.source_key, after=2, limit=2)
        self.assertIsInstance(following, UsageHistory)
        if isinstance(following, UsageHistory):
            self.assertEqual(tuple(change.record for change in following.changes), (self.record,))
            self.assertIsNone(following.next_after)

    def test_http_exposes_filtered_rows_history_and_read_only_validation(self) -> None:
        self.import_records((self.record, self.record.model_copy(update={"source_key": "second"})))
        client: Final = TestClient(create_app(self.repository, usage_reader=self.reader))
        params: Final = {"period": "2026-01", "source_id": self.source, "limit": 1}
        response: Final = client.get("/api/v1/usage", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 2)
        next_page: Final = client.get("/api/v1/usage", params=params | {"cursor": response.json()["next_cursor"]})
        self.assertEqual(next_page.status_code, 200, next_page.text)
        self.assertNotEqual(response.json()["rows"], next_page.json()["rows"])
        history: Final = client.get(f"/api/v1/usage/{self.source}/{self.record.source_key}/history")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.json()["changes"]), 1)
        self.assertEqual(client.post("/api/v1/usage", json={}).status_code, 405)
        self.assertEqual(client.get("/api/v1/usage", params=params | {"limit": 201}).status_code, 422)
        self.assertEqual(client.get("/api/v1/usage", params=params | {"period": "0000-01"}).status_code, 422)
        self.assertEqual(client.get("/api/v1/usage", params=params | {"cursor": "bad"}).status_code, 422)
