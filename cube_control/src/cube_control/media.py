from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Final, Literal, Protocol

import psycopg
from psycopg import Connection
from pydantic import AwareDatetime, Field, ValidationError

from cube_control.models import Account, FrozenModel, Identifier, MediaJob

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS cube_control.job_commands (
    command_id text PRIMARY KEY, job_id text NOT NULL REFERENCES cube_control.jobs(id),
    revision bigint NOT NULL CHECK (revision > 0), payload jsonb NOT NULL, UNIQUE(job_id, revision)
);
"""


class JobCommand(FrozenModel):
    schema_version: Literal[1] = 1
    command_id: Identifier
    expected_version: Annotated[int, Field(ge=0, strict=True)]
    job: MediaJob


class JobEntry(FrozenModel):
    command: JobCommand
    previous: MediaJob | None
    revision: Annotated[int, Field(ge=1)]
    actor: Annotated[str, Field(min_length=1, max_length=100)]
    received_at: AwareDatetime


class JobSnapshot(FrozenModel):
    job: MediaJob
    version: int


class JobHistory(FrozenModel):
    account_id: Identifier
    job_id: Identifier
    entries: tuple[JobEntry, ...]
    next_after: int | None = None


class JobReader(Protocol):
    def get(self, account_id: str, job_id: str) -> JobSnapshot | None: ...
    def history(self, account_id: str, job_id: str, after: int = 0, limit: int = 50) -> JobHistory: ...


@dataclass(frozen=True, slots=True)
class JobResult:
    status: Literal["applied", "duplicate", "conflict", "rejected", "unavailable"]
    version: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class PostgresJobManager:
    dsn: str

    def connect(self) -> Connection[tuple[str, ...]]:
        return psycopg.connect(self.dsn, connect_timeout=5)

    def manage(self, command: JobCommand, actor: str, now: datetime) -> JobResult:
        try:
            with self.connect() as connection:
                connection.execute("SELECT pg_advisory_xact_lock(hashtext('cube-control-import'))")
                stored: Final = connection.execute(
                    "SELECT payload::text FROM cube_control.job_commands WHERE command_id=%s", (command.command_id,)
                ).fetchone()
                if stored:
                    previous_command: Final = JobEntry.model_validate_json(stored[0])
                    if previous_command.command == command and previous_command.actor == actor:
                        return JobResult("duplicate", previous_command.revision)
                    return JobResult("rejected", message="Command identity conflict; nothing changed")
                account_row: Final = connection.execute(
                    "SELECT payload::text FROM cube_control.accounts WHERE id=%s", (command.job.account_id,)
                ).fetchone()
                if account_row is None or Account.model_validate_json(account_row[0]).kind != "api":
                    return JobResult("rejected", message="An API account must be registered before managing jobs")
                job_row: Final = connection.execute(
                    "SELECT payload::text FROM cube_control.jobs WHERE id=%s", (command.job.id,)
                ).fetchone()
                previous: Final = MediaJob.model_validate_json(job_row[0]) if job_row else None
                version_row: Final = connection.execute(
                    "SELECT COALESCE(max(revision),0)::text FROM cube_control.job_commands WHERE job_id=%s",
                    (command.job.id,),
                ).fetchone()
                version: Final = int(version_row[0]) if version_row else 0
                if version != command.expected_version:
                    return JobResult("conflict", version, "Job was edited; refresh before applying changes")
                if previous is None and command.job.state not in ("reserved", "submitted"):
                    return JobResult("rejected", message="New jobs must start reserved or submitted")
                if previous is not None and (previous == command.job or not command.job.can_replace(previous)):
                    return JobResult("rejected", message="Invalid or unchanged job transition")
                entry: Final = JobEntry(
                    command=command, previous=previous, revision=version + 1, actor=actor, received_at=now
                )
                connection.execute(
                    "INSERT INTO cube_control.jobs(id,account_id,payload) VALUES (%s,%s,%s::jsonb) "
                    "ON CONFLICT(id) DO UPDATE SET payload=EXCLUDED.payload",
                    (command.job.id, command.job.account_id, command.job.model_dump_json()),
                )
                connection.execute(
                    "INSERT INTO cube_control.job_commands(command_id,job_id,revision,payload) "
                    "VALUES (%s,%s,%s,%s::jsonb)",
                    (command.command_id, command.job.id, entry.revision, entry.model_dump_json()),
                )
                return JobResult("applied", entry.revision)
        except ValidationError:
            return JobResult("rejected", message="Invalid management actor or clock")
        except psycopg.Error:
            return JobResult("unavailable", message="Job database unavailable; retry the same command ID")

    def get(self, account_id: str, job_id: str) -> JobSnapshot | None:
        with self.connect() as connection:
            row: Final = connection.execute(
                "SELECT jobs.payload::text, COALESCE((SELECT max(revision) FROM cube_control.job_commands "
                "WHERE job_id=jobs.id),0)::text FROM cube_control.jobs AS jobs WHERE jobs.id=%s",
                (sha256(f"{account_id}\0{job_id}".encode()).hexdigest(),),
            ).fetchone()
            return JobSnapshot(job=MediaJob.model_validate_json(row[0]), version=int(row[1])) if row else None

    def history(self, account_id: str, job_id: str, after: int = 0, limit: int = 50) -> JobHistory:
        if after < 0 or not 1 <= limit <= 200:
            raise ValueError("Invalid job history page")
        with self.connect() as connection:
            entries: Final = tuple(
                JobEntry.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT payload::text FROM cube_control.job_commands "
                    "WHERE job_id=%s AND revision>%s ORDER BY revision LIMIT %s",
                    (sha256(f"{account_id}\0{job_id}".encode()).hexdigest(), after, limit + 1),
                )
            )
            return JobHistory(
                account_id=account_id,
                job_id=job_id,
                entries=entries[:limit],
                next_after=entries[limit - 1].revision if len(entries) > limit else None,
            )
