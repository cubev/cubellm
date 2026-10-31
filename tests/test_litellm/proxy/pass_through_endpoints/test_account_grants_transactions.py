import asyncio
import json
import os
import re
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Final, cast
from uuid import uuid4

import httpx
import psycopg
from fastapi import FastAPI
from psycopg import sql
from psycopg.types.json import Jsonb
from typing_extensions import LiteralString

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_endpoints.cube_control_endpoints import account_grant_database, router
from litellm.proxy.pass_through_endpoints.account_credentials import AccountApplicationGrant
from litellm.proxy.pass_through_endpoints.account_grants import (
    LOCK_SETTINGS_SQL,
    READ_SETTINGS_SQL,
    WRITE_SETTINGS_SQL,
    AccountGrantCommand,
    AccountGrantReceipt,
    AccountRouteDescriptor,
    GrantDatabase,
    GrantFailure,
    GrantTransaction,
    append_account_grant,
    read_account_route,
)

DSN: Final = os.environ.get("CUBELLM_ACCOUNT_GRANTS_TEST_DATABASE_URL", "")
CONFIG: Final = {
    "unrelated": {"keep": "inert-private-value"},
    "pass_through_endpoints": [
        {
            "id": "fixture-route",
            "path": "/fixture/quota",
            "target": "https://vendor.example/quota",
            "auth": True,
            "methods": ["GET"],
            "account_credential": {
                "account_id": "fixture-account",
                "credential_name": "fixture-credential",
                "headers": [{"name": "X-Api-Key", "field": "api_key"}],
            },
        }
    ],
}


def postgres_query(query: str) -> sql.SQL:
    if query not in {LOCK_SETTINGS_SQL, READ_SETTINGS_SQL, WRITE_SETTINGS_SQL}:
        raise ValueError("Unexpected SQL outside the grant transaction contract")
    return sql.SQL(
        cast(LiteralString, re.sub(r"\$\d+", "%s", query))  # cast-ok: allowlisted SQL; only parameter markers change
    )


class PostgresTransaction:
    def __init__(
        self,
        connection: psycopg.AsyncConnection[tuple[object, ...]],
        entered: asyncio.Event | None,
        release: asyncio.Event | None,
    ) -> None:
        self.connection: Final = connection
        self.entered: Final = entered
        self.release: Final = release

    async def query_raw(self, query: str, *parameters: str) -> object:
        cursor: Final = await self.connection.execute(postgres_query(query), parameters)
        rows: Final = await cursor.fetchall()
        if query == LOCK_SETTINGS_SQL and self.entered is not None and self.release is not None:
            self.entered.set()
            await self.release.wait()
        return tuple({"param_value": row[0]} for row in rows)

    async def execute_raw(self, query: str, *parameters: str) -> int:
        cursor: Final = await self.connection.execute(postgres_query(query), parameters)
        return cursor.rowcount


class PostgresDatabase:
    def __init__(self, schema: str, entered: asyncio.Event | None = None, release: asyncio.Event | None = None) -> None:
        self.schema: Final = schema
        self.entered: Final = entered
        self.release: Final = release
        self.connections: Final[asyncio.Queue[int]] = asyncio.Queue()

    @asynccontextmanager
    async def tx(self) -> AsyncGenerator[GrantTransaction]:
        connection: Final[psycopg.AsyncConnection[tuple[object, ...]]] = await psycopg.AsyncConnection.connect(DSN)
        async with connection:
            await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            self.connections.put_nowait(connection.info.backend_pid)
            yield PostgresTransaction(connection, self.entered, self.release)


@unittest.skipUnless(DSN, "A dedicated local PostgreSQL integration database is required")
class AccountGrantTransactions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.connection: psycopg.AsyncConnection[tuple[object, ...]] = await psycopg.AsyncConnection.connect(
            DSN, autocommit=True
        )
        current: Final = await (await self.connection.execute("SELECT current_database()")).fetchone()
        if current is None or current[0] != "cubecontrol_test":
            await self.connection.close()
            raise RuntimeError("Account grant integration tests require the dedicated cubecontrol_test database")
        self.schema = "cubellm_grants_" + uuid4().hex
        await self.connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        await self.connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        await self.connection.execute(
            'CREATE TABLE "LiteLLM_Config" (param_name text PRIMARY KEY, param_value jsonb NOT NULL)'
        )
        await self.connection.execute(
            'INSERT INTO "LiteLLM_Config" VALUES (%s, %s)', ("general_settings", Jsonb(CONFIG))
        )
        self.database = PostgresDatabase(self.schema)
        descriptor: Final = await read_account_route(self.database, "fixture-route")
        assert isinstance(descriptor, AccountRouteDescriptor)
        self.descriptor = descriptor

    async def asyncTearDown(self) -> None:
        try:
            await self.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        finally:
            await self.connection.close()

    def command(self, application: str = "fixture-app", key_hash: str = "a" * 64) -> AccountGrantCommand:
        return AccountGrantCommand(
            expected_fingerprint=self.descriptor.fingerprint,
            grant=AccountApplicationGrant(
                application_id=application,
                key_hash=key_hash,
                account_id="fixture-account",
                path="/fixture/quota",
                methods=("GET",),
            ),
        )

    async def backend_wait_event(self, backend: int) -> tuple[object, ...] | None:
        return await (
            await self.connection.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (backend,))
        ).fetchone()

    def application(self) -> FastAPI:
        app: Final = FastAPI()
        app.include_router(router)

        async def admin() -> UserAPIKeyAuth:
            return UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN)

        def database() -> GrantDatabase:
            return self.database

        app.dependency_overrides[user_api_key_auth] = admin
        app.dependency_overrides[account_grant_database] = database
        return app

    async def test_http_management_persists_one_grant_and_reports_storage_without_claiming_enforcement(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.application()), base_url="http://test"
        ) as client:
            before: Final = await client.get("/cube_control/account_routes/fixture-route")
            self.assertEqual(before.status_code, 200)
            before_route: Final = AccountRouteDescriptor.model_validate_json(before.content)
            self.assertEqual(before_route.fingerprint, self.descriptor.fingerprint)
            result: Final = await client.post(
                "/cube_control/account_routes/fixture-route/grants", json=self.command().model_dump(mode="json")
            )
            self.assertEqual(result.status_code, 200)
            receipt: Final = AccountGrantReceipt.model_validate_json(result.content)
            self.assertTrue({"stored", "enforcement", "route"}.issubset(receipt.model_fields_set))
            self.assertTrue(receipt.stored)
            self.assertEqual(receipt.enforcement, "not_verified")
            repeated: Final = await client.post(
                "/cube_control/account_routes/fixture-route/grants", json=self.command().model_dump(mode="json")
            )
            self.assertEqual(AccountGrantReceipt.model_validate_json(repeated.content), receipt)
            after: Final = await client.get("/cube_control/account_routes/fixture-route")
            after_route: Final = AccountRouteDescriptor.model_validate_json(after.content)
            self.assertEqual(len(after_route.grants), 1)
            self.assertEqual(after_route.fingerprint, self.descriptor.fingerprint)
            self.assertNotIn("inert-private-value", result.text + before.text + after.text)

    async def test_http_management_rejects_stale_or_out_of_scope_commands_without_writing(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.application()), base_url="http://test"
        ) as client:
            stale: Final = await client.post(
                "/cube_control/account_routes/fixture-route/grants",
                json=self.command().model_dump(mode="json") | {"expected_fingerprint": "0" * 64},
            )
            self.assertEqual(stale.status_code, 409)
            outside: Final = AccountGrantCommand(
                expected_fingerprint=self.descriptor.fingerprint,
                grant=self.command().grant.model_copy(update={"account_id": "other-account"}),
            )
            refused: Final = await client.post(
                "/cube_control/account_routes/fixture-route/grants", json=outside.model_dump(mode="json")
            )
            self.assertEqual(refused.status_code, 400)
            absent: Final = await client.get("/cube_control/account_routes/absent-route")
            self.assertEqual(absent.status_code, 404)
        current: Final = await read_account_route(self.database, "fixture-route")
        assert isinstance(current, AccountRouteDescriptor)
        self.assertEqual(current.grants, ())
        stored: Final = await append_account_grant(self.database, "fixture-route", self.command())
        self.assertIsInstance(stored, AccountGrantReceipt)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.application()), base_url="http://test") as client:
            existing: Final = await client.post(
                "/cube_control/account_routes/fixture-route/grants", json=outside.model_dump(mode="json")
            )
        self.assertEqual(existing.status_code, 400)

    async def test_two_concurrent_appends_wait_on_the_real_row_lock_and_preserve_both_grants(self) -> None:
        entered: Final = asyncio.Event()
        release: Final = asyncio.Event()
        first_database: Final = PostgresDatabase(self.schema, entered, release)
        second_database: Final = PostgresDatabase(self.schema)
        first: Final = asyncio.create_task(append_account_grant(first_database, "fixture-route", self.command()))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second: Final = asyncio.create_task(
            append_account_grant(second_database, "fixture-route", self.command("second-app", "b" * 64))
        )
        try:
            backend: Final = await asyncio.wait_for(second_database.connections.get(), timeout=5)
            for _ in range(100):
                self.assertFalse(second.done(), "Second mutation passed a row still held by the first transaction")
                if await self.backend_wait_event(backend) == ("Lock",):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Second grant mutation did not enter the PostgreSQL row-lock wait")
        finally:
            release.set()
            outcomes: Final = await asyncio.gather(first, second)
        self.assertTrue(all(isinstance(value, AccountGrantReceipt) for value in outcomes))
        current: Final = await read_account_route(self.database, "fixture-route")
        assert isinstance(current, AccountRouteDescriptor)
        self.assertEqual({value.application_id for value in current.grants}, {"fixture-app", "second-app"})
        self.assertEqual(current.fingerprint, self.descriptor.fingerprint)

    async def test_retry_does_not_rewrite_the_row_or_duplicate_a_grant(self) -> None:
        first: Final = await append_account_grant(self.database, "fixture-route", self.command())
        self.assertIsInstance(first, AccountGrantReceipt)
        before: Final = await (await self.connection.execute('SELECT xmin::text FROM "LiteLLM_Config"')).fetchone()
        repeated: Final = await append_account_grant(self.database, "fixture-route", self.command())
        after: Final = await (await self.connection.execute('SELECT xmin::text FROM "LiteLLM_Config"')).fetchone()
        self.assertEqual(repeated, first)
        self.assertEqual(after, before)

    async def test_unrelated_json_numbers_keep_their_exact_database_precision(self) -> None:
        await self.connection.execute(
            'UPDATE "LiteLLM_Config" SET param_value = param_value || %s::jsonb',
            ('{"precise_unrelated_amount":1234567890.1234567890123456789}',),
        )
        before: Final = await (
            await self.connection.execute("SELECT param_value->>'precise_unrelated_amount' FROM \"LiteLLM_Config\"")
        ).fetchone()
        result: Final = await append_account_grant(self.database, "fixture-route", self.command())
        self.assertIsInstance(result, AccountGrantReceipt)
        after: Final = await (
            await self.connection.execute("SELECT param_value->>'precise_unrelated_amount' FROM \"LiteLLM_Config\"")
        ).fetchone()
        self.assertEqual(after, before)

    async def test_string_encoded_native_config_and_unrelated_fields_survive_the_update(self) -> None:
        await self.connection.execute('UPDATE "LiteLLM_Config" SET param_value = %s', (Jsonb(json.dumps(CONFIG)),))
        result: Final = await append_account_grant(self.database, "fixture-route", self.command())
        assert isinstance(result, AccountGrantReceipt)
        self.assertEqual(result.enforcement, "not_verified")
        row: Final = await (
            await self.connection.execute("SELECT param_value->'unrelated' FROM \"LiteLLM_Config\"")
        ).fetchone()
        self.assertEqual(row, ({"keep": "inert-private-value"},))
        self.assertNotIn("inert-private-value", result.model_dump_json())

    async def test_write_failure_rolls_back_without_losing_the_existing_grant(self) -> None:
        first: Final = await append_account_grant(self.database, "fixture-route", self.command())
        self.assertIsInstance(first, AccountGrantReceipt)
        await self.connection.execute(
            'ALTER TABLE "LiteLLM_Config" ADD CHECK ('
            "jsonb_array_length(param_value->'pass_through_endpoints'->0->'account_credential'->'application_grants') <= 1)"
        )
        with self.assertRaises(psycopg.errors.CheckViolation):
            await append_account_grant(self.database, "fixture-route", self.command("second-app", "b" * 64))
        current: Final = await read_account_route(self.database, "fixture-route")
        assert isinstance(current, AccountRouteDescriptor)
        self.assertEqual(tuple(value.application_id for value in current.grants), ("fixture-app",))

    async def test_missing_config_row_returns_no_receipt_and_creates_nothing(self) -> None:
        await self.connection.execute('DELETE FROM "LiteLLM_Config"')
        result: Final = await append_account_grant(self.database, "fixture-route", self.command())
        self.assertEqual(result, GrantFailure("not_found"))
        count: Final = await (await self.connection.execute('SELECT count(*) FROM "LiteLLM_Config"')).fetchone()
        self.assertEqual(count, (0,))
