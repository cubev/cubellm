import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import uuid4

from pydantic import ValidationError

from cube_control.gateway import (
    GatewayProjection,
    GatewayProjectionBatch,
    gateway_metrics,
    prepare_gateway,
    projected_record,
)
from cube_control.gateway_store import GatewayRepository
from cube_control.models import GatewayBatch, GatewayRecord, GatewayScan, Provider
from cube_control.store import PostgresRepository

NOW: Final = datetime.now(UTC)
DSN: Final = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")


def projection() -> GatewayProjection:
    return GatewayProjection(
        request_id="source-primary-key",
        reported_provider="openrouter",
        endpoint_provider=None,
        custom_endpoint=False,
        model="model-alias",
        occurred_at=NOW,
        source_updated_at=NOW,
        status="success",
        amount_usd=Decimal("0.10"),
        input_tokens=10,
        output_tokens=2,
        cache_read_tokens=None,
        cache_write_tokens=None,
    )


class GatewayTests(unittest.TestCase):
    def test_endpoint_overrides_protocol_and_model_is_not_provider_identity(self) -> None:
        row: Final = projection().model_copy(
            update={"reported_provider": "openai", "endpoint_provider": Provider.OPENROUTER, "custom_endpoint": True}
        )
        self.assertEqual(projected_record(row, "source").provider, Provider.OPENROUTER)
        unassigned: Final = row.model_copy(update={"endpoint_provider": None, "model": "openai/model"})
        self.assertIsNone(projected_record(unassigned, "source").provider)

    def test_unknown_amounts_and_tokens_remain_unknown(self) -> None:
        row: Final = projection().model_copy(update={"amount_usd": None, "input_tokens": None})
        record: Final = projected_record(row, "source")
        metrics: Final = gateway_metrics((record,), Provider.OPENROUTER, NOW.strftime("%Y-%m"))
        self.assertEqual((metrics.calls, metrics.unknown_cost_calls, metrics.unknown_input_calls), (1, 1, 1))
        self.assertIsNone(record.amount_usd)
        self.assertIsNone(record.cache_read_tokens)
        zero: Final = record.model_copy(update={"amount_usd": Decimal(0), "input_tokens": 0})
        known: Final = gateway_metrics((zero,), Provider.OPENROUTER, NOW.strftime("%Y-%m"))
        self.assertEqual((known.unknown_cost_calls, known.unknown_input_calls), (0, 0))

    def test_boolean_is_not_a_measured_token_count(self) -> None:
        with self.assertRaises(ValidationError):
            GatewayProjection.model_validate(projection().model_dump() | {"input_tokens": True})
        with self.assertRaises(ValidationError):
            GatewayRecord.model_validate(projected_record(projection(), "source").model_dump() | {"input_tokens": True})

    def test_source_namespace_and_hashed_primary_key(self) -> None:
        first: Final = projected_record(projection(), "instance-a")
        second: Final = projected_record(projection(), "instance-b")
        self.assertNotEqual(first.source_key, second.source_key)
        self.assertNotIn("source-primary-key", first.model_dump_json())

    def test_accidental_sensitive_projection_is_rejected_without_echo(self) -> None:
        raw: Final = projection().model_dump(mode="json") | {"messages": "private-content"}
        batch: Final = prepare_gateway(
            GatewayProjectionBatch(
                scan=GatewayScan(
                    scan_id="scan",
                    source_id="source",
                    expected_sequence=0,
                    checked_at=NOW,
                    state="ok",
                    available_rows=1,
                    scanned_rows=1,
                ),
                identity="instance",
                rows=(raw,),
            )
        )
        self.assertFalse(batch.records)
        self.assertEqual((batch.scan.state, batch.scan.rejected_rows), ("partial", 1))
        self.assertNotIn("private-content", batch.model_dump_json())


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class GatewayStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.general = PostgresRepository(DSN)
        with cls.general.connect() as connection:
            row: Final = connection.execute("SELECT current_database()").fetchone()
            if row is None or row[0] != "cubecontrol_test":
                raise RuntimeError("Dedicated test database required")
        cls.general.initialize()
        cls.repository = GatewayRepository(DSN)

    def setUp(self) -> None:
        self.source = "test-" + uuid4().hex
        self.record = projected_record(projection(), self.source)

    def batch(self, sequence: int, record: GatewayRecord | None = None, identity: str | None = None) -> GatewayBatch:
        return GatewayBatch(
            scan=GatewayScan(
                scan_id=identity or uuid4().hex,
                source_id=self.source,
                expected_sequence=sequence,
                checked_at=NOW,
                state="ok",
                available_rows=1,
                scanned_rows=1,
            ),
            records=(record or self.record,),
        )

    def mirror(self) -> GatewayRecord:
        with self.general.connect() as connection:
            row: Final = connection.execute(
                "SELECT payload::text FROM cube_control.gateway_records WHERE source_id=%s", (self.source,)
            ).fetchone()
            self.assertIsNotNone(row)
            return GatewayRecord.model_validate_json(row[0] if row else "{}")

    def changes(self) -> int:
        with self.general.connect() as connection:
            row: Final = connection.execute(
                "SELECT count(*)::text FROM cube_control.gateway_changes WHERE source_id=%s", (self.source,)
            ).fetchone()
            return int(row[0]) if row else 0

    def test_retries_and_unchanged_rescans_do_not_duplicate_changes(self) -> None:
        batch: Final = self.batch(0)
        self.assertEqual(self.repository.import_batch(batch).status, "applied")
        self.assertEqual(self.repository.import_batch(batch).status, "duplicate")
        result: Final = self.repository.import_batch(self.batch(1))
        self.assertEqual((result.status, result.changed), ("applied", 0))
        self.assertEqual(self.changes(), 1)

    def test_latest_scan_keeps_advancing_past_single_digit_sequences(self) -> None:
        scan_count: Final = 12
        for sequence in range(scan_count):
            self.assertEqual(self.repository.import_batch(self.batch(sequence)).status, "applied")
        latest: Final = next(
            item for item in self.general.state().gateway_scans if item.scan.source_id == self.source
        )
        self.assertEqual(latest.sequence, scan_count)
        following: Final = self.repository.import_batch(self.batch(latest.sequence))
        self.assertEqual((following.status, following.sequence), ("applied", scan_count + 1))

    def test_correction_and_revert_work_with_identical_source_timestamp(self) -> None:
        for sequence, amount in enumerate(("0.10", "0.20", "0.10")):
            result: Final = self.repository.import_batch(
                self.batch(sequence, self.record.model_copy(update={"amount_usd": Decimal(amount)}))
            )
            self.assertEqual(result.status, "applied")
        self.assertEqual(self.mirror().amount_usd, Decimal("0.10"))
        self.assertEqual(self.changes(), 3)

    def test_attribution_change_moves_call_instead_of_double_counting(self) -> None:
        self.assertEqual(
            self.repository.import_batch(self.batch(0, self.record.model_copy(update={"provider": None}))).status,
            "applied",
        )
        self.assertEqual(self.repository.import_batch(self.batch(1)).status, "applied")
        current: Final = (self.mirror(),)
        self.assertEqual(gateway_metrics(current, None, NOW.strftime("%Y-%m")).calls, 0)
        self.assertEqual(gateway_metrics(current, Provider.OPENROUTER, NOW.strftime("%Y-%m")).calls, 1)

    def test_scan_identity_conflict_and_concurrent_scans_are_atomic(self) -> None:
        batch: Final = self.batch(0)
        self.assertEqual(self.repository.import_batch(batch).status, "applied")
        changed: Final = batch.model_copy(
            update={"records": (self.record.model_copy(update={"amount_usd": Decimal(50)}),)}
        )
        self.assertEqual(self.repository.import_batch(changed).status, "rejected")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results: Final = tuple(pool.map(self.repository.import_batch, (self.batch(1), self.batch(1))))
        self.assertEqual(sorted(result.status for result in results), ["applied", "conflict"])
        self.assertEqual(self.mirror().amount_usd, self.record.amount_usd)

    def test_missing_rows_or_error_do_not_delete_known_history(self) -> None:
        self.assertEqual(self.repository.import_batch(self.batch(0)).status, "applied")
        empty: Final = GatewayBatch(
            scan=GatewayScan(
                scan_id=uuid4().hex,
                source_id=self.source,
                expected_sequence=1,
                checked_at=NOW,
                state="ok",
                available_rows=0,
            )
        )
        self.assertEqual(self.repository.import_batch(empty).status, "applied")
        error: Final = GatewayBatch(
            scan=GatewayScan(
                scan_id=uuid4().hex,
                source_id=self.source,
                expected_sequence=2,
                checked_at=NOW,
                state="error",
                message="Source unavailable",
            )
        )
        self.assertEqual(self.repository.import_batch(error).status, "applied")
        self.assertEqual(self.mirror(), self.record)
        self.assertEqual(self.changes(), 1)
