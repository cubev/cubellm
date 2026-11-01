import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Final
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import Connection
from pydantic import TypeAdapter, ValidationError

from cube_control.api import create_app
from cube_control.credential_inventory import (
    CredentialImportAttempt,
    CredentialInventorySnapshot,
    CredentialInventoryState,
    from_legacy,
    merge_snapshot,
    parse_inventory,
    public_reference,
)
from cube_control.inventory import AccountInventoryReport, account_inventory
from cube_control.models import Account, ImportBatch, ManualCommand, Provider
from cube_control.store import ImportResult, ManualResult, PostgresRepository, StoredState

NOW: Final = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SCANNED: Final = NOW - timedelta(days=1)
LATER: Final = NOW - timedelta(hours=1)
DSN: Final = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")
PLAN: Final = TypeAdapter(dict[str, list[dict[str, object]]])
OPENAI_KEY: Final = "a1" * 10
LOOSE_KEY: Final = "b2" * 10
VOICE_KEY: Final = "c3" * 10
OLD_KEY: Final = "d4" * 10
COIN_KEY: Final = "e5" * 10
ROOT: Final = "/synthetic-root"
SECRET_PATH: Final = f"{ROOT}/project/.env"


def location(source_class: str = "current", path: str = SECRET_PATH) -> dict[str, object]:
    return {
        "host": "m5",
        "project": "synthetic-project",
        "variable": "SYNTHETIC_KEY",
        "path": path,
        "source_class": source_class,
    }


def plan_entry(
    fingerprint: str,
    service: str,
    source_class: str = "current",
    validity: str = "not_checked",
    locations: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": fingerprint,
        "service": service,
        "kind": ["api_key_or_token"],
        "validity": validity,
        "locations": [location(source_class)] if locations is None else locations,
    }


def service(name: str, fingerprints: tuple[str, ...], current: int = 1, archive: int = 0) -> dict[str, object]:
    return {
        "service": name,
        "status": "value_present",
        "hosts": ["m5"],
        "credential_ids": list(fingerprints),
        "current_value_locations": current,
        "archive_value_locations": archive,
    }


def sources(
    services: list[dict[str, object]],
    entries: list[dict[str, object]],
    unreadable: int = 0,
    scanned: datetime = SCANNED,
    roots: tuple[str, ...] = (ROOT,),
) -> tuple[bytes, bytes]:
    host: Final[dict[str, object]] = {
        "id": "m5",
        "roots": list(roots),
        "files_read": 10,
        "unreadable": unreadable,
        "size_skips": 0,
        "errors": [],
        "verification": {"errors": [], "secret_values_exported": False},
    }
    register: Final[dict[str, object]] = {
        "created": scanned.isoformat(),
        "hosts": [host],
        "services": services,
        "reused_credentials": [],
    }
    return json.dumps(register).encode(), json.dumps({"credentials": entries}).encode()


def standard(scanned: datetime = SCANNED) -> tuple[bytes, bytes]:
    return sources(
        [
            service("OpenAI", (OPENAI_KEY,)),
            service("Unzugeordnet", (LOOSE_KEY,)),
            service("ElevenLabs", (VOICE_KEY,)),
            service("Anthropic", (OLD_KEY,), current=0, archive=1),
            service("CoinGlass", (COIN_KEY,)),
        ],
        [
            plan_entry(OPENAI_KEY, "OpenAI"),
            plan_entry(LOOSE_KEY, "Unzugeordnet"),
            plan_entry(VOICE_KEY, "ElevenLabs"),
            plan_entry(OLD_KEY, "Anthropic", source_class="archive_or_export"),
            plan_entry(COIN_KEY, "CoinGlass"),
        ],
        scanned=scanned,
    )


def parsed(raw: tuple[bytes, bytes], source_id: str = "scan-a") -> CredentialInventorySnapshot:
    snapshot: Final = parse_inventory(raw[0], raw[1], source_id, NOW)
    assert isinstance(snapshot, CredentialInventorySnapshot), snapshot
    return snapshot


def state(snapshot: CredentialInventorySnapshot) -> CredentialInventoryState:
    return CredentialInventoryState(
        source_id=snapshot.source_id,
        attempt=CredentialImportAttempt(attempted_at=NOW, status="imported", scanned_at=snapshot.scanned_at),
        evidence=snapshot,
    )


def legacy_json(snapshot: CredentialInventorySnapshot) -> str:
    return snapshot.model_dump_json(
        exclude={
            "hosts": {"__all__": {"scanned_at", "presence", "roots"}},
            "candidates": {"__all__": {"locations": {"__all__": {"observed_at", "presence"}}}},
        }
    )


ACCOUNTS: Final = (
    Account(id="openai-main", provider=Provider.OPENAI, name="OpenAI main", kind="api"),
    Account(id="openai-second", provider=Provider.OPENAI, name="OpenAI second", kind="api"),
    Account(id="anthropic-api", provider=Provider.ANTHROPIC, name="Anthropic", kind="api"),
)


def stored_state(snapshots: tuple[CredentialInventorySnapshot, ...]) -> StoredState:
    return StoredState(ACCOUNTS, (), (), (), credential_inventories=tuple(map(state, snapshots)))


def report(snapshots: tuple[CredentialInventorySnapshot, ...]) -> AccountInventoryReport:
    return account_inventory(stored_state(snapshots), NOW)


def references(snapshot: CredentialInventorySnapshot) -> list[tuple[str, str]]:
    return sorted((item.fingerprint, item.locations[0].presence) for item in snapshot.candidates)


class CredentialCandidateTests(unittest.TestCase):
    def test_every_candidate_stays_unbound_with_one_finding_and_no_private_values(self) -> None:
        result: Final = report((parsed(standard()),))
        self.assertEqual({item.binding for item in result.credential_candidates}, {"unbound"})
        self.assertEqual(
            sorted((item.kind, item.reference) for item in result.credential_findings),
            sorted(
                [
                    ("unbound_candidate", public_reference("scan-a", OPENAI_KEY)),
                    ("unassigned_candidate", public_reference("scan-a", LOOSE_KEY)),
                    ("provider_hint_without_account", public_reference("scan-a", VOICE_KEY)),
                    ("archived_candidate", public_reference("scan-a", OLD_KEY)),
                    ("unbound_candidate", public_reference("scan-a", COIN_KEY)),
                ]
            ),
        )
        coin: Final = next(item for item in result.credential_findings if item.service == "CoinGlass")
        self.assertIn("Anbieter nicht zugeordnet", coin.detail)
        exposed: Final = result.model_dump_json()
        self.assertFalse(any(value in exposed for value in (OPENAI_KEY, LOOSE_KEY, COIN_KEY, SECRET_PATH, ROOT)))

    def test_a_same_provider_candidate_never_covers_either_of_two_accounts(self) -> None:
        def binding_gaps(snapshots: tuple[CredentialInventorySnapshot, ...]) -> dict[str, bool]:
            return {
                item.account_id: "no_confirmed_credential_binding" in item.gaps
                for item in report(snapshots).accounts
                if item.account_id in ("openai-main", "openai-second", "anthropic-api")
            }

        self.assertEqual(binding_gaps(()), dict.fromkeys(("anthropic-api", "openai-main", "openai-second"), False))
        self.assertEqual(
            binding_gaps((parsed(standard()),)), dict.fromkeys(("anthropic-api", "openai-main", "openai-second"), True)
        )

    def test_scan_freshness_follows_the_scan_time_not_the_import_time(self) -> None:
        result: Final = report((parsed(standard(scanned=NOW - timedelta(days=10))),))
        self.assertEqual(
            (
                result.credential_sources[0].scan_freshness,
                {item.scan_freshness for item in result.credential_candidates},
            ),
            ("stale", {"stale"}),
        )

    def test_inconsistent_and_location_less_rows_are_quarantined_and_the_api_still_answers(self) -> None:
        snapshot: Final = parsed(
            sources(
                [service("OpenAI", (OPENAI_KEY, LOOSE_KEY)), service("ElevenLabs", (VOICE_KEY, COIN_KEY))],
                [
                    plan_entry(OPENAI_KEY, "OpenAI"),
                    plan_entry(VOICE_KEY, "ElevenLabs"),
                    plan_entry(VOICE_KEY, "ElevenLabs"),
                    plan_entry(OLD_KEY, "Anthropic", validity="valid"),
                    plan_entry(COIN_KEY, "ElevenLabs", locations=[]),
                ],
            )
        )
        self.assertEqual(
            sorted((item.reason, item.fingerprint) for item in snapshot.quarantined),
            sorted(
                [
                    ("duplicate_candidate", VOICE_KEY),
                    ("register_only_candidate", LOOSE_KEY),
                    ("plan_only_candidate", OLD_KEY),
                    ("unexpected_validity", OLD_KEY),
                    ("candidate_without_locations", COIN_KEY),
                ]
            ),
        )
        self.assertEqual([item.fingerprint for item in snapshot.candidates], [OPENAI_KEY])

        @dataclass(frozen=True)
        class Fixed:
            def state(self) -> StoredState:
                return stored_state((snapshot,))

            def healthy(self) -> bool:
                return True

            def import_batch(self, batch: ImportBatch) -> ImportResult:
                return ImportResult(False)

            def manage(self, command: ManualCommand, actor: str, now: datetime) -> ManualResult:
                return ManualResult("rejected")

        response: Final = TestClient(create_app(Fixed())).get("/api/v1/inventory")
        self.assertEqual(response.status_code, 200, response.text)
        body: Final = AccountInventoryReport.model_validate_json(response.content)
        self.assertFalse(body.credential_sources[0].scan_complete)
        self.assertEqual(sum(1 for item in body.credential_findings if item.kind == "quarantined_record"), 5)

    def test_malformed_sources_are_rejected_without_echo(self) -> None:
        self.assertEqual(
            parse_inventory(b'{"created": "not-a-date", "echo-me": 1}', b"{}", "scan-a", NOW),
            "Credential inventory rejected; input values are not logged",
        )


class CredentialMergeTests(unittest.TestCase):
    def test_a_host_outage_keeps_earlier_candidates_with_their_original_time(self) -> None:
        merged: Final = merge_snapshot(parsed(standard()), parsed(sources([], [], unreadable=3, scanned=LATER)))
        self.assertEqual(len(merged.candidates), 5)
        self.assertEqual(
            {(location.presence, location.observed_at) for item in merged.candidates for location in item.locations},
            {("retained", SCANNED)},
        )

    def test_a_complete_scan_of_the_same_scope_clears_but_a_changed_scope_does_not(self) -> None:
        cleared: Final = merge_snapshot(parsed(standard()), parsed(sources([], [], scanned=LATER)))
        other_scope: Final = merge_snapshot(
            parsed(standard()), parsed(sources([], [], scanned=LATER, roots=("/different-synthetic-root",)))
        )
        self.assertEqual((len(cleared.candidates), len(other_scope.candidates)), (0, 5))

    def test_a_quarantined_candidate_is_retained_even_when_its_host_was_read_completely(self) -> None:
        register, plan = standard(scanned=LATER)
        entries: Final = PLAN.validate_json(plan)["credentials"]
        duplicated: Final = json.dumps({"credentials": [*entries, entries[0]]}).encode()
        merged: Final = merge_snapshot(parsed(standard()), parsed((register, duplicated)))
        self.assertEqual(len(merged.candidates), 5)
        openai: Final = next(item for item in merged.candidates if item.fingerprint == OPENAI_KEY)
        self.assertEqual([(item.presence, item.observed_at) for item in openai.locations], [("retained", SCANNED)])

    def test_older_or_equal_scans_never_replace_newer_evidence(self) -> None:
        newer: Final = parsed(standard())
        self.assertEqual(merge_snapshot(newer, parsed(sources([], [], scanned=SCANNED))), newer)
        self.assertEqual(merge_snapshot(newer, parsed(sources([], [], scanned=SCANNED - timedelta(days=1)))), newer)

    def test_legacy_snapshots_without_observation_times_load_with_an_unknown_scope(self) -> None:
        current: Final = parsed(standard())
        converted: Final = from_legacy(legacy_json(current))
        self.assertEqual(references(converted), references(current))
        self.assertEqual([host.scope_id for host in converted.hosts], ["scope-unknown"])
        self.assertFalse(report((converted,)).credential_sources[0].scan_complete)
        self.assertEqual(
            [
                len(merge_snapshot(converted, parsed(sources([], [], scanned=LATER, roots=roots))).candidates)
                for roots in (("/different-synthetic-root",), (ROOT,))
            ],
            [5, 0],
        )


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class CredentialInventoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = PostgresRepository(DSN)
        with self.repository.connect() as connection:
            database: Final = connection.execute("SELECT current_database()").fetchone()
        if database is None or database[0] != "cubecontrol_test":
            raise RuntimeError("Integration tests require the dedicated cubecontrol_test database")
        self.repository.initialize()
        self.repository.initialize()
        self.source = f"scan-{uuid4().hex}"

        def forget() -> None:
            with self.repository.connect() as connection:
                connection.execute("DELETE FROM cube_control.credential_inventories WHERE source_id=%s", (self.source,))

        self.addCleanup(forget)

    def stored(self) -> CredentialInventoryState:
        return next(
            item for item in PostgresRepository(DSN).state().credential_inventories if item.source_id == self.source
        )

    def test_partial_scans_retain_evidence_and_failed_imports_only_record_their_attempt(self) -> None:
        statuses: Final = [
            self.repository.record_credential_import(self.source, NOW, parsed(standard(), self.source)),
            self.repository.record_credential_import(
                self.source,
                NOW + timedelta(minutes=1),
                parsed(sources([], [], unreadable=2, scanned=LATER), self.source),
            ),
            self.repository.record_credential_import(
                self.source, NOW + timedelta(minutes=2), ("rejected", "bad input")
            ),
            self.repository.record_credential_import(
                self.source, NOW + timedelta(minutes=3), parsed(standard(), self.source)
            ),
        ]
        current: Final = self.stored()
        assert current.evidence is not None
        self.assertEqual(statuses, ["imported", "imported", "rejected", "older_or_equal_ignored"])
        self.assertEqual((current.attempt.status, current.evidence.scanned_at), ("older_or_equal_ignored", LATER))
        self.assertEqual(len(current.evidence.candidates), 5)
        self.assertEqual(
            {location.presence for item in current.evidence.candidates for location in item.locations}, {"retained"}
        )

    def test_invalid_or_mismatched_sources_never_reach_the_store(self) -> None:
        invalid: Final = "bad source id!"

        def forget_invalid() -> None:
            with self.repository.connect() as connection:
                connection.execute("DELETE FROM cube_control.credential_inventories WHERE source_id=%s", (invalid,))

        self.addCleanup(forget_invalid)
        with self.assertRaises(ValidationError):
            self.repository.record_credential_import(invalid, NOW, ("rejected", "x"))
        with self.assertRaises(ValueError):
            self.repository.record_credential_import(self.source, NOW, parsed(standard(), "another-source"))
        self.assertEqual(
            [item for item in self.repository.state().credential_inventories if item.source_id == self.source], []
        )

    def test_legacy_rows_written_before_evidence_columns_stay_readable(self) -> None:
        current: Final = parsed(standard(), self.source)
        with self.repository.connect() as connection:
            connection.execute(
                "INSERT INTO cube_control.credential_inventories (source_id, scanned_at, snapshot) VALUES (%s, %s, %s::jsonb)",
                (self.source, SCANNED, legacy_json(current)),
            )
        loaded: Final = self.stored()
        assert loaded.evidence is not None
        self.assertEqual((loaded.attempt.status, references(loaded.evidence)), ("imported", references(current)))

    def test_cli_imports_reject_bad_source_ids_and_record_unreadable_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            register_raw, plan_raw = standard()
            register: Final = Path(root) / "register.json"
            plan: Final = Path(root) / "plan.json"
            register.write_bytes(register_raw)
            plan.write_bytes(plan_raw)

            def run(source_id: str, register_path: Path) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "cube_control",
                        "import-credential-inventory",
                        "--source-id",
                        source_id,
                        "--register",
                        str(register_path),
                        "--plan",
                        str(plan),
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=60,
                    env={**os.environ, "CUBE_CONTROL_DATABASE_URL": DSN},
                )

            first: Final = run(self.source, register)
            again: Final = run(self.source, register)
            invalid: Final = run("bad source id!", register)
            missing: Final = run(self.source, Path(root) / "missing.json")
        self.assertEqual(
            [(result.returncode, json.loads(result.stdout)["status"]) for result in (first, again, missing)],
            [(0, "imported"), (0, "older_or_equal_ignored"), (2, "unreadable")],
            first.stderr,
        )
        self.assertEqual((invalid.returncode, invalid.stdout), (2, ""))
        self.assertEqual(self.stored().attempt.status, "unreadable")
        remaining: Final = self.stored().evidence
        assert remaining is not None
        self.assertEqual(len(remaining.candidates), 5)
        output: Final = "".join(result.stdout + result.stderr for result in (first, again, invalid, missing))
        self.assertFalse(any(value in output for value in (OPENAI_KEY, LOOSE_KEY, SECRET_PATH)))

    def test_a_failed_first_import_records_only_its_attempt_and_a_late_attempt_never_replaces_a_newer_one(self) -> None:
        first: Final = self.repository.record_credential_import(self.source, NOW, ("unreadable", "missing file"))
        empty: Final = self.stored()
        imported: Final = self.repository.record_credential_import(
            self.source, NOW + timedelta(minutes=2), parsed(standard(), self.source)
        )
        late: Final = self.repository.record_credential_import(
            self.source, NOW + timedelta(minutes=1), ("rejected", "late failure")
        )
        current: Final = self.stored()
        self.assertEqual((first, empty.evidence, imported, late), ("unreadable", None, "imported", "rejected"))
        self.assertEqual(
            (current.attempt.status, current.attempt.attempted_at, current.evidence is not None),
            ("imported", NOW + timedelta(minutes=2), True),
        )

    def test_sources_are_isolated_from_each_other(self) -> None:
        other: Final = f"scan-{uuid4().hex}"

        def forget_other() -> None:
            with self.repository.connect() as connection:
                connection.execute("DELETE FROM cube_control.credential_inventories WHERE source_id=%s", (other,))

        self.addCleanup(forget_other)
        self.repository.record_credential_import(self.source, NOW, parsed(standard(), self.source))
        self.repository.record_credential_import(other, NOW, parsed(sources([], [], scanned=LATER), other))
        states: Final = {item.source_id: item for item in self.repository.state().credential_inventories}
        mine: Final = states[self.source].evidence
        theirs: Final = states[other].evidence
        assert mine is not None and theirs is not None
        self.assertEqual((len(mine.candidates), len(theirs.candidates)), (5, 0))

    def test_a_first_import_waits_for_the_source_lock_before_reading_or_writing(self) -> None:
        def waiting(connection: Connection[tuple[str, ...]]) -> int:
            row: Final = connection.execute(
                "SELECT count(*)::text FROM pg_locks WHERE locktype='advisory' AND NOT granted AND objsubid=2 "
                "AND classid::bigint = (hashtext('cube-control-credentials')::bigint & 4294967295) "
                "AND objid::bigint = (hashtext(%s)::bigint & 4294967295)",
                (self.source,),
            ).fetchone()
            return int(row[0]) if row else 0

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.repository.connect() as holder:
                try:
                    holder.execute(
                        "SELECT pg_advisory_xact_lock(hashtext('cube-control-credentials'), hashtext(%s))",
                        (self.source,),
                    )
                    pending: Final = pool.submit(
                        self.repository.record_credential_import, self.source, NOW, parsed(standard(), self.source)
                    )
                    deadline: Final = monotonic() + 10
                    with self.repository.connect() as observer:
                        while waiting(observer) == 0 and monotonic() < deadline and not pending.done():
                            sleep(0.05)
                        blocked: Final = (
                            waiting(observer),
                            pending.done(),
                            [
                                item
                                for item in self.repository.state().credential_inventories
                                if item.source_id == self.source
                            ],
                        )
                finally:
                    holder.rollback()
            outcome: Final = pending.result(timeout=10)
        self.assertEqual((blocked, outcome), ((1, False, []), "imported"))

    def test_the_http_inventory_reads_a_cli_import_from_postgres_without_private_values(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            register_raw, plan_raw = standard()
            register: Final = Path(root) / "register.json"
            plan: Final = Path(root) / "plan.json"
            register.write_bytes(register_raw)
            plan.write_bytes(plan_raw)
            imported: Final = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cube_control",
                    "import-credential-inventory",
                    "--source-id",
                    self.source,
                    "--register",
                    str(register),
                    "--plan",
                    str(plan),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
                env={**os.environ, "CUBE_CONTROL_DATABASE_URL": DSN},
            )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        with TestClient(create_app(self.repository)) as client:
            response: Final = client.get("/api/v1/inventory")
        self.assertEqual(response.status_code, 200, response.text)
        body: Final = AccountInventoryReport.model_validate_json(response.content)
        mine: Final = [item for item in body.credential_candidates if item.source_id == self.source]
        self.assertEqual(
            sorted((item.service, item.binding) for item in mine),
            sorted((name, "unbound") for name in ("OpenAI", "Unzugeordnet", "ElevenLabs", "Anthropic", "CoinGlass")),
        )
        self.assertFalse(any(value in response.text for value in (OPENAI_KEY, LOOSE_KEY, SECRET_PATH, ROOT)))
