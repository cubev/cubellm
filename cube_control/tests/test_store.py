import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import uuid4

from psycopg import Connection

from threading import Barrier
from time import monotonic, sleep

from inventory_fixtures import bound, gateway_inventory, snapshot

from cube_control.gateway_inventory import (
    GatewayInventory,
    GatewayInventoryAttempt,
    GatewayInventorySnapshot,
    InventoryRoute,
)
from cube_control.inventory import account_inventory
from cube_control.inventory_evidence import GatewayInventoryState, merge_evidence
from cube_control.models import Account, ImportBatch, ManualChange, ManualCommand, Observation, Provider, UsageEvent
from cube_control.store import SCHEMA, PostgresRepository
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
                item.gateway_origin: item.evidence.credentials[0].credential.credential_name
                for item in restarted
                if item.gateway_origin in (first, second) and item.evidence is not None
            },
            {first: "first", second: "second"},
        )

    def test_a_failed_attempt_keeps_the_known_evidence_and_reports_the_failure(self) -> None:
        origin: Final = f"http://gateway-{uuid4().hex}.test"
        self.record_collection(origin, self.now, self.now, "kept")
        failed_at: Final = self.now + timedelta(minutes=5)
        self.repository.record_gateway_inventory(
            origin, GatewayInventoryAttempt(attempted_at=failed_at, status="error", message="rejected"), None
        )
        state: Final = self.inventory_state(origin)
        assert state is not None and state.evidence is not None
        self.assertEqual(
            (
                state.attempt.status,
                state.attempt.attempted_at,
                state.evidence.credentials[0].credential.credential_name,
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
        assert after_late is not None and after_late.evidence is not None
        assert after_cached is not None and after_cached.evidence is not None
        self.assertEqual(
            [
                (state.attempt.attempted_at, state.evidence.credentials[0].credential.credential_name)
                for state in (after_late, after_cached)
                if state.evidence is not None
            ],
            [(later, "newest"), (later + timedelta(minutes=1), "newest")],
        )

    def owned_origin(self) -> str:
        origin: Final = f"http://gateway-{uuid4().hex}.test"

        def forget() -> None:
            with self.repository.connect() as connection:
                connection.execute("DELETE FROM cube_control.gateway_inventories WHERE gateway_origin=%s", (origin,))

        self.addCleanup(forget)
        return origin

    def record_inventory(self, origin: str, inventory: GatewayInventory) -> None:
        self.repository.record_gateway_inventory(
            origin,
            GatewayInventoryAttempt(attempted_at=inventory.observed_at, status="collected", message="collected"),
            snapshot(origin, inventory.observed_at, inventory),
        )

    def known_then_partial(self, origin: str) -> None:
        self.record_inventory(
            origin, gateway_inventory(self.now, credentials=(bound("database", "kept", "previously-known"),))
        )
        self.record_inventory(
            origin,
            gateway_inventory(
                self.now + timedelta(minutes=1),
                routes=(InventoryRoute(origin="runtime", route_id="new", account_id="new-account", binding="account"),),
                unavailable=frozenset({("credentials", "database")}),
            ),
        )

    def test_a_scan_with_an_unavailable_source_keeps_previous_accounts_and_adds_new_ones(self) -> None:
        origin: Final = self.owned_origin()
        self.known_then_partial(origin)
        restarted: Final = PostgresRepository(DSN).state()
        state: Final = next(item for item in restarted.gateway_inventories if item.gateway_origin == origin)
        assert state.evidence is not None
        self.assertEqual(
            [(item.credential.credential_name, item.presence, item.observed_at) for item in state.evidence.credentials],
            [("kept", "retained", self.now)],
        )
        accounts: Final = {
            item.account_id: item
            for item in account_inventory(restarted, self.now + timedelta(minutes=2)).accounts
            if item.account_id in ("previously-known", "new-account")
        }
        self.assertEqual(
            {
                key: [(item.gateway_origin, item.presence) for item in (*value.credentials, *value.routes)]
                for key, value in accounts.items()
            },
            {"new-account": [(origin, "latest_collection")], "previously-known": [(origin, "retained")]},
        )

    def test_a_complete_empty_scan_clears_obsolete_bindings(self) -> None:
        origin: Final = self.owned_origin()
        self.known_then_partial(origin)
        self.record_inventory(origin, gateway_inventory(self.now + timedelta(minutes=2)))
        state: Final = self.inventory_state(origin)
        assert state is not None and state.evidence is not None
        self.assertEqual((state.evidence.credentials, state.evidence.routes), ((), ()))

    def test_legacy_snapshots_are_read_as_evidence_and_kept_under_later_partial_scans(self) -> None:
        origin: Final = self.owned_origin()
        legacy: Final = snapshot(
            origin, self.now, gateway_inventory(self.now, credentials=(bound("database", "legacy", "legacy-account"),))
        )
        attempt: Final = GatewayInventoryAttempt(attempted_at=self.now, status="collected", message="collected")
        with self.repository.connect() as connection:
            connection.execute(
                "INSERT INTO cube_control.gateway_inventories (gateway_origin, attempt, last_good) "
                "VALUES (%s, %s::jsonb, %s::jsonb)",
                (origin, attempt.model_dump_json(), legacy.model_dump_json()),
            )
        loaded: Final = self.inventory_state(origin)
        assert loaded is not None
        self.assertEqual(loaded.evidence, merge_evidence(None, legacy))
        self.record_inventory(
            origin,
            gateway_inventory(self.now + timedelta(minutes=1), unavailable=frozenset({("credentials", "database")})),
        )
        extended: Final = self.inventory_state(origin)
        assert extended is not None and extended.evidence is not None
        self.assertEqual(
            [(item.credential.credential_name, item.presence) for item in extended.evidence.credentials],
            [("legacy", "retained")],
        )
        with self.repository.connect() as connection:
            stored: Final = connection.execute(
                "SELECT last_good::text FROM cube_control.gateway_inventories WHERE gateway_origin=%s", (origin,)
            ).fetchone()
        self.assertEqual(stored and GatewayInventorySnapshot.model_validate_json(stored[0]), legacy)

    def test_the_schema_upgrade_adds_evidence_to_an_existing_inventory_table(self) -> None:
        with self.repository.connect() as connection:
            connection.execute("ALTER TABLE cube_control.gateway_inventories DROP COLUMN evidence")
            connection.execute(SCHEMA)
            columns: Final = connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='cube_control' AND table_name='gateway_inventories' ORDER BY ordinal_position"
            ).fetchall()
            connection.rollback()
        self.assertEqual([row[0] for row in columns], ["gateway_origin", "attempt", "last_good", "evidence"])

    def test_concurrent_collections_never_lose_retained_evidence(self) -> None:
        origin: Final = self.owned_origin()
        self.record_inventory(
            origin, gateway_inventory(self.now, credentials=(bound("database", "kept", "previously-known"),))
        )
        known: Final = self.inventory_state(origin)
        assert known is not None and known.evidence is not None
        earlier: Final = gateway_inventory(
            self.now + timedelta(minutes=1),
            credentials=(bound("runtime", "earlier", "earlier-account"),),
            unavailable=frozenset({("credentials", "database")}),
        )
        later: Final = gateway_inventory(
            self.now + timedelta(minutes=2),
            credentials=(bound("database", "kept", "previously-known"),),
            unavailable=frozenset({("credentials", "runtime")}),
        )
        start: Final = Barrier(2)

        def record(inventory: GatewayInventory) -> None:
            start.wait()
            self.record_inventory(origin, inventory)

        with ThreadPoolExecutor(max_workers=2) as pool:
            tuple(pool.map(record, (earlier, later)))
        final: Final = self.inventory_state(origin)
        assert final is not None
        serial: Final = (
            merge_evidence(
                merge_evidence(known.evidence, snapshot(origin, earlier.observed_at, earlier)),
                snapshot(origin, later.observed_at, later),
            ),
            merge_evidence(known.evidence, snapshot(origin, later.observed_at, later)),
        )
        self.assertIn(final.evidence, serial)

    def test_a_first_collection_waits_for_the_origin_lock_before_reading_or_writing(self) -> None:
        origin: Final = self.owned_origin()

        def waiting_writers(connection: Connection[tuple[str, ...]]) -> int:
            row: Final = connection.execute(
                "SELECT count(*)::text FROM pg_locks WHERE locktype='advisory' AND NOT granted AND objsubid=2 "
                "AND classid::bigint = (hashtext('cube-control-inventory')::bigint & 4294967295) "
                "AND objid::bigint = (hashtext(%s)::bigint & 4294967295)",
                (origin,),
            ).fetchone()
            return int(row[0]) if row else 0

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.repository.connect() as holder:
                try:
                    holder.execute(
                        "SELECT pg_advisory_xact_lock(hashtext('cube-control-inventory'), hashtext(%s))", (origin,)
                    )
                    pending: Final = pool.submit(
                        self.record_inventory,
                        origin,
                        gateway_inventory(self.now, credentials=(bound("database", "kept", "previously-known"),)),
                    )
                    deadline: Final = monotonic() + 10
                    with self.repository.connect() as observer:
                        while waiting_writers(observer) == 0 and monotonic() < deadline and not pending.done():
                            sleep(0.05)
                        blocked: Final = (waiting_writers(observer), pending.done(), self.inventory_state(origin))
                finally:
                    holder.rollback()
            pending.result(timeout=10)
        self.assertEqual(blocked, (1, False, None))
        self.assertIsNotNone(self.inventory_state(origin))
