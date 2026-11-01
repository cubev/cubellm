from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, LiteralString, Protocol

import psycopg
from psycopg import Connection
from pydantic import TypeAdapter, ValidationError

from cube_control.gateway_inventory import GatewayInventoryAttempt, GatewayInventorySnapshot
from cube_control.credential_inventory import (
    CredentialImportAttempt,
    CredentialInventorySnapshot,
    CredentialInventoryState,
    ImportStatus,
    from_legacy,
    merge_snapshot,
)
from cube_control.inventory_evidence import GatewayEvidence, GatewayInventoryState, merge_evidence
from cube_control.gateway_store import SCHEMA as GATEWAY_SCHEMA
from cube_control.gateway_store import current_records, latest_scans
from cube_control.media import SCHEMA as MEDIA_SCHEMA
from cube_control.models import (
    CATALOG,
    Account,
    FleetSnapshot,
    GatewayRecord,
    GatewayScanView,
    Identifier,
    ImportBatch,
    ManualCommand,
    ManualEntry,
    MediaJob,
    Observation,
    UsageEvent,
)


@dataclass(frozen=True, slots=True)
class ImportResult:
    ok: bool
    inserted: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class ManualResult:
    status: Literal["applied", "duplicate", "conflict", "rejected", "unavailable"]
    version: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class StoredState:
    accounts: tuple[Account, ...]
    observations: tuple[Observation, ...]
    events: tuple[UsageEvent, ...]
    jobs: tuple[MediaJob, ...]
    fleet: FleetSnapshot | None = None
    manual_entries: tuple[ManualEntry, ...] = ()
    gateway_records: tuple[GatewayRecord, ...] = ()
    gateway_scans: tuple[GatewayScanView, ...] = ()
    gateway_inventories: tuple[GatewayInventoryState, ...] = ()
    credential_inventories: tuple[CredentialInventoryState, ...] = ()


INVENTORY_ROW: Final = TypeAdapter(tuple[str, str, str | None, str | None])
INVENTORY_COLUMNS: Final = "gateway_origin, attempt::text, last_good::text, evidence::text"


CREDENTIAL_ROW: Final = TypeAdapter(tuple[str, str | None, str | None, str | None])
SOURCE_ID: Final = TypeAdapter[str](Identifier)
CREDENTIAL_COLUMNS: Final = "source_id, attempt::text, evidence::text, snapshot::text"


def credential_outcome(
    previous: CredentialInventorySnapshot | None,
    attempted_at: datetime,
    outcome: CredentialInventorySnapshot | tuple[ImportStatus, str],
) -> tuple[CredentialInventorySnapshot | None, CredentialImportAttempt]:
    if not isinstance(outcome, CredentialInventorySnapshot):
        return previous, CredentialImportAttempt(attempted_at=attempted_at, status=outcome[0], message=outcome[1])
    newer: Final = previous is None or outcome.scanned_at > previous.scanned_at
    return merge_snapshot(previous, outcome), CredentialImportAttempt(
        attempted_at=attempted_at,
        status="imported" if newer else "older_or_equal_ignored",
        scanned_at=outcome.scanned_at,
    )


def credential_state(row: object) -> CredentialInventoryState:
    source_id, attempt, evidence, legacy = CREDENTIAL_ROW.validate_python(row)
    snapshot: Final = (
        CredentialInventorySnapshot.model_validate_json(evidence)
        if evidence is not None
        else from_legacy(legacy)
        if legacy is not None
        else None
    )
    return CredentialInventoryState(
        source_id=source_id,
        attempt=CredentialImportAttempt.model_validate_json(attempt)
        if attempt is not None
        else CredentialImportAttempt(
            attempted_at=snapshot.imported_at if snapshot is not None else datetime.min.replace(tzinfo=UTC),
            status="imported",
            scanned_at=snapshot.scanned_at if snapshot is not None else None,
        ),
        evidence=snapshot,
    )


def inventory_state(row: object) -> GatewayInventoryState:
    origin, attempt, last_good, evidence = INVENTORY_ROW.validate_python(row)
    return GatewayInventoryState(
        gateway_origin=origin,
        attempt=GatewayInventoryAttempt.model_validate_json(attempt),
        evidence=GatewayEvidence.model_validate_json(evidence)
        if evidence is not None
        else merge_evidence(None, GatewayInventorySnapshot.model_validate_json(last_good))
        if last_good is not None
        else None,
    )


class Repository(Protocol):
    def state(self) -> StoredState: ...
    def healthy(self) -> bool: ...
    def import_batch(self, batch: ImportBatch) -> ImportResult: ...
    def manage(self, command: ManualCommand, actor: str, now: datetime) -> ManualResult: ...


SCHEMA: Final[LiteralString] = """
CREATE SCHEMA IF NOT EXISTS cube_control;
CREATE TABLE IF NOT EXISTS cube_control.accounts (id text PRIMARY KEY, payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS cube_control.observations (
    id text PRIMARY KEY, account_id text NOT NULL REFERENCES cube_control.accounts(id),
    payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cube_control.events (
    id text PRIMARY KEY, account_id text NOT NULL REFERENCES cube_control.accounts(id),
    payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS cube_control.jobs (
    id text PRIMARY KEY, account_id text NOT NULL REFERENCES cube_control.accounts(id), payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS cube_control.manual_commands (
    command_id text PRIMARY KEY, account_id text NOT NULL REFERENCES cube_control.accounts(id),
    revision bigint NOT NULL, payload jsonb NOT NULL, UNIQUE(account_id, revision)
);
CREATE TABLE IF NOT EXISTS cube_control.fleet (id text PRIMARY KEY CHECK (id = 'current'), payload jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS cube_control.gateway_inventories (
    gateway_origin text PRIMARY KEY, attempt jsonb NOT NULL, last_good jsonb
);
ALTER TABLE cube_control.gateway_inventories ADD COLUMN IF NOT EXISTS evidence jsonb;
CREATE TABLE IF NOT EXISTS cube_control.credential_inventories (
    source_id text PRIMARY KEY, scanned_at timestamptz NOT NULL, snapshot jsonb NOT NULL
);
ALTER TABLE cube_control.credential_inventories ADD COLUMN IF NOT EXISTS attempt jsonb;
ALTER TABLE cube_control.credential_inventories ADD COLUMN IF NOT EXISTS evidence jsonb;
ALTER TABLE cube_control.credential_inventories ALTER COLUMN snapshot DROP NOT NULL;
ALTER TABLE cube_control.credential_inventories ALTER COLUMN scanned_at DROP NOT NULL;
"""


@dataclass(frozen=True, slots=True)
class PostgresRepository:
    dsn: str

    def connect(self) -> Connection[tuple[str, ...]]:
        return psycopg.connect(self.dsn, connect_timeout=5)

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(SCHEMA)
            connection.execute(GATEWAY_SCHEMA)
            connection.execute(MEDIA_SCHEMA)
            for account in CATALOG:
                connection.execute(
                    "INSERT INTO cube_control.accounts (id, payload) VALUES (%s, %s::jsonb) ON CONFLICT DO NOTHING",
                    (account.id, account.model_dump_json()),
                )

    def healthy(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1 FROM cube_control.accounts LIMIT 1")
                return True
        except psycopg.Error:
            return False

    def state(self) -> StoredState:
        with self.connect() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            accounts: Final[tuple[Account, ...]] = tuple(
                Account.model_validate_json(row[0])
                for row in connection.execute("SELECT payload::text FROM cube_control.accounts ORDER BY id")
            )
            observations: Final[tuple[Observation, ...]] = tuple(
                Observation.model_validate_json(row[0])
                for row in connection.execute("SELECT payload::text FROM cube_control.observations ORDER BY created_at")
            )
            events: Final[tuple[UsageEvent, ...]] = tuple(
                UsageEvent.model_validate_json(row[0])
                for row in connection.execute("SELECT payload::text FROM cube_control.events ORDER BY created_at")
            )
            jobs: Final[tuple[MediaJob, ...]] = tuple(
                MediaJob.model_validate_json(row[0])
                for row in connection.execute("SELECT payload::text FROM cube_control.jobs")
            )
            fleet_row: Final[tuple[str, ...] | None] = connection.execute(
                "SELECT payload::text FROM cube_control.fleet WHERE id='current'"
            ).fetchone()
            inventories: Final[tuple[GatewayInventoryState, ...]] = tuple(
                map(
                    inventory_state,
                    connection.execute(
                        f"SELECT {INVENTORY_COLUMNS} FROM cube_control.gateway_inventories ORDER BY gateway_origin"
                    ),
                )
            )
            manual_entries: Final[tuple[ManualEntry, ...]] = tuple(
                ManualEntry.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT payload::text FROM cube_control.manual_commands ORDER BY account_id, revision"
                )
            )
            return StoredState(
                accounts,
                observations,
                events,
                jobs,
                FleetSnapshot.model_validate_json(fleet_row[0]) if fleet_row else None,
                manual_entries,
                current_records(connection),
                latest_scans(connection),
                inventories,
                tuple(
                    map(
                        credential_state,
                        connection.execute(
                            f"SELECT {CREDENTIAL_COLUMNS} FROM cube_control.credential_inventories ORDER BY source_id"
                        ),
                    )
                ),
            )

    def record_credential_import(
        self,
        source_id: str,
        attempted_at: datetime,
        outcome: CredentialInventorySnapshot | tuple[ImportStatus, str],
    ) -> ImportStatus:
        SOURCE_ID.validate_python(source_id)
        if isinstance(outcome, CredentialInventorySnapshot) and outcome.source_id != source_id:
            raise ValueError("Credential inventory snapshot belongs to another source")
        with self.connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('cube-control-credentials'), hashtext(%s))", (source_id,)
            )
            row: Final = connection.execute(
                f"SELECT {CREDENTIAL_COLUMNS} FROM cube_control.credential_inventories WHERE source_id=%s FOR UPDATE",
                (source_id,),
            ).fetchone()
            stored: Final = credential_state(row) if row is not None else None
            previous: Final = stored.evidence if stored is not None else None
            evidence, attempt = credential_outcome(previous, attempted_at, outcome)
            newest: Final = (
                attempt if stored is None or attempt.attempted_at >= stored.attempt.attempted_at else stored.attempt
            )
            connection.execute(
                "INSERT INTO cube_control.credential_inventories (source_id, scanned_at, attempt, evidence) "
                "VALUES (%s, %s, %s::jsonb, %s::jsonb) ON CONFLICT (source_id) DO UPDATE "
                "SET scanned_at = EXCLUDED.scanned_at, attempt = EXCLUDED.attempt, evidence = EXCLUDED.evidence",
                (
                    source_id,
                    evidence.scanned_at if evidence is not None else None,
                    newest.model_dump_json(),
                    evidence.model_dump_json() if evidence is not None else None,
                ),
            )
            return attempt.status

    def record_gateway_inventory(
        self, gateway_origin: str, attempt: GatewayInventoryAttempt, snapshot: GatewayInventorySnapshot | None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('cube-control-inventory'), hashtext(%s))", (gateway_origin,)
            )
            row: Final = connection.execute(
                f"SELECT {INVENTORY_COLUMNS} FROM cube_control.gateway_inventories WHERE gateway_origin=%s FOR UPDATE",
                (gateway_origin,),
            ).fetchone()
            stored: Final = inventory_state(row) if row is not None else None
            newest_attempt: Final = (
                attempt if stored is None or attempt.attempted_at > stored.attempt.attempted_at else stored.attempt
            )
            previous: Final = stored.evidence if stored is not None else None
            evidence: Final = merge_evidence(previous, snapshot) if snapshot is not None else previous
            connection.execute(
                "INSERT INTO cube_control.gateway_inventories (gateway_origin, attempt, evidence) "
                "VALUES (%s, %s::jsonb, %s::jsonb) ON CONFLICT (gateway_origin) DO UPDATE "
                "SET attempt = EXCLUDED.attempt, evidence = EXCLUDED.evidence",
                (
                    gateway_origin,
                    newest_attempt.model_dump_json(),
                    evidence.model_dump_json() if evidence is not None else None,
                ),
            )

    def import_batch(self, batch: ImportBatch) -> ImportResult:
        try:
            with self.connect() as connection:
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('cube-control-import'))")
                for account in batch.accounts:
                    previous_row: Final[tuple[str, ...] | None] = connection.execute(
                        "SELECT payload::text FROM cube_control.accounts WHERE id=%s", (account.id,)
                    ).fetchone()
                    previous: Final[Account | None] = (
                        Account.model_validate_json(previous_row[0]) if previous_row else None
                    )
                    if previous and (previous.provider != account.provider or previous.kind != account.kind):
                        connection.rollback()
                        return ImportResult(False, message="Account identity cannot change provider or account kind")
                    connection.execute(
                        "INSERT INTO cube_control.accounts (id,payload) VALUES (%s,%s::jsonb) "
                        "ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload",
                        (account.id, account.model_dump_json()),
                    )
                before: Final[int] = sum(
                    int(row[0])
                    for row in connection.execute(
                        "SELECT (SELECT count(*) FROM cube_control.observations) + "
                        "(SELECT count(*) FROM cube_control.events)"
                    )
                )
                for observation in batch.observations:
                    stored_observation: Final[tuple[str, ...] | None] = connection.execute(
                        "SELECT payload::text FROM cube_control.observations WHERE id=%s", (observation.id,)
                    ).fetchone()
                    if stored_observation and Observation.model_validate_json(stored_observation[0]) != observation:
                        connection.rollback()
                        return ImportResult(False, message="Observation identity conflict")
                    connection.execute(
                        "INSERT INTO cube_control.observations(id,account_id,payload) VALUES (%s,%s,%s::jsonb) "
                        "ON CONFLICT DO NOTHING",
                        (observation.id, observation.account_id, observation.model_dump_json()),
                    )
                for event in batch.events:
                    stored_event: Final[tuple[str, ...] | None] = connection.execute(
                        "SELECT payload::text FROM cube_control.events WHERE id=%s", (event.id,)
                    ).fetchone()
                    if stored_event and UsageEvent.model_validate_json(stored_event[0]) != event:
                        connection.rollback()
                        return ImportResult(False, message="Usage event identity conflict")
                    connection.execute(
                        "INSERT INTO cube_control.events(id,account_id,payload) VALUES (%s,%s,%s::jsonb) "
                        "ON CONFLICT DO NOTHING",
                        (event.id, event.account_id, event.model_dump_json()),
                    )
                for job in batch.jobs:
                    stored_job: Final[tuple[str, ...] | None] = connection.execute(
                        "SELECT payload::text FROM cube_control.jobs WHERE id=%s", (job.id,)
                    ).fetchone()
                    previous_job: Final[MediaJob | None] = (
                        MediaJob.model_validate_json(stored_job[0]) if stored_job else None
                    )
                    if (
                        job != previous_job
                        and connection.execute(
                            "SELECT command_id FROM cube_control.job_commands WHERE job_id=%s LIMIT 1", (job.id,)
                        ).fetchone()
                        is not None
                    ):
                        connection.rollback()
                        return ImportResult(False, message="Managed jobs require a versioned command")
                    if previous_job and not job.can_replace(previous_job):
                        connection.rollback()
                        return ImportResult(False, message="Invalid media job transition")
                    if previous_job is None and job.state not in ("reserved", "submitted"):
                        connection.rollback()
                        return ImportResult(False, message="New media jobs must start reserved or submitted")
                    connection.execute(
                        "INSERT INTO cube_control.jobs(id,account_id,payload) VALUES (%s,%s,%s::jsonb) "
                        "ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload",
                        (job.id, job.account_id, job.model_dump_json()),
                    )
                if batch.fleet is not None:
                    connection.execute(
                        "INSERT INTO cube_control.fleet(id,payload) VALUES ('current',%s::jsonb) "
                        "ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload "
                        "WHERE (cube_control.fleet.payload->>'checked_at')::timestamptz "
                        "<= (EXCLUDED.payload->>'checked_at')::timestamptz",
                        (batch.fleet.model_dump_json(),),
                    )
                after: Final[int] = sum(
                    int(row[0])
                    for row in connection.execute(
                        "SELECT (SELECT count(*) FROM cube_control.observations) + "
                        "(SELECT count(*) FROM cube_control.events)"
                    )
                )
                return ImportResult(True, after - before)
        except psycopg.Error:
            return ImportResult(False, message="Database rejected the import; nothing was changed")

    def manage(self, command: ManualCommand, actor: str, now: datetime) -> ManualResult:
        try:
            with self.connect() as connection:
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('cube-control-import'))")
                stored: Final = connection.execute(
                    "SELECT payload::text FROM cube_control.manual_commands WHERE command_id=%s", (command.command_id,)
                ).fetchone()
                if stored:
                    previous: Final = ManualEntry.model_validate_json(stored[0])
                    if previous.command == command and previous.actor == actor:
                        return ManualResult("duplicate", previous.revision)
                    return ManualResult("rejected", message="Command identity conflict; nothing changed")
                account: Final = connection.execute(
                    "SELECT id FROM cube_control.accounts WHERE id=%s", (command.account_id,)
                ).fetchone()
                if command.create_account is not None and account is not None:
                    return ManualResult("rejected", message="Account ID already registered; nothing changed")
                if command.create_account is None and account is None:
                    return ManualResult("rejected", message="Account is not registered")
                row: Final = connection.execute(
                    "SELECT COALESCE(max(revision),0)::text FROM cube_control.manual_commands WHERE account_id=%s",
                    (command.account_id,),
                ).fetchone()
                version: Final[int] = int(row[0]) if row else 0
                if version != command.expected_version:
                    return ManualResult("conflict", version, "Account was edited; refresh before applying changes")
                entry: Final = ManualEntry(command=command, revision=version + 1, actor=actor, received_at=now)
                if command.create_account is not None:
                    connection.execute(
                        "INSERT INTO cube_control.accounts(id,payload) VALUES (%s,%s::jsonb)",
                        (command.account_id, command.create_account.model_dump_json()),
                    )
                connection.execute(
                    "INSERT INTO cube_control.manual_commands(command_id,account_id,revision,payload) "
                    "VALUES (%s,%s,%s,%s::jsonb)",
                    (command.command_id, command.account_id, entry.revision, entry.model_dump_json(exclude_none=True)),
                )
                return ManualResult("applied", entry.revision)
        except ValidationError:
            return ManualResult("rejected", message="Invalid management actor or clock")
        except psycopg.Error:
            return ManualResult("unavailable", message="Management database unavailable; retry the same command ID")
