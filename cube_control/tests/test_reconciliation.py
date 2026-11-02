import os
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, NoReturn
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError

from cube_control.api import create_app
from cube_control.credential_binding import CredentialBindingCommand
from cube_control.gateway_store import GatewayPeriod, GatewayRepository
from cube_control.models import (
    Account,
    GatewayBatch,
    GatewayRecord,
    GatewayScan,
    GatewayScanView,
    ImportBatch,
    ManualCommand,
    Observation,
    Provider,
    SpendCoverage,
)
from cube_control.reconciliation import (
    PERIOD_REJECTED,
    ReconciliationReport,
    ReconciliationRow,
    parse_period,
    reconcile,
    reconcile_stored,
)
from cube_control.store import ImportResult, ManualResult, PostgresRepository, StoredState
from cube_control.summary import latest_measurements


class UntouchedStore:
    def state(self) -> NoReturn:
        raise AssertionError("Store read before input validation")

    def healthy(self) -> NoReturn:
        raise AssertionError("Store read before input validation")

    def import_batch(self, batch: ImportBatch) -> ImportResult:
        raise AssertionError(f"Unexpected write {batch!r}")

    def manage(self, command: ManualCommand, actor: str, now: datetime) -> ManualResult:
        raise AssertionError(f"Unexpected write {command!r} {actor} {now}")

    def bind_credential(self, command: CredentialBindingCommand, actor: str, now: datetime) -> ManualResult:
        raise AssertionError(f"Unexpected write {command!r} {actor} {now}")

    def period(self, start: datetime, end: datetime) -> NoReturn:
        raise AssertionError(f"Gateway read before input validation {start} {end}")


DSN: Final = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")
START: Final = datetime(2026, 9, 1, tzinfo=UTC)
END: Final = datetime(2026, 10, 1, tzinfo=UTC)
CLOSED: Final = datetime(2026, 10, 3, tzinfo=UTC)
ACCOUNT: Final = Account(id="openai-main", provider=Provider.OPENAI, name="OpenAI", kind="api")
SUBSCRIPTION: Final = Account(id="chat-plan", provider=Provider.OPENAI, name="Plan", kind="subscription")


def statement(
    value: str,
    source: Literal["provider_api", "local_collector", "manual", "import"] = "provider_api",
    basis: Literal["final_billing_statement", "usage_to_date"] | None = "final_billing_statement",
    start: datetime = START,
    unit: Literal["USD", "EUR"] = "USD",
    observed_at: datetime = CLOSED - timedelta(minutes=1),
    external_id: str = "statement",
) -> Observation:
    return Observation(
        account_id=ACCOUNT.id,
        external_id=external_id,
        metric="spend",
        window=f"{source}-statement",
        status="known",
        source=source,
        observed_at=observed_at,
        collected_at=observed_at,
        value=Decimal(value),
        unit=unit,
        period="2026-09",
        coverage=SpendCoverage(start=start, end=END, basis=basis, scope="account_total") if basis else None,
    )


def call(
    key: str,
    amount: str | None,
    call_type: str | None = "acompletion",
    account_id: str | None = ACCOUNT.id,
    provider: Provider | None = Provider.OPENAI,
) -> GatewayRecord:
    return GatewayRecord(
        source_key=key,
        provider=provider,
        account_id=account_id,
        call_type=call_type,
        model="gpt",
        occurred_at=datetime(2026, 9, 10, tzinfo=UTC),
        status="succeeded",
        amount_usd=Decimal(amount) if amount is not None else None,
    )


def scan(
    source_id: str = "gateway-a",
    state: Literal["ok", "partial", "error"] = "ok",
    checked_at: datetime = datetime(2026, 10, 2, tzinfo=UTC),
    coverage_start: datetime | None = START,
    rejected_rows: int = 0,
) -> GatewayScanView:
    return GatewayScanView(
        scan=GatewayScan(
            scan_id=f"{source_id}-scan",
            source_id=source_id,
            expected_sequence=0,
            checked_at=checked_at,
            state=state,
            rejected_rows=rejected_rows,
            coverage_start=coverage_start,
        ),
        sequence=1,
    )


def row(
    observations: tuple[Observation, ...] = (statement("10.10"),),
    records: tuple[GatewayRecord, ...] = (call("a", "0.1"), call("b", "0.2")),
    scans: tuple[GatewayScanView, ...] = (scan(),),
    now: datetime = CLOSED,
) -> ReconciliationRow:
    return next(
        item
        for item in reconcile((ACCOUNT,), observations, records, scans, "2026-09", now).rows
        if item.account_id == ACCOUNT.id
    )


class ReconciliationTests(unittest.TestCase):
    def test_any_calendar_month_with_a_representable_following_month_is_a_period(self) -> None:
        self.assertEqual(
            [parse_period(period) for period in ("0001-01", "1999-12", "2124-02", "9999-11")],
            [
                (datetime(1, 1, 1, tzinfo=UTC), datetime(1, 2, 1, tzinfo=UTC)),
                (datetime(1999, 12, 1, tzinfo=UTC), datetime(2000, 1, 1, tzinfo=UTC)),
                (datetime(2124, 2, 1, tzinfo=UTC), datetime(2124, 3, 1, tzinfo=UTC)),
                (datetime(9999, 11, 1, tzinfo=UTC), datetime(9999, 12, 1, tzinfo=UTC)),
            ],
        )

    def test_out_of_range_periods_are_rejected_before_any_store_access(self) -> None:
        store: Final = UntouchedStore()
        client: Final = TestClient(create_app(store, clock=lambda: CLOSED, gateway_reader=store))
        periods: Final = ("0000-01", "9999-12", "2026-13", "2026-00", "2026-9", "x2026-09")
        self.assertEqual(
            [client.get("/api/v1/reconciliation", params={"period": period}).status_code for period in periods],
            [422] * len(periods),
        )
        cli: Final = tuple(
            subprocess.run(
                [sys.executable, "-m", "cube_control", "reconcile", "--period", period],
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
                env={**os.environ, "CUBE_CONTROL_DATABASE_URL": "host=/nonexistent-socket dbname=unused"},
            )
            for period in ("0000-01", "9999-12")
        )
        self.assertEqual(
            [(item.returncode, item.stdout, item.stderr) for item in cli],
            [(2, "", PERIOD_REJECTED + "\n")] * 2,
        )

    def test_closed_month_with_final_statement_and_complete_capture_compares_exactly(self) -> None:
        result: Final = row(records=(call("a", "0.1"), call("b", "0.2"), call("m", "5", call_type="/v1/models")))
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.comparison, "consistent_with_provider_total")
        self.assertEqual((result.delta, result.direction), (Decimal("9.80"), "provider_higher"))
        self.assertEqual(
            (result.gateway.provider_request_usd, result.gateway.management_calls, result.gateway.management_usd),
            (Decimal("0.3"), 1, Decimal("5")),
        )

    def test_gateway_spend_above_the_provider_total_is_an_anomaly_not_agreement(self) -> None:
        result: Final = row(observations=(statement("0.25"),))
        self.assertEqual(
            (result.comparison, result.direction, result.delta),
            ("gateway_exceeds_provider_total", "gateway_higher", Decimal("-0.05")),
        )

    def test_unknown_costs_and_unknown_traffic_are_never_counted_as_zero(self) -> None:
        cases: Final = (
            ((call("a", "0.1"), call("u", None)), "gateway_costs_unknown"),
            ((call("a", "0.1"), call("t", "0.4", call_type=None)), "gateway_traffic_unknown"),
            ((call("a", "0.1", provider=Provider.ANTHROPIC),), "gateway_provider_mismatch"),
        )
        for records, reason in cases:
            with self.subTest(reason=reason):
                result = row(records=records)
                self.assertIn(reason, result.reasons)
                self.assertEqual(result.comparison, "not_comparable")
                self.assertEqual(result.gateway.provider_request_usd, Decimal("0.1"))

    def test_provider_figures_without_a_final_statement_for_exactly_the_month_are_not_comparable(self) -> None:
        cases: Final = (
            (statement("10", basis=None), "provider_coverage_unstructured"),
            (statement("10", basis="usage_to_date"), "provider_coverage_not_final"),
            (statement("10", start=datetime(2024, 1, 1, tzinfo=UTC)), "provider_coverage_other_window"),
            (statement("10", source="manual"), "provider_not_provider_reported"),
            (statement("10", unit="EUR"), "currency_mismatch"),
        )
        for observation, reason in cases:
            with self.subTest(reason=reason):
                result = row(observations=(observation,))
                self.assertIn(reason, result.reasons)
                self.assertEqual(result.comparison, "not_comparable")

    def test_a_final_statement_cannot_be_observed_before_its_coverage_ends(self) -> None:
        for observed_at in (datetime(2026, 9, 11, tzinfo=UTC), END - timedelta(microseconds=1)):
            with self.subTest(observed_at=observed_at), self.assertRaisesRegex(ValidationError, "before its coverage"):
                statement("10", observed_at=observed_at)
        running: Final = statement("10", basis="usage_to_date", observed_at=datetime(2026, 9, 11, tzinfo=UTC))
        self.assertIsNotNone(running.coverage)
        at_end: Final = row(observations=(statement("10.3", observed_at=END),))
        self.assertEqual((at_end.reasons, at_end.comparison), ((), "consistent_with_provider_total"))

    def test_enriched_evidence_wins_a_tie_with_its_legacy_projection_in_any_order(self) -> None:
        enriched: Final = statement("10.10", external_id="enriched")
        legacy: Final = enriched.model_copy(update={"coverage": None, "external_id": "legacy"})
        for observations in ((legacy, enriched), (enriched, legacy)):
            with self.subTest(first=observations[0].external_id):
                self.assertEqual(latest_measurements(ACCOUNT.id, observations), (enriched,))
        with self.assertRaisesRegex(ValidationError, "beyond the provider query"):
            SpendCoverage(
                start=START, end=END, basis="usage_to_date", scope="account_total", query_end=END - timedelta(1)
            )

    def test_month_to_date_figures_never_count_as_a_closed_comparison(self) -> None:
        now: Final = datetime(2026, 9, 20, tzinfo=UTC)
        result: Final = row(
            observations=(statement("10.10", basis="usage_to_date", observed_at=now - timedelta(minutes=1)),),
            scans=(scan(checked_at=now),),
            now=now,
        )
        self.assertIn("month_not_closed", result.reasons)
        self.assertEqual(result.comparison, "not_comparable")
        self.assertEqual(result.direction, "provider_higher")

    def test_disagreeing_current_provider_readings_block_the_comparison(self) -> None:
        result: Final = row(observations=(statement("10.10"), statement("11", source="import")))
        self.assertIn("provider_conflict", result.reasons)
        self.assertEqual(result.provider_reading.source if result.provider_reading else None, "provider_api")
        self.assertEqual(len(result.provider_evidence), 2)

    def test_final_statement_survives_its_freshness_window_but_a_stale_running_total_does_not(self) -> None:
        later: Final = datetime(2027, 3, 1, tzinfo=UTC)
        self.assertNotIn("provider_not_current", row(now=later).reasons)
        stale: Final = row(observations=(statement("10", basis="usage_to_date"),), now=later)
        self.assertIn("provider_not_current", stale.reasons)
        self.assertEqual((stale.provider_reading, stale.provider_evidence[0].current), (None, False))

    def test_every_gateway_source_must_have_read_the_whole_month(self) -> None:
        cases: Final = (
            ((), "gateway_scan_missing"),
            ((scan(), scan("gateway-b", state="partial")), "gateway_scan_incomplete"),
            ((scan(rejected_rows=1),), "gateway_scan_rejected_rows"),
            ((scan(checked_at=datetime(2026, 10, 9, tzinfo=UTC)),), "gateway_scan_clock_skew"),
            ((scan(checked_at=datetime(2026, 9, 30, tzinfo=UTC)),), "gateway_scan_before_period_end"),
            ((scan(coverage_start=None),), "gateway_coverage_unknown"),
            ((scan(coverage_start=datetime(2026, 9, 2, tzinfo=UTC)),), "gateway_coverage_after_period_start"),
        )
        for scans, reason in cases:
            with self.subTest(reason=reason):
                result = row(scans=scans)
                self.assertIn(reason, result.reasons)
                self.assertEqual(result.comparison, "not_comparable")

    def test_running_month_scan_older_than_the_capture_window_is_stale(self) -> None:
        now: Final = datetime(2026, 9, 20, 12, tzinfo=UTC)
        self.assertIn("gateway_scan_stale", row(scans=(scan(checked_at=now - timedelta(hours=1)),), now=now).reasons)
        self.assertNotIn(
            "gateway_scan_stale", row(scans=(scan(checked_at=now - timedelta(minutes=1)),), now=now).reasons
        )

    def test_all_catalogued_api_accounts_and_unresolved_targets_stay_visible(self) -> None:
        report: Final = reconcile(
            (ACCOUNT, SUBSCRIPTION),
            (),
            (call("x", "1", account_id="retired"), call("y", "2", account_id=None, provider=None)),
            (scan(),),
            "2026-09",
            CLOSED,
        )
        self.assertEqual(
            [(item.account_id, item.reasons[:3]) for item in report.rows],
            [
                ("openai-main", ("provider_missing", "gateway_missing")),
                ("retired", ("account_not_in_catalogue", "provider_missing")),
            ],
        )
        self.assertEqual(
            [(item.provider, item.gateway.provider_request_usd) for item in report.unattributed],
            [("unknown", Decimal("2"))],
        )

    def test_a_past_month_without_its_gateway_records_is_reported_as_not_loaded(self) -> None:
        state: Final = StoredState((ACCOUNT,), (statement("10.10"),), (), (), gateway_records=(call("a", "0.1"),))
        unloaded: Final = reconcile_stored(state, None, "2026-09", CLOSED)
        loaded: Final = reconcile_stored(state, GatewayPeriod((scan(),), (call("a", "0.1"),)), "2026-09", CLOSED)
        self.assertEqual((unloaded.gateway_loaded, "gateway_not_loaded" in unloaded.rows[0].reasons), (False, True))
        self.assertEqual(
            (loaded.rows[0].comparison, loaded.rows[0].delta), ("consistent_with_provider_total", Decimal("10.00"))
        )


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class ReconciliationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = PostgresRepository(DSN)
        with self.repository.connect() as connection:
            database: Final = connection.execute("SELECT current_database()").fetchone()
        if database is None or database[0] != "cubecontrol_test":
            self.fail("Dedicated test database required")
        self.gateway = GatewayRepository(DSN)
        self.source = "reconcile-" + uuid4().hex
        self.account = Account(id="reconcile-" + uuid4().hex, provider="reconcile-provider", name="Fixture", kind="api")
        self.repository.initialize()
        self.addCleanup(self.remove_fixtures)
        self.remove_fixtures()

    def remove_fixtures(self) -> None:
        with self.repository.connect() as connection:
            for table in ("gateway_changes", "gateway_records", "gateway_scans"):
                connection.execute(f"DELETE FROM cube_control.{table} WHERE source_id=%s", (self.source,))
            connection.execute("DELETE FROM cube_control.observations WHERE account_id=%s", (self.account.id,))
            connection.execute("DELETE FROM cube_control.accounts WHERE id=%s", (self.account.id,))

    def store_month(self) -> None:
        bounds: Final = (
            ("before", START - timedelta(microseconds=1), "1"),
            ("first", START, "0.000000000000000001"),
            ("last", END - timedelta(microseconds=1), "2.5"),
            ("after", END, "7"),
        )
        records: Final = tuple(
            call(key, amount, account_id=self.account.id).model_copy(
                update={"provider": self.account.provider, "occurred_at": occurred_at}
            )
            for key, occurred_at, amount in bounds
        )
        provider_total: Final = statement("4", source="import").model_copy(update={"account_id": self.account.id})
        self.assertTrue(
            self.repository.import_batch(ImportBatch(accounts=(self.account,), observations=(provider_total,))).ok
        )
        imported: Final = self.gateway.import_batch(
            GatewayBatch(
                scan=scan(self.source).scan.model_copy(
                    update={"scan_id": uuid4().hex, "available_rows": len(records), "scanned_rows": len(records)}
                ),
                records=records,
            )
        )
        self.assertEqual(imported.status, "applied")

    def test_stored_month_boundaries_coverage_and_exact_sums_reach_the_http_report(self) -> None:
        self.store_month()
        client: Final = TestClient(create_app(self.repository, clock=lambda: CLOSED, gateway_reader=self.gateway))
        self.assertEqual(client.get("/api/v1/reconciliation?period=2026-13").status_code, 422)
        response: Final = client.get("/api/v1/reconciliation?period=2026-09")
        self.assertIn('"delta":"1.499999999999999999"', response.text)
        report: Final = ReconciliationReport.model_validate_json(response.text)
        own: Final = next(item for item in report.rows if item.account_id == self.account.id)
        source: Final = next(item for item in report.scans if item.source_id == self.source)
        self.assertEqual((source.status, source.coverage_start), ("complete", START))
        self.assertEqual(
            (own.gateway.provider_request_calls, own.gateway.provider_request_usd, own.delta),
            (2, Decimal("2.500000000000000001"), Decimal("1.499999999999999999")),
        )
        self.assertEqual(
            own.provider_reading.coverage.basis if own.provider_reading and own.provider_reading.coverage else None,
            "final_billing_statement",
        )
        self.assertEqual(
            [reason for reason in own.reasons if not reason.startswith(("gateway_scan", "gateway_coverage"))], []
        )

    def test_the_cli_prints_the_same_exact_report_for_a_stored_month(self) -> None:
        self.store_month()
        result: Final = subprocess.run(
            [sys.executable, "-m", "cube_control", "reconcile", "--period", "2026-09"],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
            env={**os.environ, "CUBE_CONTROL_DATABASE_URL": DSN},
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        report: Final = ReconciliationReport.model_validate_json(result.stdout)
        own: Final = next(item for item in report.rows if item.account_id == self.account.id)
        self.assertEqual(
            (report.period, report.gateway_loaded, own.gateway.provider_request_usd, own.delta),
            ("2026-09", True, Decimal("2.500000000000000001"), Decimal("1.499999999999999999")),
        )

    def test_a_downgraded_import_and_its_enriched_retry_coexist_without_identity_conflicts(self) -> None:
        enriched: Final = statement("4", source="import", external_id="local-enriched").model_copy(
            update={"account_id": self.account.id}
        )
        legacy: Final = enriched.model_copy(update={"coverage": None, "external_id": "local-legacy"})
        results: Final = tuple(
            self.repository.import_batch(ImportBatch(accounts=(self.account,), observations=observations))
            for observations in ((legacy,), (enriched,), (enriched,), (legacy,))
        )
        self.assertEqual([result.ok for result in results], [True] * 4)
        self.assertEqual([result.inserted for result in results], [1, 1, 0, 0])
        conflicting: Final = enriched.model_copy(update={"value": Decimal("5")})
        self.assertFalse(self.repository.import_batch(ImportBatch(observations=(conflicting,))).ok)
        state: Final = self.repository.state()
        stored: Final = tuple(item for item in state.observations if item.account_id == self.account.id)
        self.assertEqual(sorted(item.external_id for item in stored), ["local-enriched", "local-legacy"])
        self.assertEqual(latest_measurements(self.account.id, stored), (enriched,))
        own: Final = next(
            item for item in reconcile_stored(state, None, "2026-09", CLOSED).rows if item.account_id == self.account.id
        )
        self.assertEqual(
            own.provider_reading.coverage if own.provider_reading else None,
            enriched.coverage,
        )


if __name__ == "__main__":
    unittest.main()
