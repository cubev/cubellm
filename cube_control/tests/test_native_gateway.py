import json
import os
import subprocess
import sys
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import Connection, connect, sql
from pydantic import BaseModel, JsonValue, RootModel

from cube_control.api import create_app
from cube_control.gateway import GatewayProjectionBatch
from cube_control.gateway_store import GatewayRepository
from cube_control.models import Account, GatewayBatch, GatewayScan, ImportBatch
from cube_control.native_gateway import PROJECTION_COLUMNS, prepare_native_gateway
from cube_control.store import PostgresRepository
from cube_control.usage import PostgresUsageReader, UsageHistory, UsagePage

DSN: Final = os.environ.get("CUBE_CONTROL_TEST_DATABASE_URL", "")
NOW: Final = datetime(2026, 1, 15, tzinfo=UTC)


class ProjectionContract(BaseModel):
    schema_version: Literal[1]
    columns: str


class JSONColumn(RootModel[JsonValue]):
    pass


@unittest.skipUnless(DSN, "A dedicated integration database is required")
class NativeGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection: Connection[tuple[str, ...]] = connect(DSN)
        self.addCleanup(self.connection.close)
        row: Final = self.connection.execute("SELECT current_database()").fetchone()
        if row is None or row[0] != "cubecontrol_test":
            raise RuntimeError("Dedicated test database required")
        self.connection.execute("""CREATE TEMP TABLE "LiteLLM_SpendLogs" (
            request_id text, custom_llm_provider text, api_base text, model text,
            "startTime" timestamp, "endTime" timestamp, updated_at timestamp, created_at timestamp,
            metadata jsonb, model_id text, call_type text, request_duration_ms integer,
            status text, spend numeric, prompt_tokens integer, completion_tokens integer, api_key text
        )""")

    def insert(self, metadata: Mapping[str, JsonValue]) -> None:
        self.connection.execute(
            """INSERT INTO "LiteLLM_SpendLogs" VALUES (
            'request',NULL,'https://api.github.com/rate_limit','unknown',
            '2026-01-15 12:00:00','2026-01-15 12:00:00.1',NULL,'2026-01-15 12:00:00.1',
            %s::jsonb,NULL,'pass_through_endpoint',100,'success',0,0,0,'inert-private-key-canary'
        )""",
            (json.dumps(metadata),),
        )

    def batch(self, columns: str = PROJECTION_COLUMNS) -> GatewayProjectionBatch:
        if columns != PROJECTION_COLUMNS:
            raise ValueError("Native projection contract does not match this version")
        query: Final = sql.SQL('SELECT row_to_json(x)::text FROM (SELECT {} FROM "LiteLLM_SpendLogs") x').format(
            sql.SQL(PROJECTION_COLUMNS)
        )
        rows: Final = tuple(JSONColumn.model_validate_json(row[0]).root for row in self.connection.execute(query))
        return GatewayProjectionBatch(
            scan=GatewayScan(
                scan_id="scan",
                source_id="source",
                expected_sequence=0,
                checked_at=NOW,
                state="ok",
                available_rows=len(rows),
                scanned_rows=len(rows),
            ),
            identity="native-instance",
            rows=rows,
        )

    def test_native_metadata_reaches_records_without_credentials_or_invented_usage(self) -> None:
        self.insert(
            {
                "provider_account_id": "github-account",
                "provider_account_source": "configured_credential",
                "provider_origin": "https://api.github.com",
                "provider_cost_status": "unverified",
                "user_api_key_application_id": "request-app",
                "api_key": "inert-metadata-canary",
                "messages": ["inert-prompt-canary"],
            }
        )
        batch: Final = prepare_native_gateway(self.batch())
        self.assertEqual((batch.scan.state, len(batch.records)), ("ok", 1))
        record: Final = batch.records[0]
        self.assertEqual(
            (record.account_id, record.application_id, record.provider), ("github-account", "request-app", "github")
        )
        self.assertIsNotNone(record.account_evidence)
        if record.account_evidence is None:
            self.fail("Native account evidence was discarded")
        self.assertEqual(record.account_evidence.origin, "https://api.github.com")
        self.assertEqual(record.account_evidence.cost_status, "unverified")
        self.assertEqual((record.input_tokens, record.output_tokens, record.cache_read_tokens), (None, None, None))
        for canary in ("inert-private-key-canary", "inert-metadata-canary", "inert-prompt-canary"):
            self.assertNotIn(canary, batch.model_dump_json())
            self.assertNotIn(canary, self.batch().model_dump_json())

    def test_sql_projection_import_and_usage_history_keep_the_request_account(self) -> None:
        account_id: Final = "native-" + uuid4().hex
        source: Final = "native-" + uuid4().hex
        self.insert(
            {
                "provider_account_id": account_id,
                "provider_account_source": "configured_credential",
                "provider_origin": "https://api.github.com",
                "provider_cost_status": "unverified",
                "user_api_key_application_id": "request-app",
            }
        )
        repository: Final = PostgresRepository(DSN)
        repository.initialize()
        self.assertTrue(
            repository.import_batch(
                ImportBatch(
                    accounts=(Account(id=account_id, provider="github", name="Native projection fixture", kind="api"),)
                )
            ).ok
        )
        projected: Final = prepare_native_gateway(self.batch())
        scan: Final = projected.scan.model_copy(update={"scan_id": uuid4().hex, "source_id": source})
        batch: Final = projected.model_copy(update={"scan": scan})
        gateway: Final = GatewayRepository(DSN)
        self.assertEqual(gateway.import_batch(batch).status, "applied")
        client: Final = TestClient(create_app(repository, usage_reader=PostgresUsageReader(DSN)))
        query: Final = {"period": "2026-01", "source_id": source, "account_id": account_id}
        page: Final = client.get("/api/v1/usage", params=query)
        self.assertEqual(page.status_code, 200)
        view: Final = UsagePage.model_validate_json(page.content)
        record: Final = view.rows[0].record
        self.assertEqual((record.account_id, record.application_id), (account_id, "request-app"))
        self.assertIsNotNone(record.account_evidence)
        if record.account_evidence is None:
            self.fail("Account evidence missing from usage API")
        self.assertEqual(record.account_evidence.origin, "https://api.github.com")
        self.assertEqual((view.provider_calls, view.metrics.unknown_input_calls), (1, 1))
        later_record: Final = batch.records[0].model_copy(update={"application_id": "later-app"})
        later_scan: Final = scan.model_copy(update={"scan_id": uuid4().hex, "expected_sequence": 1})
        later: Final = batch.model_copy(update={"scan": later_scan, "records": (later_record,)})
        self.assertEqual(gateway.import_batch(later).status, "applied")
        history: Final = client.get(f"/api/v1/usage/{source}/{batch.records[0].source_key}/history")
        self.assertEqual(history.status_code, 200)
        changes: Final = UsageHistory.model_validate_json(history.content).changes
        self.assertEqual([change.record.application_id for change in changes], ["request-app", "later-app"])
        self.assertEqual([change.record.account_evidence for change in changes], [record.account_evidence] * 2)

    def test_cli_exports_and_projects_native_rows_without_database_configuration(self) -> None:
        self.insert(
            {
                "provider_account_id": "account",
                "provider_account_source": "configured_credential",
                "provider_origin": "https://api.github.com",
                "provider_cost_status": "unverified",
                "user_api_key_application_id": "request-app",
            }
        )
        environment: Final = dict(os.environ, CUBE_CONTROL_DATABASE_URL="")
        contract_process: Final = subprocess.run(
            [sys.executable, "-m", "cube_control", "native-gateway-projection"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(contract_process.returncode, 0, contract_process.stderr)
        contract: Final = ProjectionContract.model_validate_json(contract_process.stdout)
        batch: Final = self.batch(contract.columns)
        projected_process: Final = subprocess.run(
            [sys.executable, "-m", "cube_control", "project-native-gateway"],
            env=environment,
            input=batch.model_dump_json(),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(projected_process.returncode, 0, projected_process.stderr)
        projected: Final = GatewayBatch.model_validate_json(projected_process.stdout)
        self.assertEqual(len(projected.records), 1)
        self.assertEqual(projected.records[0].account_id, "account")
        self.assertEqual(projected.records[0].application_id, "request-app")
        self.assertEqual(projected.records[0].provider, "github")
        self.assertIsNotNone(projected.records[0].account_evidence)
        self.assertNotIn("inert-private-key-canary", projected_process.stdout)

    def test_custom_api_provider_comes_only_from_the_bound_credential_metadata(self) -> None:
        self.insert(
            {
                "provider_account_id": "custom-account",
                "provider_account_source": "configured_credential",
                "provider_origin": "https://custom-api.example",
                "provider_cost_status": "unverified",
                "provider_account_provider": "custom-provider",
            }
        )
        self.connection.execute(
            'UPDATE "LiteLLM_SpendLogs" SET api_base=%s,custom_llm_provider=%s,model=%s',
            ("https://custom-api.example/quota", "client-provider", "unrelated-model"),
        )
        record: Final = prepare_native_gateway(self.batch()).records[0]
        self.assertEqual((record.provider, record.account_id), ("custom-provider", "custom-account"))
        self.assertIsNotNone(record.account_evidence)
        if record.account_evidence is None:
            self.fail("Custom provider evidence missing")
        self.assertEqual(record.account_evidence.provider, "custom-provider")
        self.connection.execute('UPDATE "LiteLLM_SpendLogs" SET metadata=metadata-%s', ("provider_account_provider",))
        self.assert_rejected_batch()

    def test_provider_metadata_cannot_relabel_a_known_vendor_origin(self) -> None:
        self.insert(
            {
                "provider_account_id": "account",
                "provider_account_source": "configured_credential",
                "provider_origin": "https://api.github.com",
                "provider_cost_status": "unverified",
                "provider_account_provider": "another-provider",
            }
        )
        self.assert_rejected_batch()

    def test_recorded_target_cannot_conflict_with_the_account_origin(self) -> None:
        self.insert(
            {
                "provider_account_id": "account",
                "provider_account_source": "configured_credential",
                "provider_origin": "https://api.github.com",
                "provider_cost_status": "unverified",
            }
        )
        for target in (
            "https://other.example/quota",
            "https://user:inert-canary@api.github.com/quota",
            "https://@api.github.com/quota",
            "https://api.github.com:444/quota",
            "",
        ):
            with self.subTest(target=target):
                self.connection.execute('UPDATE "LiteLLM_SpendLogs" SET api_base=%s', (target,))
                self.assert_rejected_batch()

    def test_unstamped_metadata_never_claims_an_account_or_evidence(self) -> None:
        self.insert(
            {
                "provider_account_id": "forged-account",
                "provider_origin": "https://api.github.com",
                "account_id": "another-account",
                "cube_account_id": "another-account",
            }
        )
        record: Final = prepare_native_gateway(self.batch()).records[0]
        self.assertIsNone(record.account_id)
        self.assertIsNone(record.account_evidence)

    def test_incomplete_conflicting_or_secret_bearing_evidence_rejects_the_row(self) -> None:
        for metadata in (
            {"provider_origin": "https://wrong.example"},
            {"provider_origin": "https://api.github.com?api_key=inert-canary"},
            {"provider_origin": "https://user:inert-canary@api.github.com"},
            {"provider_origin": None},
            {"provider_cost_status": "verified"},
            {"provider_account_id": None},
        ):
            with self.subTest(metadata=metadata):
                self.connection.execute('DELETE FROM "LiteLLM_SpendLogs"')
                self.insert(
                    {
                        "provider_account_id": "account",
                        "provider_account_source": "configured_credential",
                        "provider_origin": "https://api.github.com",
                        "provider_cost_status": "unverified",
                    }
                    | metadata
                )
                self.assert_rejected_batch()

    def assert_rejected_batch(self) -> None:
        result: Final = prepare_native_gateway(self.batch())
        self.assertEqual((result.scan.state, result.scan.rejected_rows, result.records), ("partial", 1, ()))
        self.assertNotIn("inert-canary", result.model_dump_json())
