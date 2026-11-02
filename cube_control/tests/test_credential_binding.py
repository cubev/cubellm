import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Final
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr, TypeAdapter
from test_credential_inventory import (
    OLD_KEY,
    OPENAI_KEY,
    ROOT,
    SCANNED,
    SECRET_PATH,
    VOICE_KEY,
    parsed,
    sources,
    standard,
)

from cube_control.api import create_app
from cube_control.browser_session import private_authority
from cube_control.credential_binding import CredentialBindingCommand, CredentialBindingEntry
from cube_control.credential_inventory import public_reference
from cube_control.inventory import AccountInventoryEntry, AccountInventoryReport, account_inventory
from cube_control.models import Account, ImportBatch, Provider
from cube_control.store import ManualResult, PostgresRepository, StoredState

DSN: Final = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")
NOW: Final = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ORIGIN: Final = "http://127.0.0.1:4001"
TOKEN: Final = "synthetic-binding-token-" + "x" * 16
SESSION: Final = TypeAdapter(dict[str, str])


def headers(csrf: str = "", origin: str = ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "Content-Type": "application/json", **({"X-CSRF-Token": csrf} if csrf else {})}


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class CredentialBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = PostgresRepository(DSN)
        with self.repository.connect() as connection:
            database: Final = connection.execute("SELECT current_database()").fetchone()
        if database is None or database[0] != "cubecontrol_test":
            raise RuntimeError("Integration tests require the dedicated cubecontrol_test database")
        self.repository.initialize()
        marker: Final = uuid4().hex
        self.sources = (f"binding-a-{marker}", f"binding-b-{marker}")
        self.accounts = (
            Account(id=f"binding-first-{marker}", provider=Provider.OPENAI, name="First", kind="api"),
            Account(id=f"binding-second-{marker}", provider=Provider.OPENAI, name="Second", kind="api"),
            Account(id=f"binding-custom-{marker}", provider="custom-voice-api", name="Custom", kind="api"),
        )
        self.first = public_reference(self.sources[0], OPENAI_KEY)
        self.second = public_reference(self.sources[1], OPENAI_KEY)

        def forget() -> None:
            with self.repository.connect() as connection:
                for reference in (
                    self.first,
                    self.second,
                    *(public_reference(source, VOICE_KEY) for source in self.sources),
                ):
                    connection.execute(
                        "DELETE FROM cube_control.credential_binding_commands WHERE reference=%s", (reference,)
                    )
                for source in self.sources:
                    connection.execute("DELETE FROM cube_control.credential_inventories WHERE source_id=%s", (source,))
                for account in self.accounts:
                    connection.execute("DELETE FROM cube_control.manual_commands WHERE account_id=%s", (account.id,))
                    connection.execute("DELETE FROM cube_control.accounts WHERE id=%s", (account.id,))

        self.addCleanup(forget)
        assert self.repository.import_batch(ImportBatch(accounts=self.accounts)).ok
        for source in self.sources:
            assert self.repository.record_credential_import(source, NOW, parsed(standard(), source)) == "imported"

    def bind(
        self, reference: str, version: int, account: str | None, command_id: str = "", actor: str = "operator"
    ) -> ManualResult:
        return self.repository.bind_credential(
            CredentialBindingCommand(
                command_id=command_id or f"cmd-{uuid4().hex}",
                reference=reference,
                expected_version=version,
                action="bind" if account else "unbind",
                account_id=account,
            ),
            actor,
            NOW,
        )

    def report(self) -> AccountInventoryReport:
        return account_inventory(self.repository.state(), NOW)

    def account(self, report: AccountInventoryReport, index: int) -> AccountInventoryEntry:
        return next(item for item in report.accounts if item.account_id == self.accounts[index].id)

    def test_same_provider_accounts_only_show_their_own_confirmed_findings(self) -> None:
        results: Final = [self.bind(self.first, 0, self.accounts[0].id), self.bind(self.second, 0, self.accounts[1].id)]
        report: Final = self.report()
        self.assertEqual([(item.status, item.version) for item in results], [("applied", 1), ("applied", 1)])
        self.assertEqual(
            [
                (
                    [view.reference for view in self.account(report, index).confirmed_credentials],
                    "no_confirmed_credential_binding" in self.account(report, index).gaps,
                )
                for index in range(3)
            ],
            [([self.first], False), ([self.second], False), ([], True)],
        )
        candidate: Final = next(item for item in report.credential_candidates if item.reference == self.first)
        self.assertEqual(
            (candidate.binding, candidate.bound_account_id, candidate.binding_revision, candidate.validity),
            ("operator_confirmed", self.accounts[0].id, 1, "not_checked"),
        )
        self.assertNotIn(self.first, [item.reference for item in report.credential_findings])

    def test_rebinding_and_unbinding_keep_an_audited_history(self) -> None:
        steps: Final = [
            self.bind(self.first, 0, self.accounts[0].id, actor="first-operator"),
            self.bind(self.first, 1, self.accounts[2].id, actor="second-operator"),
            self.bind(self.first, 2, None, actor="first-operator"),
        ]
        view: Final = next(item for item in self.report().credential_bindings if item.reference == self.first)
        self.assertEqual([item.status for item in steps], ["applied"] * 3)
        self.assertEqual((view.account_id, view.revision, view.candidate), (None, 3, "current"))
        self.assertEqual(
            [(item.revision, item.action, item.account_id, item.actor) for item in view.history],
            [
                (1, "bind", self.accounts[0].id, "first-operator"),
                (2, "bind", self.accounts[2].id, "second-operator"),
                (3, "unbind", None, "first-operator"),
            ],
        )

    def test_retries_conflicts_and_identity_reuse_follow_the_versioned_contract(self) -> None:
        first: Final = self.bind(self.first, 0, self.accounts[0].id, command_id="fixed-command")
        retried: Final = self.bind(self.first, 0, self.accounts[0].id, command_id="fixed-command")
        other_actor: Final = self.bind(self.first, 0, self.accounts[0].id, command_id="fixed-command", actor="someone")
        other_payload: Final = self.bind(self.first, 0, self.accounts[1].id, command_id="fixed-command")
        stale: Final = self.bind(self.first, 0, self.accounts[1].id)
        self.assertEqual(
            [(item.status, item.version) for item in (first, retried, other_actor, other_payload, stale)],
            [("applied", 1), ("duplicate", 1), ("rejected", 0), ("rejected", 0), ("conflict", 1)],
        )

    def test_concurrent_first_bindings_produce_one_winner_and_one_conflict(self) -> None:
        start: Final = Barrier(2)

        def race(account: str) -> str:
            start.wait()
            return self.bind(self.first, 0, account).status

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes: Final = sorted(pool.map(race, (self.accounts[0].id, self.accounts[1].id)))
        self.assertEqual(outcomes, ["applied", "conflict"])

    def test_invalid_bindings_are_rejected_without_a_revision(self) -> None:
        unknown_reference: Final = public_reference(f"missing-{uuid4().hex}", OPENAI_KEY)
        results: Final = [
            self.bind(self.first, 0, f"unregistered-{uuid4().hex}"),
            self.bind(unknown_reference, 0, self.accounts[0].id),
            self.bind(self.first, 0, None),
        ]
        bound: Final = self.bind(self.first, 0, self.accounts[0].id)
        repeated: Final = self.bind(self.first, 1, self.accounts[0].id)
        self.assertEqual(
            [item.status for item in (*results, bound, repeated)], ["rejected"] * 3 + ["applied", "rejected"]
        )

    def test_a_binding_keeps_its_history_when_the_finding_disappears(self) -> None:
        self.bind(self.first, 0, self.accounts[0].id)
        self.assertEqual(
            self.repository.record_credential_import(
                self.sources[0], NOW + timedelta(minutes=1), parsed(sources([], [], scanned=NOW), self.sources[0])
            ),
            "imported",
        )
        report: Final = self.report()
        view: Final = next(item for item in report.credential_bindings if item.reference == self.first)
        self.assertEqual((view.account_id, view.candidate, len(view.history)), (self.accounts[0].id, "missing", 1))
        assert view.origin is not None
        self.assertEqual(
            (view.origin.source_id, view.origin.service, view.origin.state, view.origin.last_observed_at),
            (self.sources[0], "OpenAI", "current", SCANNED),
        )
        self.assertEqual(view.history[0].origin, view.origin)
        self.assertIn("confirmed_credential_not_in_latest_scan", self.account(report, 0).gaps)
        self.assertIn(
            ("binding_candidate_missing", self.first, self.sources[0], "OpenAI"),
            [(item.kind, item.reference, item.source_id, item.service) for item in report.credential_findings],
        )
        independent: Final = next(item for item in report.credential_candidates if item.reference == self.second)
        self.assertEqual((independent.presence, independent.binding), ("latest_scan", "unbound"))
        exposed: Final = report.model_dump_json()
        self.assertFalse(any(value in exposed for value in (OPENAI_KEY, VOICE_KEY, SECRET_PATH, ROOT)))

    def test_a_vanished_finding_stays_manageable_with_its_stored_origin(self) -> None:
        self.assertEqual(self.bind(self.first, 0, self.accounts[0].id).status, "applied")
        self.repository.record_credential_import(
            self.sources[0], NOW + timedelta(minutes=1), parsed(sources([], [], scanned=NOW), self.sources[0])
        )
        steps: Final = [
            self.bind(self.first, 1, self.accounts[2].id),
            self.bind(self.first, 2, None),
            self.bind(self.first, 3, self.accounts[1].id),
        ]
        self.assertEqual(
            [(item.status, item.version) for item in steps], [("applied", 2), ("applied", 3), ("applied", 4)]
        )
        view: Final = next(item for item in self.report().credential_bindings if item.reference == self.first)
        self.assertEqual((view.account_id, view.candidate), (self.accounts[1].id, "missing"))
        self.assertEqual(
            {(entry.origin.source_id, entry.origin.service) if entry.origin else None for entry in view.history},
            {(self.sources[0], "OpenAI")},
        )

    def test_binding_an_archived_finding_keeps_its_archive_evidence(self) -> None:
        archived: Final = public_reference(self.sources[0], OLD_KEY)
        self.addCleanup(self.forget_reference, archived)
        self.assertEqual(self.bind(archived, 0, self.accounts[2].id).status, "applied")
        kinds: Final = [item.kind for item in self.report().credential_findings if item.reference == archived]
        self.assertEqual(kinds, ["archived_candidate"])

    def forget_reference(self, reference: str) -> None:
        with self.repository.connect() as connection:
            connection.execute("DELETE FROM cube_control.credential_binding_commands WHERE reference=%s", (reference,))

    def test_the_cli_refuses_unreadable_and_oversized_commands_without_echo(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            oversized: Final = Path(root) / "oversized.json"
            oversized.write_text('{"echo-me": "' + "x" * 70000 + '"}')

            def run(path: Path) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, "-m", "cube_control", "bind-credential", str(path)],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=60,
                    env={**os.environ, "CUBE_CONTROL_DATABASE_URL": DSN},
                )

            missing: Final = run(Path(root) / "missing.json")
            large: Final = run(oversized)
        self.assertEqual(
            [(item.returncode, item.stdout, item.stderr) for item in (missing, large)],
            [(2, "", "Binding command is not readable\n"), (2, "", "Binding command exceeds size limit\n")],
        )

    def test_the_session_api_binds_with_guards_and_retries_after_signing_in_again(self) -> None:
        authority: Final = private_authority(SecretStr(TOKEN), ORIGIN, "browser-operator")
        assert authority is not None
        path: Final = f"/api/v1/credential-bindings/{self.first}/commands"
        body: Final = {
            "command_id": f"browser-{uuid4().hex}",
            "reference": self.first,
            "expected_version": 0,
            "action": "bind",
            "account_id": self.accounts[0].id,
        }

        def login(client: TestClient) -> str:
            response: Final = client.post("/api/v1/session", content=json.dumps({"token": TOKEN}), headers=headers())
            return SESSION.validate_json(response.content)["csrf_token"]

        with TestClient(create_app(self.repository, authority=authority), base_url=ORIGIN) as client:
            anonymous: Final = client.post(path, json=body, headers=headers("x"))
            csrf: Final = login(client)
            guarded: Final = [
                client.post(path, json=body, headers=headers()).status_code,
                client.post(path, json=body, headers=headers(csrf, origin="http://evil.example")).status_code,
                client.post(
                    f"/api/v1/credential-bindings/{self.second}/commands", json=body, headers=headers(csrf)
                ).status_code,
            ]
            applied: Final = client.post(path, json=body, headers=headers(csrf))
            client.request("DELETE", "/api/v1/session", headers=headers(csrf))
            again: Final = client.post(path, json=body, headers=headers(login(client)))
        self.assertEqual((anonymous.status_code, guarded), (401, [403, 403, 422]))
        self.assertEqual(
            [(item.status_code, json.loads(item.text)["status"]) for item in (applied, again)],
            [(200, "applied"), (200, "duplicate")],
        )
        entries: Final = [
            item for item in self.repository.state().credential_bindings if item.command.reference == self.first
        ]
        self.assertEqual([(item.revision, item.actor) for item in entries], [(1, "browser-operator")])

    def test_the_cli_applies_and_retries_a_saved_binding_command(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            command: Final = Path(root) / "binding.json"
            command.write_text(
                json.dumps(
                    {
                        "command_id": f"cli-{uuid4().hex}",
                        "reference": self.second,
                        "expected_version": 0,
                        "action": "bind",
                        "account_id": self.accounts[1].id,
                    }
                )
            )

            def run() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, "-m", "cube_control", "bind-credential", str(command), "--actor", "cli-operator"],
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=60,
                    env={**os.environ, "CUBE_CONTROL_DATABASE_URL": DSN},
                )

            outcomes: Final = [run(), run()]
        self.assertEqual(
            [
                (item.returncode, json.loads(item.stdout)["status"], json.loads(item.stdout)["version"])
                for item in outcomes
            ],
            [(0, "applied", 1), (0, "duplicate", 1)],
            outcomes[0].stderr,
        )


class BindingProjectionTests(unittest.TestCase):
    def test_binding_targets_stay_visible_even_without_a_catalogue_entry(self) -> None:
        reference: Final = public_reference("scan-a", OPENAI_KEY)

        def entry(revision: int, account: str) -> CredentialBindingEntry:
            return CredentialBindingEntry(
                command=CredentialBindingCommand(
                    command_id=f"cmd-{revision}",
                    reference=reference,
                    expected_version=revision - 1,
                    action="bind",
                    account_id=account,
                ),
                revision=revision,
                actor="operator",
                received_at=NOW,
            )

        report: Final = account_inventory(
            StoredState((), (), (), (), credential_bindings=(entry(1, "account-a"), entry(2, "account-b"))), NOW
        )
        self.assertEqual(
            [
                (item.account_id, item.references, "not_in_catalogue" in item.gaps, item.provider, item.kind)
                for item in report.accounts
            ],
            [
                ("account-a", ("credential_binding",), True, None, None),
                ("account-b", ("credential_binding",), True, None, None),
            ],
        )
        self.assertEqual(
            [(item.account_id, item.candidate, item.revision) for item in report.credential_bindings],
            [("account-b", "missing", 2)],
        )
