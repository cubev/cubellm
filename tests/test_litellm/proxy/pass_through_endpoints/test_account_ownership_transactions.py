import asyncio
import os
import re
import unittest
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import JsonValue
from typing_extensions import LiteralString

from litellm.proxy.pass_through_endpoints.account_grants import (
    LOCK_SETTINGS_SQL,
    AccountRouteDescriptor,
    GrantFailure,
    GrantTransaction,
    describe_account_route,
)
from litellm.proxy.pass_through_endpoints.account_ownership import (
    CLAIM_ADOPTED_SQL,
    CLAIM_CREATED_SQL,
    READ_OWNERSHIP_SQL,
    OwnershipConflict,
    OwnershipRecorded,
    ResourceAdoptionCommand,
    ResourceClaim,
    ResourceOwnership,
    adopt_resource,
    read_ownership,
    record_ownership,
)

DSN: Final = os.environ.get("CUBELLM_ACCOUNT_GRANTS_TEST_DATABASE_URL", "")
MIGRATION: Final = (
    Path(__file__).resolve().parents[4]
    / "litellm-proxy-extras/litellm_proxy_extras/migrations/20261101120000_add_account_resource_ownership/migration.sql"
)
CREATOR: Final = "a" * 64
ROUTE: Final[dict[str, JsonValue]] = {
    "id": "fixture-route",
    "path": "/cube_api/media",
    "target": "https://vendor.example/v1",
    "methods": ["GET", "POST"],
    "include_subpath": True,
    "auth": True,
    "forward_headers": False,
    "account_credential": {
        "account_id": "fixture-account",
        "credential_name": "fixture-credential",
        "headers": [{"name": "X-Api-Key", "field": "api_key", "prefix": ""}],
        "allowed_query_parameters": [],
        "resource_types": [{"name": "video-job", "item_paths": ["/cube_api/media/jobs/{resource_id}"]}],
    },
}
SETTINGS: Final[dict[str, JsonValue]] = {"pass_through_endpoints": [ROUTE]}
ROTATED: Final = "b" * 64


def claim(**changes: str | None) -> ResourceClaim:
    return ResourceClaim.model_validate(
        {
            "account_id": "fixture-account",
            "resource_type": "video-job",
            "resource_id": "vid_1",
            "route_id": "fixture-route",
            "owner_application_id": "fixture-app",
            "provenance": "created",
            "creator_key_hash": CREATOR,
        }
        | changes
    )


def postgres_query(query: str) -> sql.SQL:
    if query not in {CLAIM_CREATED_SQL, CLAIM_ADOPTED_SQL, READ_OWNERSHIP_SQL, LOCK_SETTINGS_SQL}:
        raise ValueError("Unexpected SQL outside the ownership transaction contract")
    return sql.SQL(
        cast(
            LiteralString, re.sub(r"\$(\d+)", r"%(p\1)s", query)
        )  # cast-ok: allowlisted SQL; only parameter markers change
    )


def numbered(parameters: tuple[str, ...]) -> dict[str, str]:
    return {"p" + str(index): value for index, value in enumerate(parameters, start=1)}


class PostgresTransaction:
    def __init__(self, database: "PostgresDatabase", connection: psycopg.AsyncConnection[tuple[object, ...]]) -> None:
        self.database: Final = database
        self.connection: Final = connection

    async def query_raw(self, query: str, *parameters: str) -> object:
        if query == READ_OWNERSHIP_SQL and self.database.hide_readback:
            return ()
        cursor: Final = await self.connection.execute(postgres_query(query), numbered(parameters))
        names: Final = tuple(column.name for column in cursor.description or ())
        return tuple(dict(zip(names, row)) for row in await cursor.fetchall())

    async def execute_raw(self, query: str, *parameters: str) -> int:
        cursor: Final = await self.connection.execute(postgres_query(query), numbered(parameters))
        if self.database.entered is not None and self.database.release is not None:
            self.database.entered.set()
            await self.database.release.wait()
        return cursor.rowcount


class PostgresDatabase:
    def __init__(
        self,
        schema: str,
        *,
        hide_readback: bool = False,
        entered: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.schema: Final = schema
        self.hide_readback: Final = hide_readback
        self.entered: Final = entered
        self.release: Final = release
        self.connections: Final[asyncio.Queue[int]] = asyncio.Queue()

    @asynccontextmanager
    async def tx(self) -> AsyncGenerator[GrantTransaction]:
        connection: Final[psycopg.AsyncConnection[tuple[object, ...]]] = await psycopg.AsyncConnection.connect(DSN)
        async with connection:
            await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            self.connections.put_nowait(connection.info.backend_pid)
            yield PostgresTransaction(self, connection)


@unittest.skipUnless(DSN, "A dedicated local PostgreSQL integration database is required")
class AccountOwnershipTransactions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.connection: psycopg.AsyncConnection[tuple[object, ...]] = await psycopg.AsyncConnection.connect(
            DSN, autocommit=True
        )
        current: Final = await (await self.connection.execute("SELECT current_database()")).fetchone()
        if current is None or current[0] != "cubecontrol_test":
            await self.connection.close()
            raise RuntimeError("Ownership integration tests require the dedicated cubecontrol_test database")
        self.schema = "cubellm_ownership_" + uuid4().hex
        await self.connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        await self.connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        await self.connection.execute(
            sql.SQL(cast(LiteralString, MIGRATION.read_text()))  # cast-ok: the repository's own migration file
        )
        await self.connection.execute(
            'CREATE TABLE "LiteLLM_Config" (param_name text PRIMARY KEY, param_value jsonb NOT NULL)'
        )
        await self.connection.execute(
            'INSERT INTO "LiteLLM_Config" VALUES (%s, %s)', ("general_settings", Jsonb(SETTINGS))
        )
        self.database = PostgresDatabase(self.schema)
        descriptor: Final = describe_account_route(SETTINGS, "fixture-route")
        assert isinstance(descriptor, AccountRouteDescriptor)
        self.fingerprint = descriptor.fingerprint

    async def asyncTearDown(self) -> None:
        try:
            await self.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        finally:
            await self.connection.close()

    async def rows(self) -> list[tuple[object, ...]]:
        cursor: Final = await self.connection.execute(
            'SELECT resource_id, owner_application_id, provenance, creator_key_hash FROM "LiteLLM_AccountResourceOwnership"'
            " ORDER BY resource_id"
        )
        return await cursor.fetchall()

    async def test_created_resource_is_owned_by_the_application_and_retries_are_idempotent(self) -> None:
        first: Final = await record_ownership(self.database, claim())
        repeated: Final = await record_ownership(self.database, claim())
        rotated: Final = await record_ownership(self.database, claim(creator_key_hash=ROTATED))
        expected: Final = ResourceOwnership(
            route_id="fixture-route", owner_application_id="fixture-app", provenance="created", creator_key_hash=CREATOR
        )
        self.assertEqual(first, OwnershipRecorded(expected, True))
        self.assertEqual(repeated, OwnershipRecorded(expected, False))
        self.assertEqual(rotated, OwnershipRecorded(expected, False))
        self.assertEqual(await self.rows(), [("vid_1", "fixture-app", "created", CREATOR)])
        self.assertEqual(await read_ownership(self.database, "fixture-account", "video-job", "vid_1"), expected)

    async def test_another_application_or_route_cannot_take_over_a_resource(self) -> None:
        await record_ownership(self.database, claim())
        for changes in (
            {"owner_application_id": "other-app"},
            {"owner_application_id": "other-app", "provenance": "adopted", "creator_key_hash": None},
            {"route_id": "other-route"},
        ):
            with self.subTest(changes=changes):
                result = await record_ownership(self.database, claim(**changes))
                self.assertIsInstance(result, OwnershipConflict)
                assert isinstance(result, OwnershipConflict)
                self.assertEqual(result.ownership.owner_application_id, "fixture-app")
        self.assertEqual(await self.rows(), [("vid_1", "fixture-app", "created", CREATOR)])

    async def test_administrators_adopt_existing_resources_without_creator_provenance(self) -> None:
        adopted: Final = await record_ownership(
            self.database, claim(resource_id="legacy_7", provenance="adopted", creator_key_hash=None)
        )
        assert isinstance(adopted, OwnershipRecorded)
        self.assertEqual(
            (adopted.inserted, adopted.ownership.provenance, adopted.ownership.creator_key_hash),
            (True, "adopted", None),
        )
        await record_ownership(self.database, claim())
        readopted: Final = await record_ownership(self.database, claim(provenance="adopted", creator_key_hash=None))
        self.assertEqual(
            readopted,
            OwnershipRecorded(
                ResourceOwnership(
                    route_id="fixture-route",
                    owner_application_id="fixture-app",
                    provenance="created",
                    creator_key_hash=CREATOR,
                ),
                False,
            ),
        )
        self.assertEqual(
            await self.rows(),
            [("legacy_7", "fixture-app", "adopted", None), ("vid_1", "fixture-app", "created", CREATOR)],
        )

    async def test_a_new_row_that_cannot_be_verified_is_rolled_back(self) -> None:
        with self.assertRaises(RuntimeError):
            await record_ownership(PostgresDatabase(self.schema, hide_readback=True), claim())
        self.assertEqual(await self.rows(), [])

    async def test_concurrent_claims_by_two_applications_leave_exactly_one_owner(self) -> None:
        entered: Final = asyncio.Event()
        release: Final = asyncio.Event()
        first: Final = asyncio.create_task(
            record_ownership(PostgresDatabase(self.schema, entered=entered, release=release), claim())
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        second_database: Final = PostgresDatabase(self.schema)
        second: Final = asyncio.create_task(
            record_ownership(second_database, claim(owner_application_id="other-app", creator_key_hash=ROTATED))
        )
        try:
            backend: Final = await asyncio.wait_for(second_database.connections.get(), timeout=5)
            for _ in range(100):
                self.assertFalse(second.done(), "Second claim passed an uncommitted competing insert")
                waiting = await (
                    await self.connection.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (backend,)
                    )
                ).fetchone()
                if waiting == ("Lock",):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Second claim did not wait on the first uncommitted insert")
        finally:
            release.set()
            outcomes: Final = await asyncio.gather(first, second)
        self.assertIsInstance(outcomes[0], OwnershipRecorded)
        self.assertIsInstance(outcomes[1], OwnershipConflict)
        self.assertEqual(await self.rows(), [("vid_1", "fixture-app", "created", CREATOR)])

    async def test_non_canonical_ids_are_never_looked_up(self) -> None:
        self.assertIsNone(
            await read_ownership(PostgresDatabase("missing-schema"), "fixture-account", "video-job", "a/b")
        )

    def adoption(self, **changes: str) -> ResourceAdoptionCommand:
        return ResourceAdoptionCommand.model_validate(
            {
                "expected_fingerprint": self.fingerprint,
                "resource_type": "video-job",
                "resource_id": "legacy_7",
                "application_id": "fixture-app",
            }
            | changes
        )

    async def test_adoption_binds_an_existing_resource_to_the_application_once(self) -> None:
        first: Final = await adopt_resource(self.database, "fixture-route", self.adoption())
        repeated: Final = await adopt_resource(self.database, "fixture-route", self.adoption())
        expected: Final = ResourceOwnership(
            route_id="fixture-route", owner_application_id="fixture-app", provenance="adopted", creator_key_hash=None
        )
        self.assertEqual((first, repeated), (OwnershipRecorded(expected, True), OwnershipRecorded(expected, False)))
        self.assertEqual(await self.rows(), [("legacy_7", "fixture-app", "adopted", None)])

    async def test_adoption_against_a_stale_or_mismatched_route_writes_nothing(self) -> None:
        for route_id, command, failure in (
            ("fixture-route", self.adoption(expected_fingerprint="0" * 64), GrantFailure("conflict")),
            ("fixture-route", self.adoption(resource_type="audio-job"), GrantFailure("invalid")),
            ("missing-route", self.adoption(), GrantFailure("not_found")),
        ):
            with self.subTest(route_id=route_id, command=command):
                self.assertEqual(await adopt_resource(self.database, route_id, command), failure)
        self.assertEqual(await self.rows(), [])

    async def test_adoption_never_takes_over_a_resource_of_another_application(self) -> None:
        await record_ownership(self.database, claim(resource_id="legacy_7"))
        result: Final = await adopt_resource(self.database, "fixture-route", self.adoption(application_id="other-app"))
        self.assertIsInstance(result, OwnershipConflict)
        self.assertEqual(await self.rows(), [("legacy_7", "fixture-app", "created", CREATOR)])

    async def test_adoption_waits_for_a_competing_route_change_and_then_refuses_the_stale_fingerprint(self) -> None:
        writer: Final = await psycopg.AsyncConnection.connect(DSN)
        try:
            await writer.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            await writer.execute(
                'SELECT param_value FROM "LiteLLM_Config" WHERE param_name = %s FOR UPDATE', ("general_settings",)
            )
            await writer.execute(
                'UPDATE "LiteLLM_Config" SET param_value = jsonb_set(param_value, %s, %s::jsonb)',
                (["pass_through_endpoints", "0", "target"], '"https://vendor.example/v2"'),
            )
            adoption_database: Final = PostgresDatabase(self.schema)
            adoption: Final = asyncio.create_task(adopt_resource(adoption_database, "fixture-route", self.adoption()))
            backend: Final = await asyncio.wait_for(adoption_database.connections.get(), timeout=5)
            for _ in range(100):
                self.assertFalse(adoption.done(), "Adoption read configuration that a writer still held")
                waiting = await (
                    await self.connection.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (backend,)
                    )
                ).fetchone()
                if waiting == ("Lock",):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Adoption did not wait on the configuration row lock")
            await writer.commit()
            self.assertEqual(await adoption, GrantFailure("conflict"))
        finally:
            await writer.close()
        self.assertEqual(await self.rows(), [])

    async def test_a_route_change_waits_until_a_running_adoption_has_committed(self) -> None:
        entered: Final = asyncio.Event()
        release: Final = asyncio.Event()
        adoption: Final = asyncio.create_task(
            adopt_resource(
                PostgresDatabase(self.schema, entered=entered, release=release), "fixture-route", self.adoption()
            )
        )
        writer: Final = await psycopg.AsyncConnection.connect(DSN)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await writer.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            change: Final = asyncio.create_task(
                writer.execute(
                    'UPDATE "LiteLLM_Config" SET param_value = jsonb_set(param_value, %s, %s::jsonb)',
                    (["pass_through_endpoints", "0", "target"], '"https://vendor.example/v2"'),
                )
            )
            for _ in range(100):
                self.assertFalse(change.done(), "Route change passed configuration still locked by the adoption")
                waiting = await (
                    await self.connection.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (writer.info.backend_pid,)
                    )
                ).fetchone()
                if waiting == ("Lock",):
                    break
                await asyncio.sleep(0.01)
            else:
                self.fail("Route change did not wait for the adoption transaction")
            release.set()
            recorded: Final = await adoption
            await change
            await writer.commit()
        finally:
            release.set()
            await writer.close()
            if not adoption.done():
                adoption.cancel()
        self.assertIsInstance(recorded, OwnershipRecorded)
        self.assertEqual(await self.rows(), [("legacy_7", "fixture-app", "adopted", None)])
        target: Final = await (
            await self.connection.execute(
                "SELECT param_value #>> '{pass_through_endpoints,0,target}' FROM \"LiteLLM_Config\""
            )
        ).fetchone()
        self.assertEqual(target, ("https://vendor.example/v2",))
        stale: Final = await adopt_resource(self.database, "fixture-route", self.adoption(resource_id="legacy_8"))
        self.assertEqual(stale, GrantFailure("conflict"))
        self.assertEqual(await self.rows(), [("legacy_7", "fixture-app", "adopted", None)])

    async def test_an_unverifiable_adoption_is_rolled_back(self) -> None:
        with self.assertRaises(RuntimeError):
            await adopt_resource(PostgresDatabase(self.schema, hide_readback=True), "fixture-route", self.adoption())
        self.assertEqual(await self.rows(), [])
