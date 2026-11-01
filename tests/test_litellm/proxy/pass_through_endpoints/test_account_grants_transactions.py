import asyncio
import json
import os
import re
import tempfile
import unittest
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import JsonValue, SecretStr, TypeAdapter
from typing_extensions import LiteralString

from litellm.models.credentials import CredentialItem
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.route_checks import RouteChecks
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.management_endpoints.cube_control_endpoints import (
    account_config_invalidator,
    account_credential_lookup,
    account_grant_database,
    router,
)
from litellm.proxy.pass_through_endpoints.account_credentials import (
    AccountApplicationGrant,
    AccountHeaderCredential,
    account_application_allowed,
)
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
from litellm.proxy.pass_through_endpoints.account_routes import (
    APPEND_ROUTE_SQL,
    INSERT_SETTINGS_SQL,
    AccountRouteCreateCommand,
    AccountRouteDefinition,
    AccountRouteReceipt,
    create_account_route,
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
    if query not in {LOCK_SETTINGS_SQL, READ_SETTINGS_SQL, WRITE_SETTINGS_SQL, INSERT_SETTINGS_SQL, APPEND_ROUTE_SQL}:
        raise ValueError("Unexpected SQL outside the account configuration transaction contract")
    return sql.SQL(
        cast(
            LiteralString, re.sub(r"\$(\d+)", r"%(p\1)s", query)
        )  # cast-ok: allowlisted SQL; only parameter markers change
    )


def numbered(parameters: tuple[str, ...]) -> dict[str, str]:
    return {"p" + str(index): value for index, value in enumerate(parameters, start=1)}


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
        cursor: Final = await self.connection.execute(postgres_query(query), numbered(parameters))
        rows: Final = await cursor.fetchall()
        if query == LOCK_SETTINGS_SQL and self.entered is not None and self.release is not None:
            self.entered.set()
            await self.release.wait()
        return tuple({"param_value": row[0]} for row in rows)

    async def execute_raw(self, query: str, *parameters: str) -> int:
        cursor: Final = await self.connection.execute(postgres_query(query), numbered(parameters))
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


class ManagementContractTransport(httpx.BaseTransport):
    def __init__(self, client: httpx.AsyncClient, loop: asyncio.AbstractEventLoop, user_id: str, team_id: str) -> None:
        self.client: Final = client
        self.loop: Final = loop
        self.user_id: Final = user_id
        self.team_id: Final = team_id
        self.user: dict[str, JsonValue] | None = None
        self.key: dict[str, JsonValue] | None = None
        self.key_hash: str | None = None
        self.grant_writes = 0

    async def native_request(self, request: httpx.Request) -> httpx.Response:
        response: Final = await self.client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers={"Content-Type": request.headers.get("content-type", "application/json")},
        )
        assert response.status_code == 200, (request.url.path, response.status_code, response.text)
        return response

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path: Final = request.url.path
        if path.startswith("/cube_control/"):
            if request.method == "POST":
                self.grant_writes += 1
            response: Final = asyncio.run_coroutine_threadsafe(self.native_request(request), self.loop).result(
                timeout=10
            )
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)
        if request.method == "POST":
            body: Final = TypeAdapter(dict[str, JsonValue]).validate_json(request.content)
            if path == "/user/new":
                self.user = body
            elif path == "/key/generate":
                token: Final = body.get("key")
                if not isinstance(token, str):
                    return httpx.Response(422)
                self.key_hash = sha256(token.encode()).hexdigest()
                self.key = {name: value for name, value in body.items() if name != "key"} | {"status": "active"}
            else:
                return httpx.Response(404)
            return httpx.Response(200, json={})
        if path == "/team/info":
            return httpx.Response(
                200,
                json={
                    "team_info": {
                        "team_id": self.team_id,
                        "models": [],
                        "members_with_roles": [{"user_id": self.user_id, "role": "user"}] if self.user else [],
                    }
                },
            )
        if path == "/user/info":
            return httpx.Response(200, json={"user_info": self.user}) if self.user else httpx.Response(404)
        if path == "/key/info":
            return httpx.Response(200, json={"info": self.key}) if self.key else httpx.Response(404)
        return httpx.Response(404)


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

    def route_command(
        self, route_id: str = "managed-route", path: str = "/cube_api/fixture/quota"
    ) -> AccountRouteCreateCommand:
        return AccountRouteCreateCommand(
            route=AccountRouteDefinition.model_validate(
                {
                    "id": route_id,
                    "path": path,
                    "target": "https://vendor.example/v1/quota",
                    "methods": ["GET", "POST"],
                    "account_credential": {
                        "account_id": "fixture-account",
                        "credential_name": "fixture-credential",
                        "headers": [{"name": "X-Api-Key", "field": "api_key"}],
                    },
                }
            )
        )

    async def stored_routes(self) -> tuple[dict[str, JsonValue], ...]:
        row: Final = await (
            await self.connection.execute("SELECT param_value->'pass_through_endpoints' FROM \"LiteLLM_Config\"")
        ).fetchone()
        assert row is not None
        return TypeAdapter(tuple[dict[str, JsonValue], ...]).validate_python(row[0])

    async def concurrently(
        self, first_command: AccountRouteCreateCommand, second_command: AccountRouteCreateCommand
    ) -> tuple[AccountRouteReceipt | GrantFailure, AccountRouteReceipt | GrantFailure]:
        entered: Final = asyncio.Event()
        release: Final = asyncio.Event()
        first_database: Final = PostgresDatabase(self.schema, entered, release)
        second_database: Final = PostgresDatabase(self.schema)
        first: Final = asyncio.create_task(create_account_route(first_database, first_command))
        await asyncio.wait_for(entered.wait(), timeout=5)
        second: Final = asyncio.create_task(create_account_route(second_database, second_command))
        try:
            backend: Final = await asyncio.wait_for(second_database.connections.get(), timeout=5)
            for _ in range(100):
                self.assertFalse(second.done(), "Second route creation passed a row still held by the first one")
                if await self.backend_wait_event(backend) == ("Lock",):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Second route creation did not enter a PostgreSQL lock wait")
        finally:
            release.set()
            outcomes: Final = await asyncio.gather(first, second)
        return outcomes[0], outcomes[1]

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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.application()), base_url="http://test"
        ) as client:
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

    async def test_controller_stores_native_grant_and_native_gates_enforce_its_scope(self) -> None:
        from cube_control.application_access import (
            ApplicationAccessSpec,
            deliver_application_access,
            prepare_application_access,
            provision_application_access,
        )
        from cube_control.application_accounts import AccountRouteScope

        await self.connection.execute(
            'UPDATE "LiteLLM_Config" SET param_value=jsonb_set(param_value, %s, %s::jsonb)',
            (["pass_through_endpoints", "0", "path"], json.dumps("/cube_api/fixture/quota")),
        )
        route: Final = await read_account_route(self.database, "fixture-route")
        assert isinstance(route, AccountRouteDescriptor)
        scope: Final = AccountRouteScope(
            route_id=route.route_id,
            account_id=route.account_id,
            path=route.path,
            expected_fingerprint=route.fingerprint,
            methods=("GET",),
        )
        spec: Final = ApplicationAccessSpec(
            application_id="fixture-controller-app",
            team_id="fixture-controller-team",
            gateway_origin="http://127.0.0.1:4000",
            account_routes=(scope,),
        )
        with tempfile.TemporaryDirectory() as root:
            directory: Final = Path(root) / "state"
            destination: Final = Path(root) / "consumer"
            self.assertEqual(prepare_application_access(spec, directory).status, "prepared")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.application()), base_url="http://test"
            ) as client:
                bridge: Final = ManagementContractTransport(
                    client, asyncio.get_running_loop(), spec.user_id, spec.team_id
                )
                provisioned: Final = await asyncio.to_thread(
                    provision_application_access,
                    directory,
                    SecretStr("inert-controller-master"),
                    bridge,
                )
                self.assertEqual(provisioned.status, "provisioned", provisioned.message)
                self.assertEqual(bridge.grant_writes, 1)
                delivered: Final = await asyncio.to_thread(
                    deliver_application_access,
                    directory,
                    destination,
                    SecretStr("inert-controller-master"),
                    bridge,
                )
                self.assertEqual(delivered.status, "delivered")
                self.assertEqual(bridge.grant_writes, 1)
            row: Final = await (await self.connection.execute('SELECT param_value FROM "LiteLLM_Config"')).fetchone()
            assert row is not None and bridge.key is not None and bridge.key_hash is not None
            stored: Final = TypeAdapter(dict[str, JsonValue]).validate_python(row[0])
            routes: Final = TypeAdapter(tuple[dict[str, JsonValue], ...]).validate_python(
                stored["pass_through_endpoints"]
            )
            policy: Final = AccountHeaderCredential.model_validate(routes[0]["account_credential"])
            token: Final = UserAPIKeyAuth.model_validate(bridge.key | {"token": bridge.key_hash})
            self.assertTrue(RouteChecks.is_virtual_key_allowed_to_call_route(scope.path, token))
            self.assertTrue(RouteChecks.check_passthrough_route_access(scope.path, token))
            self.assertTrue(
                account_application_allowed(policy, spec.application_id, bridge.key_hash, scope.path, "GET")
            )
            self.assertFalse(
                account_application_allowed(policy, spec.application_id, bridge.key_hash, scope.path, "POST")
            )
            self.assertFalse(account_application_allowed(policy, spec.application_id, "b" * 64, scope.path, "GET"))
            with self.assertRaises(HTTPException) as denied:
                RouteChecks.is_virtual_key_allowed_to_call_route(scope.path + "/child", token)
            self.assertEqual(denied.exception.status_code, 403)
            self.assertNotIn((destination / "api-key").read_text().strip(), policy.model_dump_json())

    async def test_route_creation_appends_once_preserves_unrelated_values_and_retries_without_writing(self) -> None:
        await self.connection.execute(
            'UPDATE "LiteLLM_Config" SET param_value = param_value || %s::jsonb',
            ('{"precise_unrelated_amount":1234567890.1234567890123456789}',),
        )
        before_routes: Final = await self.stored_routes()
        created: Final = await create_account_route(self.database, self.route_command())
        assert isinstance(created, AccountRouteReceipt)
        self.assertEqual((created.created, created.enforcement, created.route.grants), (True, "not_verified", ()))
        routes: Final = await self.stored_routes()
        self.assertEqual(routes[:-1], before_routes)
        self.assertEqual(routes[-1], self.route_command().route.stored())
        amount: Final = await (
            await self.connection.execute("SELECT param_value->>'precise_unrelated_amount' FROM \"LiteLLM_Config\"")
        ).fetchone()
        self.assertEqual(amount, ("1234567890.1234567890123456789",))
        before: Final = await (await self.connection.execute('SELECT xmin::text FROM "LiteLLM_Config"')).fetchone()
        repeated: Final = await create_account_route(self.database, self.route_command())
        after: Final = await (await self.connection.execute('SELECT xmin::text FROM "LiteLLM_Config"')).fetchone()
        self.assertEqual(repeated, created.model_copy(update={"created": False}))
        self.assertEqual(after, before)

    async def test_created_route_accepts_grants_and_a_retry_still_recognises_it(self) -> None:
        created: Final = await create_account_route(self.database, self.route_command())
        assert isinstance(created, AccountRouteReceipt)
        grant: Final = AccountGrantCommand(
            expected_fingerprint=created.route.fingerprint,
            grant=AccountApplicationGrant(
                application_id="fixture-app",
                key_hash="a" * 64,
                account_id="fixture-account",
                path="/cube_api/fixture/quota",
                methods=("GET",),
            ),
        )
        self.assertIsInstance(await append_account_grant(self.database, "managed-route", grant), AccountGrantReceipt)
        repeated: Final = await create_account_route(self.database, self.route_command())
        assert isinstance(repeated, AccountRouteReceipt)
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.route.fingerprint, created.route.fingerprint)
        self.assertEqual(repeated.route.grants, (grant.grant,))

    async def test_conflicting_id_or_shadowing_path_writes_nothing(self) -> None:
        self.assertIsInstance(await create_account_route(self.database, self.route_command()), AccountRouteReceipt)
        before: Final = await (await self.connection.execute('SELECT xmin::text FROM "LiteLLM_Config"')).fetchone()
        original: Final = self.route_command().route
        for definition in (
            original.model_copy(update={"methods": ("GET",)}),
            original.model_copy(update={"id": "same-path"}),
            original.model_copy(update={"id": "parent", "path": "/cube_api/fixture", "include_subpath": True}),
        ):
            with self.subTest(definition=definition.id):
                result = await create_account_route(self.database, AccountRouteCreateCommand(route=definition))
                self.assertEqual(result, GrantFailure("conflict"))
        after: Final = await (await self.connection.execute('SELECT xmin::text FROM "LiteLLM_Config"')).fetchone()
        self.assertEqual(after, before)

    async def test_stored_parameter_route_that_would_capture_the_path_blocks_creation(self) -> None:
        await self.connection.execute(
            'UPDATE "LiteLLM_Config" SET param_value = jsonb_set(param_value, %s, %s::jsonb)',
            (["pass_through_endpoints", "0", "path"], json.dumps("/cube_api/{rest:path}")),
        )
        result: Final = await create_account_route(self.database, self.route_command())
        self.assertEqual(result, GrantFailure("conflict"))
        self.assertEqual(tuple(route["id"] for route in await self.stored_routes()), ("fixture-route",))

    async def test_two_concurrent_distinct_routes_wait_on_the_row_lock_and_both_persist(self) -> None:
        first, second = await self.concurrently(
            self.route_command("first-route", "/cube_api/first"),
            self.route_command("second-route", "/cube_api/second"),
        )
        self.assertTrue(isinstance(first, AccountRouteReceipt) and first.created)
        self.assertTrue(isinstance(second, AccountRouteReceipt) and second.created)
        self.assertEqual(
            tuple(route["id"] for route in await self.stored_routes()),
            ("fixture-route", "first-route", "second-route"),
        )

    async def test_concurrent_identical_creations_store_one_route(self) -> None:
        first, second = await self.concurrently(self.route_command(), self.route_command())
        assert isinstance(first, AccountRouteReceipt) and isinstance(second, AccountRouteReceipt)
        self.assertEqual((first.created, second.created), (True, False))
        self.assertEqual(first.route, second.route)
        self.assertEqual(sum(route["id"] == "managed-route" for route in await self.stored_routes()), 1)

    async def test_concurrent_same_path_stores_only_the_first_route(self) -> None:
        first, second = await self.concurrently(self.route_command("first-route"), self.route_command("second-route"))
        self.assertIsInstance(first, AccountRouteReceipt)
        self.assertEqual(second, GrantFailure("conflict"))
        self.assertEqual(tuple(route["id"] for route in await self.stored_routes()), ("fixture-route", "first-route"))

    async def test_missing_settings_row_is_created_once_under_concurrency(self) -> None:
        await self.connection.execute('DELETE FROM "LiteLLM_Config"')
        first, second = await self.concurrently(
            self.route_command("first-route", "/cube_api/first"),
            self.route_command("second-route", "/cube_api/second"),
        )
        self.assertTrue(isinstance(first, AccountRouteReceipt) and isinstance(second, AccountRouteReceipt))
        count: Final = await (await self.connection.execute('SELECT count(*) FROM "LiteLLM_Config"')).fetchone()
        self.assertEqual(count, (1,))
        self.assertEqual(tuple(route["id"] for route in await self.stored_routes()), ("first-route", "second-route"))

    async def test_string_encoded_config_is_normalised_and_unrelated_values_survive(self) -> None:
        await self.connection.execute('UPDATE "LiteLLM_Config" SET param_value = %s', (Jsonb(json.dumps(CONFIG)),))
        self.assertIsInstance(await create_account_route(self.database, self.route_command()), AccountRouteReceipt)
        row: Final = await (
            await self.connection.execute("SELECT param_value->'unrelated' FROM \"LiteLLM_Config\"")
        ).fetchone()
        self.assertEqual(row, ({"keep": "inert-private-value"},))
        self.assertEqual(tuple(route["id"] for route in await self.stored_routes()), ("fixture-route", "managed-route"))

    async def test_failed_route_write_rolls_back_the_inserted_settings_row(self) -> None:
        await self.connection.execute('DELETE FROM "LiteLLM_Config"')
        await self.connection.execute(
            "ALTER TABLE \"LiteLLM_Config\" ADD CHECK (NOT param_value ? 'pass_through_endpoints')"
        )
        with self.assertRaises(psycopg.errors.CheckViolation):
            await create_account_route(self.database, self.route_command())
        count: Final = await (await self.connection.execute('SELECT count(*) FROM "LiteLLM_Config"')).fetchone()
        self.assertEqual(count, (0,))

    async def test_http_route_creation_uses_the_live_router_and_reports_storage_only(self) -> None:
        app: Final = self.application()
        credential: Final = CredentialItem(
            credential_name="fixture-credential",
            credential_values={"api_key": "inert-provider-value"},
            credential_info={"cube_account_id": "fixture-account", "cube_allowed_origins": ["https://vendor.example"]},
        )

        def lookup() -> Callable[[str], CredentialItem | None]:
            return lambda name: credential if name == credential.credential_name else None

        async def legacy() -> str:
            return "legacy"

        invalidated: Final[list[str]] = []

        def invalidator() -> Callable[[str], Awaitable[None]]:
            async def invalidate(name: str) -> None:
                invalidated.append(name)

            return invalidate

        app.dependency_overrides[account_credential_lookup] = lookup
        app.dependency_overrides[account_config_invalidator] = invalidator
        app.add_api_route("/cube_api/legacy/{rest:path}", legacy, methods=["GET"])
        shadowed: Final = self.route_command("shadowed-route", "/cube_api/legacy/quota")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            rejected: Final = await client.post("/cube_control/account_routes", json=shadowed.model_dump(mode="json"))
            created: Final = await client.post(
                "/cube_control/account_routes", json=self.route_command().model_dump(mode="json")
            )
            repeated: Final = await client.post(
                "/cube_control/account_routes", json=self.route_command().model_dump(mode="json")
            )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(created.status_code, 200, created.text)
        receipt: Final = AccountRouteReceipt.model_validate_json(created.content)
        self.assertEqual((receipt.created, receipt.enforcement), (True, "not_verified"))
        self.assertEqual(AccountRouteReceipt.model_validate_json(repeated.content).created, False)
        self.assertNotIn("inert-provider-value", created.text)
        self.assertEqual(invalidated, ["general_settings"])
        self.assertEqual(tuple(route["id"] for route in await self.stored_routes()), ("fixture-route", "managed-route"))

    async def test_controller_creates_the_route_and_then_grants_an_application_on_it(self) -> None:
        from cube_control.account_routes import AccountRouteHeader, AccountRouteSpec, ensure_account_route
        from cube_control.application_access import (
            ApplicationAccessSpec,
            prepare_application_access,
            provision_application_access,
        )
        from cube_control.application_accounts import AccountRouteScope

        app: Final = self.application()
        credential: Final = CredentialItem(
            credential_name="fixture-credential",
            credential_values={"api_key": "inert-provider-value"},
            credential_info={"cube_account_id": "fixture-account", "cube_allowed_origins": ["https://vendor.example"]},
        )

        def lookup() -> Callable[[str], CredentialItem | None]:
            return lambda name: credential if name == credential.credential_name else None

        app.dependency_overrides[account_credential_lookup] = lookup
        route_spec: Final = AccountRouteSpec(
            gateway_origin="http://127.0.0.1:4000",
            route_id="controller-route",
            account_id="fixture-account",
            credential_name="fixture-credential",
            headers=(AccountRouteHeader(name="X-Api-Key", field="api_key"),),
            target="https://vendor.example/v1/quota",
            path="/cube_api/controller/quota",
            methods=("GET",),
        )
        with tempfile.TemporaryDirectory() as root:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                route_bridge: Final = ManagementContractTransport(client, asyncio.get_running_loop(), "", "")
                created: Final = await asyncio.to_thread(
                    ensure_account_route, route_spec, SecretStr("inert-controller-master"), route_bridge
                )
                repeated: Final = await asyncio.to_thread(
                    ensure_account_route, route_spec, SecretStr("inert-controller-master"), route_bridge
                )
                self.assertEqual((created.status, repeated.status), ("created", "existing"), created.message)
                assert created.fingerprint is not None
                self.assertEqual(repeated.fingerprint, created.fingerprint)
                spec: Final = ApplicationAccessSpec(
                    application_id="fixture-route-app",
                    team_id="fixture-route-team",
                    gateway_origin="http://127.0.0.1:4000",
                    account_routes=(
                        AccountRouteScope(
                            route_id=route_spec.route_id,
                            account_id=route_spec.account_id,
                            expected_fingerprint=created.fingerprint,
                            path=route_spec.path,
                            methods=("GET",),
                        ),
                    ),
                )
                bridge: Final = ManagementContractTransport(
                    client, asyncio.get_running_loop(), spec.user_id, spec.team_id
                )
                directory: Final = Path(root) / "state"
                self.assertEqual(prepare_application_access(spec, directory).status, "prepared")
                provisioned: Final = await asyncio.to_thread(
                    provision_application_access, directory, SecretStr("inert-controller-master"), bridge
                )
                self.assertEqual(provisioned.status, "provisioned", provisioned.message)
        assert bridge.key_hash is not None
        descriptor: Final = await read_account_route(self.database, "controller-route")
        assert isinstance(descriptor, AccountRouteDescriptor)
        self.assertEqual(descriptor.fingerprint, created.fingerprint)
        self.assertEqual(
            tuple((grant.application_id, grant.key_hash, grant.methods) for grant in descriptor.grants),
            ((spec.application_id, bridge.key_hash, ("GET",)),),
        )
