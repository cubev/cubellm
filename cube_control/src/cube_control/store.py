from dataclasses import dataclass
from typing import Final, Protocol

import psycopg
from psycopg import Connection

from cube_control.models import CATALOG, Account, FleetSnapshot, ImportBatch, MediaJob, Observation, UsageEvent


@dataclass(frozen=True, slots=True)
class ImportResult:
    ok: bool
    inserted: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class StoredState:
    accounts: tuple[Account, ...]
    observations: tuple[Observation, ...]
    events: tuple[UsageEvent, ...]
    jobs: tuple[MediaJob, ...]
    fleet: FleetSnapshot | None = None


class Repository(Protocol):
    def state(self) -> StoredState: ...
    def healthy(self) -> bool: ...
    def import_batch(self, batch: ImportBatch) -> ImportResult: ...


SCHEMA: Final[str] = """
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
CREATE TABLE IF NOT EXISTS cube_control.fleet (id text PRIMARY KEY CHECK (id = 'current'), payload jsonb NOT NULL);
"""


@dataclass(frozen=True, slots=True)
class PostgresRepository:
    dsn: str

    def connect(self) -> Connection[tuple[str, ...]]:
        return psycopg.connect(self.dsn, connect_timeout=5)

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(SCHEMA)
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
            return StoredState(
                accounts,
                observations,
                events,
                jobs,
                FleetSnapshot.model_validate_json(fleet_row[0]) if fleet_row else None,
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
