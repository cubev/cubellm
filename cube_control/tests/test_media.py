import os
import json
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import uuid4

from fastapi.testclient import TestClient

from cube_control.api import create_app
from cube_control.media import JobCommand, PostgresJobManager
from cube_control.models import Account, ImportBatch, MediaJob
from cube_control.store import PostgresRepository
from cube_control.summary import reservations

DSN: Final = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository = PostgresRepository(DSN)
        with cls.repository.connect() as connection:
            row: Final = connection.execute("SELECT current_database()").fetchone()
            if row is None or row[0] != "cubecontrol_test":
                raise RuntimeError("Media tests require the dedicated cubecontrol_test database")
        cls.repository.initialize()
        cls.manager = PostgresJobManager(DSN)

    def setUp(self) -> None:
        self.now = datetime.now(UTC)
        self.account = Account(id="media-" + uuid4().hex, provider="heygen", name="Media fixture", kind="api")
        self.assertTrue(self.repository.import_batch(ImportBatch(accounts=(self.account,))).ok)
        self.job = MediaJob(
            account_id=self.account.id,
            job_id="fixture",
            state="reserved",
            updated_at=self.now,
            reserved_amount=Decimal("2.000000000001"),
            unit="credits",
            expires_at=self.now + timedelta(minutes=5),
        )

    def command(self, job: MediaJob, version: int = 0) -> JobCommand:
        return JobCommand(command_id=uuid4().hex, expected_version=version, job=job)

    def test_retries_preserve_exact_history_and_unknown_outcome_hold(self) -> None:
        reserve: Final = self.command(self.job)
        self.assertEqual(self.manager.manage(reserve, "owner", self.now).status, "applied")
        self.assertEqual(self.manager.manage(reserve, "owner", self.now + timedelta(minutes=1)).status, "duplicate")
        self.assertEqual(self.manager.manage(reserve, "another-owner", self.now).status, "rejected")
        submitted: Final = self.job.model_copy(update={"state": "submitted"})
        self.assertEqual(self.manager.manage(self.command(submitted, 1), "owner", self.now).status, "applied")
        history: Final = self.manager.history(self.account.id, self.job.job_id).entries
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[0].previous)
        self.assertEqual(history[1].previous, self.job)
        self.assertEqual(history[1].command.job, submitted)
        self.assertEqual(history[0].received_at, self.now)
        self.assertEqual(
            reservations((submitted,), self.account.id, self.now + timedelta(days=1)),
            {"credits": self.job.reserved_amount},
        )
        expired: Final = submitted.model_copy(update={"state": "expired", "updated_at": self.job.expires_at})
        self.assertEqual(self.manager.manage(self.command(expired, 2), "owner", self.now).status, "rejected")
        finalized: Final = submitted.model_copy(update={"state": "finalized", "actual_amount": Decimal("1.123456789")})
        self.assertEqual(self.manager.manage(self.command(finalized, 2), "owner", self.now).status, "applied")
        self.assertEqual(reservations((finalized,), self.account.id, self.now), {})
        self.assertEqual(self.manager.history(self.account.id, "other").entries, ())
        self.assertEqual(self.manager.history("other", self.job.job_id).entries, ())

    def test_concurrent_edits_have_one_winner_and_no_lost_history(self) -> None:
        self.assertEqual(self.manager.manage(self.command(self.job), "owner", self.now).status, "applied")
        commands: Final = tuple(
            self.command(self.job.model_copy(update={"state": state}), 1) for state in ("submitted", "failed")
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results: Final = tuple(pool.map(lambda command: self.manager.manage(command, "owner", self.now), commands))
        self.assertEqual(sorted(result.status for result in results), ["applied", "conflict"])
        self.assertEqual(
            tuple(entry.revision for entry in self.manager.history(self.account.id, self.job.job_id).entries), (1, 2)
        )

    def test_import_cannot_overwrite_managed_job_and_rolls_back_whole_batch(self) -> None:
        self.assertEqual(self.manager.manage(self.command(self.job), "owner", self.now).status, "applied")
        other: Final = self.job.model_copy(update={"job_id": "must-rollback"})
        changed: Final = self.job.model_copy(update={"state": "submitted"})
        self.assertFalse(self.repository.import_batch(ImportBatch(jobs=(other, changed))).ok)
        relevant: Final = tuple(job for job in self.repository.state().jobs if job.account_id == self.account.id)
        self.assertEqual(relevant, (self.job,))
        self.assertTrue(self.repository.import_batch(ImportBatch(jobs=(self.job,))).ok)
        self.assertEqual(len(self.manager.history(self.account.id, self.job.job_id).entries), 1)

    def test_imported_baseline_is_captured_before_first_managed_change(self) -> None:
        self.assertTrue(self.repository.import_batch(ImportBatch(jobs=(self.job,))).ok)
        submitted: Final = self.job.model_copy(update={"state": "submitted"})
        self.assertEqual(self.manager.manage(self.command(submitted), "owner", self.now).status, "applied")
        self.assertEqual(self.manager.history(self.account.id, self.job.job_id).entries[0].previous, self.job)

    def test_rejected_actor_and_identity_never_leave_partial_changes(self) -> None:
        command: Final = self.command(self.job)
        self.assertEqual(self.manager.manage(command, "", self.now).status, "rejected")
        self.assertEqual(self.manager.history(self.account.id, self.job.job_id).entries, ())
        self.assertFalse(any(job.id == self.job.id for job in self.repository.state().jobs))
        self.assertEqual(self.manager.manage(command, "owner", self.now).status, "applied")
        changed: Final = command.model_copy(update={"job": self.job.model_copy(update={"state": "submitted"})})
        self.assertEqual(self.manager.manage(changed, "owner", self.now).status, "rejected")
        missing: Final = self.job.model_copy(update={"account_id": "unregistered"})
        self.assertEqual(self.manager.manage(self.command(missing), "owner", self.now).status, "rejected")

    def test_expiry_is_only_for_unsubmitted_reservations_after_deadline(self) -> None:
        self.assertEqual(self.manager.manage(self.command(self.job), "owner", self.now).status, "applied")
        premature: Final = self.job.model_copy(update={"state": "expired"})
        self.assertEqual(self.manager.manage(self.command(premature, 1), "owner", self.now).status, "rejected")
        expired: Final = premature.model_copy(update={"updated_at": self.job.expires_at})
        self.assertEqual(self.manager.manage(self.command(expired, 1), "owner", self.job.expires_at).status, "applied")
        self.assertEqual(self.manager.manage(self.command(self.job, 2), "owner", self.now).status, "rejected")

    def test_read_api_preserves_precision_pages_history_and_never_accepts_writes(self) -> None:
        self.assertEqual(self.manager.manage(self.command(self.job), "owner", self.now).status, "applied")
        submitted: Final = self.job.model_copy(update={"state": "submitted"})
        self.assertEqual(self.manager.manage(self.command(submitted, 1), "owner", self.now).status, "applied")
        client: Final = TestClient(create_app(self.repository, job_reader=self.manager))
        path: Final = f"/api/v1/accounts/{self.account.id}/jobs/{self.job.job_id}"
        snapshot: Final = client.get(path)
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["version"], 2)
        self.assertEqual(snapshot.json()["job"]["reserved_amount"], str(self.job.reserved_amount))
        first: Final = client.get(path + "/history?limit=1").json()
        self.assertEqual([row["revision"] for row in first["entries"]], [1])
        self.assertEqual(first["next_after"], 1)
        second: Final = client.get(path + "/history?limit=1&after=1").json()
        self.assertEqual([row["revision"] for row in second["entries"]], [2])
        self.assertIsNone(second["next_after"])
        self.assertEqual(client.get(path + "/history?limit=201").status_code, 422)
        self.assertEqual(client.get(path + "/history?after=-1").status_code, 422)
        self.assertEqual(client.post(path, json={"state": "failed"}).status_code, 405)
        self.assertEqual(client.get(path.replace(self.account.id, "missing")).status_code, 404)
        unavailable: Final = TestClient(create_app(self.repository))
        self.assertEqual(unavailable.get(path).status_code, 503)
        self.assertEqual(unavailable.get(path + "/history").status_code, 503)

    def test_cli_retry_and_history_use_same_persisted_command(self) -> None:
        command: Final = self.command(self.job)
        environment: Final = dict(os.environ) | {"CUBE_CONTROL_DATABASE_URL": DSN}
        arguments: Final = [sys.executable, "-m", "cube_control", "manage-job", "--actor", "cli-fixture"]
        first: Final = subprocess.run(
            arguments, input=command.model_dump_json(), text=True, capture_output=True, env=environment, check=True
        )
        second: Final = subprocess.run(
            arguments, input=command.model_dump_json(), text=True, capture_output=True, env=environment, check=True
        )
        self.assertEqual(json.loads(first.stdout)["status"], "applied")
        self.assertEqual(json.loads(second.stdout)["status"], "duplicate")
        history: Final = subprocess.run(
            [sys.executable, "-m", "cube_control", "job-history", self.account.id, self.job.job_id, "--limit", "1"],
            text=True,
            capture_output=True,
            env=environment,
            check=True,
        )
        self.assertEqual(json.loads(history.stdout)["entries"][0]["actor"], "cli-fixture")
        invalid: Final = subprocess.run(
            arguments, input='{"credential_values":"private-canary"}', text=True, capture_output=True, env=environment
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertNotIn("private-canary", invalid.stdout + invalid.stderr)


if __name__ == "__main__":
    unittest.main()
