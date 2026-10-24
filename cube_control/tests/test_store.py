import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import uuid4

from cube_control.models import Account, ImportBatch, Observation, Provider, UsageEvent
from cube_control.store import PostgresRepository

DSN: Final[str] = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = PostgresRepository(DSN)
        with cls.repository.connect() as connection:
            row: Final = connection.execute("SELECT current_database()").fetchone()
            if row is None or row[0] != "cubecontrol_test":
                raise RuntimeError("Integration tests require the dedicated cubecontrol_test database")
        cls.repository.initialize()

    def setUp(self) -> None:
        self.account = Account(id="test-" + uuid4().hex, provider=Provider.OPENAI, name="Test account", kind="api")
        self.now = datetime.now(UTC)
        self.repository.import_batch(ImportBatch(accounts=(self.account,)))

    def event(self, identity: str = "request-1", amount: str = "0.30") -> UsageEvent:
        return UsageEvent(
            account_id=self.account.id,
            event_id=identity,
            occurred_at=self.now,
            collected_at=self.now,
            model="fixture",
            period=self.now.strftime("%Y-%m"),
            amount=Decimal(amount),
        )

    def test_duplicate_reports_and_requests_are_idempotent(self) -> None:
        observation: Final[Observation] = Observation(
            account_id=self.account.id,
            external_id="report-1",
            metric="spend",
            window="month",
            period=self.now.strftime("%Y-%m"),
            status="known",
            source="provider_api",
            observed_at=self.now,
            collected_at=self.now,
            value=Decimal("0.40"),
            unit="USD",
        )
        batch: Final[ImportBatch] = ImportBatch(observations=(observation,), events=(self.event(),))
        first: Final = self.repository.import_batch(batch)
        second: Final = self.repository.import_batch(batch)
        self.assertTrue(first.ok)
        self.assertEqual(first.inserted, 2)
        self.assertTrue(second.ok)
        self.assertEqual(second.inserted, 0)

    def test_conflicting_request_identity_rolls_back_entire_batch(self) -> None:
        original: Final[UsageEvent] = self.event()
        self.assertTrue(self.repository.import_batch(ImportBatch(events=(original,))).ok)
        result: Final = self.repository.import_batch(
            ImportBatch(events=(self.event("request-2"), self.event(amount="99")))
        )
        self.assertFalse(result.ok)
        relevant: Final = tuple(e for e in self.repository.state().events if e.account_id == self.account.id)
        self.assertEqual(relevant, (original,))

    def test_concurrent_duplicate_imports_count_one_request(self) -> None:
        batch: Final[ImportBatch] = ImportBatch(events=(self.event(),))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results: Final = tuple(pool.map(self.repository.import_batch, (batch, batch)))
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(sum(r.inserted for r in results), 1)

    def test_application_role_is_not_a_database_administrator(self) -> None:
        with self.repository.connect() as connection:
            row: Final = connection.execute(
                "SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            self.assertEqual(row, (False, False, False))

    def test_account_cannot_silently_change_provider(self) -> None:
        changed: Final[Account] = self.account.model_copy(update={"provider": Provider.SUNO})
        result: Final = self.repository.import_batch(ImportBatch(accounts=(changed,)))
        self.assertFalse(result.ok)
        stored: Final = next(a for a in self.repository.state().accounts if a.id == self.account.id)
        self.assertEqual(stored.provider, self.account.provider)
