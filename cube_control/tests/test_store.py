import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import uuid4

from inventory_fixtures import bound, gateway_inventory, snapshot

from cube_control.gateway_inventory import GatewayInventoryAttempt, GatewayInventoryState
from cube_control.models import Account, ImportBatch, ManualChange, ManualCommand, Observation, Provider, UsageEvent
from cube_control.store import PostgresRepository
from cube_control.summary import account_view

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

    def test_creation_is_atomic_idempotent_and_never_replaces_existing_identity(self) -> None:
        account: Final = Account(id=self.account.id + "-new", provider="custom-provider", name="Custom", kind="api")
        command: Final = ManualCommand(
            command_id=account.id + "-create", account_id=account.id, expected_version=0, create_account=account
        )
        self.assertEqual(self.repository.manage(command, "owner", self.now).status, "applied")
        self.assertEqual(self.repository.manage(command, "owner", self.now).status, "duplicate")
        self.assertEqual(self.repository.manage(command, "different-owner", self.now).status, "rejected")
        replacement: Final = command.model_copy(update={"command_id": account.id + "-other"})
        self.assertEqual(self.repository.manage(replacement, "owner", self.now).status, "rejected")
        incoming: Final = account.model_copy(update={"name": "Automatic name", "state": "active"})
        self.assertTrue(self.repository.import_batch(ImportBatch(accounts=(incoming,))).ok)
        state: Final = self.repository.state()
        view: Final = account_view(incoming, state, self.now, self.now.strftime("%Y-%m"))
        self.assertEqual(view.account, account)
        self.assertEqual(view.manual_version, 1)
        self.assertEqual(len(tuple(e for e in state.manual_entries if e.command.account_id == account.id)), 1)

    def test_failed_creation_does_not_leave_an_account_without_journal(self) -> None:
        account: Final = Account(id=self.account.id + "-invalid", provider="custom-provider", name="New", kind="api")
        command: Final = ManualCommand(
            command_id=account.id + "-create", account_id=account.id, expected_version=0, create_account=account
        )
        self.assertEqual(self.repository.manage(command, "", self.now).status, "rejected")
        self.assertFalse(any(a.id == account.id for a in self.repository.state().accounts))
        self.assertEqual(self.repository.manage(command, "owner", self.now).status, "applied")

    def test_concurrent_creations_have_one_winner_and_one_history_entry(self) -> None:
        account: Final = Account(id=self.account.id + "-concurrent", provider="custom-provider", name="New", kind="api")
        commands: Final = tuple(
            ManualCommand(
                command_id=account.id + str(index), account_id=account.id, expected_version=0, create_account=account
            )
            for index in range(2)
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results: Final = tuple(
                pool.map(lambda command: self.repository.manage(command, "owner", self.now), commands)
            )
        self.assertEqual(sorted(result.status for result in results), ["applied", "rejected"])
        self.assertEqual(
            len(tuple(e for e in self.repository.state().manual_entries if e.command.account_id == account.id)), 1
        )

    def command(self, identity: str = "manual-1", version: int = 0) -> ManualCommand:
        return ManualCommand(
            command_id=self.account.id + identity,
            account_id=self.account.id,
            expected_version=version,
            changes=(ManualChange(field="plan", action="set", value="Manual plan"),),
        )

    def test_manual_retries_are_idempotent_but_changed_identity_is_rejected(self) -> None:
        command: Final = self.command()
        first: Final = self.repository.manage(command, "owner", self.now)
        second: Final = self.repository.manage(command, "owner", self.now)
        self.assertEqual((first.status, second.status), ("applied", "duplicate"))
        self.assertEqual((first.version, second.version), (1, 1))
        changed: Final = command.model_copy(
            update={"changes": (ManualChange(field="plan", action="set", value="Different"),)}
        )
        self.assertEqual(self.repository.manage(changed, "owner", self.now).status, "rejected")
        entries: Final = tuple(
            e for e in self.repository.state().manual_entries if e.command.account_id == self.account.id
        )
        self.assertEqual(len(entries), 1)

    def test_manual_conflict_does_not_apply_any_field(self) -> None:
        self.assertEqual(self.repository.manage(self.command(), "owner", self.now).status, "applied")
        conflict: Final = ManualCommand(
            command_id=self.account.id + "conflict",
            account_id=self.account.id,
            expected_version=0,
            changes=(
                ManualChange(field="name", action="set", value="Changed"),
                ManualChange(field="state", action="set", value="paused"),
            ),
        )
        result: Final = self.repository.manage(conflict, "owner", self.now)
        self.assertEqual((result.status, result.version), ("conflict", 1))
        view: Final = account_view(self.account, self.repository.state(), self.now, self.now.strftime("%Y-%m"))
        self.assertEqual((view.account.name, view.account.state), (self.account.name, self.account.state))

    def test_collector_update_preserves_manual_field_and_release_reveals_latest(self) -> None:
        self.assertEqual(self.repository.manage(self.command(), "owner", self.now).status, "applied")
        incoming: Final = self.account.model_copy(update={"plan": "Collector plan", "state": "active"})
        self.assertTrue(self.repository.import_batch(ImportBatch(accounts=(incoming,))).ok)
        view: Final = account_view(incoming, self.repository.state(), self.now, self.now.strftime("%Y-%m"))
        self.assertEqual((view.account.plan, view.account.state), ("Manual plan", "active"))
        release: Final = ManualCommand(
            command_id=self.account.id + "release",
            account_id=self.account.id,
            expected_version=1,
            changes=(ManualChange(field="plan", action="automatic"),),
        )
        self.assertEqual(self.repository.manage(release, "owner", self.now).status, "applied")
        released: Final = account_view(incoming, self.repository.state(), self.now, self.now.strftime("%Y-%m"))
        self.assertEqual(released.account.plan, "Collector plan")

    def test_concurrent_edit_has_one_winner(self) -> None:
        commands: Final = (self.command("a"), self.command("b"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results: Final = tuple(
                pool.map(lambda command: self.repository.manage(command, "owner", self.now), commands)
            )
        self.assertEqual(sorted(result.status for result in results), ["applied", "conflict"])

    def inventory_state(self, origin: str) -> GatewayInventoryState | None:
        return next(
            (item for item in self.repository.state().gateway_inventories if item.gateway_origin == origin), None
        )

    def record_collection(self, origin: str, at: datetime, observed_at: datetime, name: str) -> None:
        self.repository.record_gateway_inventory(
            origin,
            GatewayInventoryAttempt(attempted_at=at, status="collected", message="collected"),
            snapshot(
                origin, at, gateway_inventory(observed_at, credentials=(bound("runtime", name, self.account.id),))
            ),
        )

    def test_gateway_inventories_coexist_and_survive_a_new_repository(self) -> None:
        first: Final = f"http://gateway-{uuid4().hex}.test"
        second: Final = f"http://gateway-{uuid4().hex}.test"
        self.record_collection(first, self.now, self.now, "first")
        self.record_collection(second, self.now, self.now, "second")
        restarted: Final = PostgresRepository(DSN).state().gateway_inventories
        self.assertEqual(
            {
                item.gateway_origin: item.last_good.inventory.credentials[0].credential_name
                for item in restarted
                if item.gateway_origin in (first, second) and item.last_good is not None
            },
            {first: "first", second: "second"},
        )

    def test_a_failed_attempt_keeps_the_last_good_snapshot_and_reports_the_failure(self) -> None:
        origin: Final = f"http://gateway-{uuid4().hex}.test"
        self.record_collection(origin, self.now, self.now, "kept")
        failed_at: Final = self.now + timedelta(minutes=5)
        self.repository.record_gateway_inventory(
            origin, GatewayInventoryAttempt(attempted_at=failed_at, status="error", message="rejected"), None
        )
        state: Final = self.inventory_state(origin)
        assert state is not None and state.last_good is not None
        self.assertEqual(
            (
                state.attempt.status,
                state.attempt.attempted_at,
                state.last_good.inventory.credentials[0].credential_name,
            ),
            ("error", failed_at, "kept"),
        )

    def test_late_deliveries_never_replace_newer_attempts_or_newer_evidence(self) -> None:
        origin: Final = f"http://gateway-{uuid4().hex}.test"
        later: Final = self.now + timedelta(minutes=10)
        self.record_collection(origin, later, later, "newest")
        self.record_collection(origin, self.now, self.now, "late")
        after_late: Final = self.inventory_state(origin)
        self.record_collection(origin, later + timedelta(minutes=1), self.now, "cached")
        after_cached: Final = self.inventory_state(origin)
        assert after_late is not None and after_late.last_good is not None
        assert after_cached is not None and after_cached.last_good is not None
        self.assertEqual(
            [
                (state.attempt.attempted_at, state.last_good.inventory.credentials[0].credential_name)
                for state in (after_late, after_cached)
                if state.last_good is not None
            ],
            [(later, "newest"), (later + timedelta(minutes=1), "newest")],
        )
