import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import JsonValue, ValidationError

from litellm.proxy.pass_through_endpoints.account_grants import GrantFailure, describe_account_route
from litellm.proxy.pass_through_endpoints.account_routes import (
    APPEND_ROUTE_SQL,
    AccountRouteCreateCommand,
    AccountRouteDefinition,
    AccountRouteReceipt,
    application_route_patterns,
    create_account_route,
    plan_account_route,
    serving_route_patterns,
)

DEFINITION: Final = AccountRouteDefinition.model_validate(
    {
        "id": "fixture-route",
        "path": "/cube_api/fixture/quota",
        "target": "https://vendor.example/v1/quota",
        "methods": ["GET", "POST"],
        "account_credential": {
            "account_id": "fixture-account",
            "credential_name": "fixture-credential",
            "headers": [{"name": "X-Api-Key", "field": "api_key"}],
        },
    }
)
EMPTY_JSON_OBJECT: Final[dict[str, JsonValue]] = {}
GRANT: Final[dict[str, JsonValue]] = {
    "application_id": "fixture-app",
    "key_hash": "a" * 64,
    "account_id": "fixture-account",
    "path": "/cube_api/fixture/quota",
    "methods": ["GET"],
}


def settings(*routes: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return MappingProxyType({"unrelated": {"keep": 1.10}, "pass_through_endpoints": [dict(route) for route in routes]})


def route(**changes: JsonValue) -> dict[str, JsonValue]:
    return {"id": "other-route", "path": "/vendor/other", "target": "https://other.example", **changes}


def test_new_route_is_stored_authenticated_without_forwarding_and_describable() -> None:
    assert plan_account_route(settings(route()), DEFINITION) is True
    assert plan_account_route(MappingProxyType({}), DEFINITION) is True
    assert plan_account_route(MappingProxyType({"pass_through_endpoints": None}), DEFINITION) is True
    stored: Final = DEFINITION.stored()
    assert stored["auth"] is True and stored["forward_headers"] is False
    descriptor: Final = describe_account_route(settings(stored), DEFINITION.id)
    assert not isinstance(descriptor, GrantFailure)
    assert (descriptor.path, descriptor.methods, descriptor.grants) == (DEFINITION.path, DEFINITION.methods, ())
    assert descriptor.origin == "https://vendor.example"


def test_identical_retry_is_a_noop_even_after_grants_were_appended() -> None:
    stored: Final = DEFINITION.stored()
    credential: Final = stored["account_credential"]
    assert isinstance(credential, dict)
    granted: Final[dict[str, JsonValue]] = {
        **stored,
        "account_credential": {**credential, "application_grants": [GRANT]},
    }
    assert plan_account_route(settings(stored), DEFINITION) is False
    assert plan_account_route(settings(granted), DEFINITION) is False


@pytest.mark.parametrize(
    "changes",
    (
        {"target": "https://vendor.example/v2/quota"},
        {"methods": ["GET"]},
        {"include_subpath": True},
        {"path": "/cube_api/fixture/other"},
        {"headers": {"X-Extra": "value"}},
    ),
)
def test_same_id_with_different_content_conflicts(changes: Mapping[str, JsonValue]) -> None:
    assert plan_account_route(settings({**DEFINITION.stored(), **changes}), DEFINITION) == GrantFailure("conflict")


def test_duplicate_existing_ids_conflict() -> None:
    assert plan_account_route(settings(DEFINITION.stored(), DEFINITION.stored()), DEFINITION) == GrantFailure(
        "conflict"
    )


@pytest.mark.parametrize(
    "value",
    (
        MappingProxyType({"pass_through_endpoints": {"id": "fixture-route"}}),
        MappingProxyType({"pass_through_endpoints": EMPTY_JSON_OBJECT}),
        MappingProxyType({"pass_through_endpoints": ""}),
        settings({"id": "broken"}),
        settings(route(include_subpath="yes")),
    ),
)
def test_unreadable_existing_routes_are_invalid(value: Mapping[str, JsonValue]) -> None:
    assert plan_account_route(value, DEFINITION) == GrantFailure("invalid")


@pytest.mark.parametrize(
    "changes",
    (
        {"path": "/fixture/quota"},
        {"path": "/cube_api/"},
        {"path": "/cube_api/fixture/"},
        {"path": "/cube_api/a//b"},
        {"path": "/cube_api/../key/generate"},
        {"path": "/cube_api/a%2fb"},
        {"path": "/cube_api/*"},
        {"methods": []},
        {"methods": ["get"]},
        {"methods": ["GET", "GET"]},
        {"target": "http://vendor.example/quota"},
        {"target": "https://vendor.example/quota?key=1"},
        {"target": "https://user:pass@vendor.example/quota"},
        {"target": "https://vendor example/quota"},
        {"target": "https://vendor%20example/quota"},
        {"target": "https://vendor%0aexample/quota"},
        {"target": "https://vendor.example/a b"},
        {"target": "https://[::1/quota"},
        {"extra": True},
        {
            "account_credential": {
                **DEFINITION.account_credential.model_dump(mode="json"),
                "application_grants": [GRANT],
            }
        },
    ),
)
def test_definition_rejects_unsafe_or_unmanaged_routes(changes: Mapping[str, JsonValue]) -> None:
    with pytest.raises(ValidationError):
        AccountRouteDefinition.model_validate(DEFINITION.model_dump(mode="json") | dict(changes))


@pytest.mark.parametrize(
    ("target", "origin"),
    (
        ("https://api_service.local/quota", "https://api_service.local"),
        ("https://[fe80::1%25eth0]/quota", "https://[fe80::1%25eth0]"),
        ("https://vendor.example:8443/a%20b", "https://vendor.example:8443"),
    ),
)
def test_definition_keeps_internal_hosts_zones_and_encoded_paths(target: str, origin: str) -> None:
    definition: Final = AccountRouteDefinition.model_validate(DEFINITION.model_dump(mode="json") | {"target": target})
    descriptor: Final = describe_account_route(settings(definition.stored()), definition.id)
    assert not isinstance(descriptor, GrantFailure)
    assert descriptor.origin == origin


def test_literal_loopback_targets_may_use_plain_http() -> None:
    local: Final = AccountRouteDefinition.model_validate(
        DEFINITION.model_dump(mode="json") | {"target": "http://127.0.0.1:9000/quota"}
    )
    assert local.stored()["target"] == "http://127.0.0.1:9000/quota"


@dataclass(frozen=True, slots=True)
class RouterCase:
    candidate: str
    include_subpath: bool
    conflicts: bool
    stored: tuple[tuple[str, bool], ...] = ()
    registered: tuple[str, ...] = ()
    mounted: tuple[str, ...] = ()
    probes: tuple[str, ...] = ("/child", "/child/deeper", "/v1/batches/1", "/mcp", "/7", "/end")


ROUTER_CASES: Final = (
    RouterCase("/cube_api/review/quota", False, True, stored=(("/cube_api/{rest:path}", False),)),
    RouterCase("/cube_api/review/quota", False, True, stored=(("/cube_api/review/{name}", False),)),
    RouterCase("/cube_api/review/quota", False, False, stored=(("/cube_api/review/{name}/details", False),)),
    RouterCase("/cube_api/review/quota", True, True, stored=(("/cube_api/review/{name}/details", False),)),
    RouterCase("/cube_api/review/quota", True, False, stored=(("/cube_api/other/{rest:path}", False),)),
    RouterCase("/cube_api/review/quota", True, True, stored=(("/cube_api/{rest:path}/end", False),)),
    RouterCase("/cube_api/review", True, False, stored=(("/cube_api", False),)),
    RouterCase("/cube_api/review/quota/child", False, True, stored=(("/cube_api/review/quota", True),)),
    RouterCase("/cube_api/review/quota", True, True, stored=(("/cube_api/review/quota/child", False),)),
    RouterCase("/cube_api/review/quota", False, False, stored=(("/cube_api/review/quot", True),)),
    RouterCase("/cube_api/review/quota", True, False, stored=(("/cube_api/review/quota-extra", True),)),
    RouterCase("/cube_api/review/quota", False, False, stored=(("/cube_api/review/{id:int}", False),)),
    RouterCase("/cube_api/mcp", False, True, registered=("/{mcp_server_name}/mcp",)),
    RouterCase("/cube_api/review/quota", True, False, registered=("/{mcp_server_name}/mcp",)),
    RouterCase("/cube_api/v1", True, True, registered=("/{provider}/v1/batches/{batch_id:path}",)),
    RouterCase("/cube_api/review", True, False, registered=("/{provider}/v1/batches/{batch_id:path}",)),
    RouterCase("/cube_api/legacy/quota", False, True, mounted=("/cube_api/legacy",)),
    RouterCase("/cube_api", True, True, mounted=("/cube_api/legacy",)),
    RouterCase("/cube_api/legacyx", True, False, mounted=("/cube_api/legacy",)),
)


def candidate(case: RouterCase) -> AccountRouteDefinition:
    return DEFINITION.model_copy(update={"path": case.candidate, "include_subpath": case.include_subpath})


def catch_all_application() -> FastAPI:
    app: Final = FastAPI()

    async def existing() -> str:
        return "existing"

    app.add_api_route("/{rest:path}", existing, methods=["GET"])
    return app


def router_with_existing_routes(case: RouterCase) -> FastAPI:
    app: Final = FastAPI()

    async def existing() -> str:
        return "existing"

    for path, include_subpath in case.stored:
        app.add_api_route(path, existing, methods=["GET"])
        if include_subpath:
            app.add_api_route(path + "/{subpath:path}", existing, methods=["GET"])
    for path in case.registered:
        app.add_api_route(path, existing, methods=["GET", "POST"])
    for path in case.mounted:
        app.mount(path, catch_all_application())
    return app


async def answered_by_candidate(app: FastAPI, case: RouterCase) -> bool:
    async def account_route() -> str:
        return "candidate"

    app.add_api_route(case.candidate, account_route, methods=["GET"])
    if case.include_subpath:
        app.add_api_route(case.candidate + "/{subpath:path}", account_route, methods=["GET"])
    paths: Final = (case.candidate, *(case.candidate + probe for probe in case.probes if case.include_subpath))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses: Final = await asyncio.gather(*(client.get(path) for path in paths))
    return all(response.json() == "candidate" for response in responses)


@pytest.mark.parametrize("case", ROUTER_CASES)
async def test_planner_rejects_every_route_the_real_router_would_shadow(case: RouterCase) -> None:
    app: Final = router_with_existing_routes(case)
    registered: Final = application_route_patterns(app.router.routes)
    stored: Final = settings(
        *(
            route(id="existing-" + str(index), path=path, include_subpath=include_subpath)
            for index, (path, include_subpath) in enumerate(case.stored)
        )
    )
    plan: Final = plan_account_route(stored, candidate(case), () if case.stored else registered)
    assert plan == (GrantFailure("conflict") if case.conflicts else True)
    if plan is True:
        assert await answered_by_candidate(app, case)


async def test_review_reproduction_is_really_shadowed_by_the_router() -> None:
    case: Final = ROUTER_CASES[0]
    assert not await answered_by_candidate(router_with_existing_routes(case), case)


@pytest.mark.parametrize("path", ("/cube_api/{rest:unknown}", "/cube_api/{name}/{name}"))
def test_patterns_the_router_cannot_compile_are_treated_as_conflicts(path: str) -> None:
    assert plan_account_route(settings(route(path=path)), DEFINITION) == GrantFailure("conflict")


def test_same_path_with_other_methods_is_rejected_conservatively() -> None:
    other_method: Final = route(path=DEFINITION.path, methods=["DELETE"])
    assert plan_account_route(settings(other_method), DEFINITION) == GrantFailure("conflict")


async def test_serving_application_routes_are_read_from_the_live_request() -> None:
    app: Final = FastAPI()
    shadowed: Final = DEFINITION.model_copy(update={"path": "/cube_api/legacy/quota"})

    async def legacy() -> str:
        return "legacy"

    async def probe(request: Request) -> bool:
        return plan_account_route(MappingProxyType({}), shadowed, serving_route_patterns(request.scope)) is True

    app.add_api_route("/probe", probe, methods=["GET"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        allowed_before: Final = (await client.get("/probe")).text
        app.add_api_route("/cube_api/legacy/{rest:path}", legacy, methods=["GET"])
        allowed_after: Final = (await client.get("/probe")).text
    assert (allowed_before, allowed_after) == ("true", "false")


def test_a_scope_without_a_serving_application_blocks_creation() -> None:
    assert plan_account_route(MappingProxyType({}), DEFINITION, serving_route_patterns({})) == GrantFailure("conflict")


def test_unknown_router_entries_block_every_new_route() -> None:
    app: Final = FastAPI()
    app.router.routes.append(object())  # pyright: ignore[reportArgumentType]  # simulates an unrecognised route type
    patterns: Final = application_route_patterns(app.router.routes)
    assert plan_account_route(MappingProxyType({}), DEFINITION, patterns) == GrantFailure("conflict")


@dataclass(frozen=True, slots=True)
class ScriptedTransaction:
    before: Mapping[str, JsonValue]
    after: Mapping[str, JsonValue]
    updated_rows: int

    async def query_raw(self, query: str, *parameters: str) -> object:
        return ({"param_value": dict(self.before if "FOR UPDATE" in query else self.after)},)

    async def execute_raw(self, query: str, *parameters: str) -> int:
        return self.updated_rows if query == APPEND_ROUTE_SQL else 0


@dataclass(frozen=True, slots=True)
class ScriptedDatabase:
    transaction: ScriptedTransaction

    @asynccontextmanager
    async def tx(self) -> AsyncGenerator[ScriptedTransaction]:
        yield self.transaction


def readback(*routes: Mapping[str, JsonValue], updated_rows: int = 1) -> ScriptedDatabase:
    return ScriptedDatabase(ScriptedTransaction(settings(route()), settings(*routes), updated_rows))


async def test_route_creation_reports_only_a_verified_readback() -> None:
    receipt: Final = await create_account_route(
        readback(route(), DEFINITION.stored()), AccountRouteCreateCommand(route=DEFINITION)
    )
    assert isinstance(receipt, AccountRouteReceipt) and receipt.created


@pytest.mark.parametrize(
    "database",
    (
        readback(route(), DEFINITION.stored(), updated_rows=0),
        readback(route()),
        readback(route(), DEFINITION.stored(), DEFINITION.stored()),
        readback(route(), {**DEFINITION.stored(), "auth": False}),
        readback(route(target="https://changed.example"), DEFINITION.stored()),
        readback(route(), route(id="inserted-route", path="/cube_api/inserted"), DEFINITION.stored()),
        readback(route(), DEFINITION.stored(), route(id="appended-route", path="/cube_api/appended")),
        readback(route(), {**DEFINITION.stored(), "target": "https://vendor.example/v2/quota"}),
    ),
)
async def test_route_creation_refuses_to_commit_an_unexpected_write(database: ScriptedDatabase) -> None:
    with pytest.raises(RuntimeError):
        await create_account_route(database, AccountRouteCreateCommand(route=DEFINITION))


LEGACY_STORED_ROUTE: Final[dict[str, JsonValue]] = {
    "id": "legacy-proof",
    "path": "/cube_api/legacy-proof/quota",
    "target": "https://vendor.example/quota",
    "methods": ["GET"],
    "include_subpath": False,
    "auth": True,
    "forward_headers": False,
    "account_credential": {
        "account_id": "legacy-account",
        "credential_name": "legacy-credential",
        "headers": [{"name": "X-Api-Key", "field": "api_key", "prefix": ""}],
        "allowed_query_parameters": [],
    },
}
LEGACY_FINGERPRINT: Final = "b31c235b29ca20bc01109312b46d27a330f5250f47378bd68df013191c190d2b"
LEGACY_DEFINITION: Final[dict[str, JsonValue]] = {
    "id": "legacy-proof",
    "path": "/cube_api/legacy-proof/quota",
    "target": "https://vendor.example/quota",
    "methods": ["GET"],
    "account_credential": {
        "account_id": "legacy-account",
        "credential_name": "legacy-credential",
        "headers": [{"name": "X-Api-Key", "field": "api_key"}],
    },
}


def test_routes_created_before_resource_types_keep_their_stored_shape_retry_and_fingerprint() -> None:
    definition: Final = AccountRouteDefinition.model_validate(LEGACY_DEFINITION)
    assert definition.stored() == LEGACY_STORED_ROUTE
    assert plan_account_route(settings(LEGACY_STORED_ROUTE), definition) is False
    stored: Final = describe_account_route(settings(LEGACY_STORED_ROUTE), "legacy-proof")
    assert not isinstance(stored, GrantFailure) and stored.fingerprint == LEGACY_FINGERPRINT


def test_declared_resource_types_are_persisted_and_change_the_fingerprint() -> None:
    credential: Final = LEGACY_DEFINITION["account_credential"]
    assert isinstance(credential, dict)
    resource_types: Final[list[JsonValue]] = [
        {"name": "quota-report", "item_paths": ["/cube_api/legacy-proof/quota/{resource_id}"]}
    ]
    definition: Final = AccountRouteDefinition.model_validate(
        LEGACY_DEFINITION
        | {"include_subpath": True, "account_credential": credential | {"resource_types": resource_types}}
    )
    stored: Final = definition.stored()
    stored_credential: Final = stored["account_credential"]
    assert isinstance(stored_credential, dict)
    assert stored_credential["resource_types"] == [
        {"name": "quota-report", "item_paths": ["/cube_api/legacy-proof/quota/{resource_id}"], "creation": None}
    ]
    without: Final = {
        **stored,
        "account_credential": {k: v for k, v in stored_credential.items() if k != "resource_types"},
    }
    with_resources: Final = describe_account_route(settings(stored), "legacy-proof")
    without_resources: Final = describe_account_route(settings(without), "legacy-proof")
    assert not isinstance(with_resources, GrantFailure) and not isinstance(without_resources, GrantFailure)
    assert with_resources.fingerprint != without_resources.fingerprint
    assert plan_account_route(settings(without), definition) == GrantFailure("conflict")


def query_definition(name: str) -> AccountRouteDefinition:
    return AccountRouteDefinition.model_validate(
        {
            **DEFINITION.model_dump(),
            "account_credential": {
                "account_id": "fixture-account",
                "credential_name": "fixture-credential",
                "query": {"name": name, "field": "api_key"},
                "allowed_query_parameters": ["page"],
            },
        }
    )


def test_query_destination_is_stored_only_when_set_and_bound_into_the_fingerprint() -> None:
    header_credential: Final = DEFINITION.stored()["account_credential"]
    assert isinstance(header_credential, dict) and "query" not in header_credential
    stored: Final = query_definition("ticket").stored()
    credential: Final = stored["account_credential"]
    assert isinstance(credential, dict)
    assert (credential["headers"], credential["query"]) == ([], {"name": "ticket", "field": "api_key", "prefix": ""})
    assert plan_account_route(settings(stored), query_definition("ticket")) is False
    assert plan_account_route(settings(stored), query_definition("token")) == GrantFailure("conflict")
    fingerprints: Final = tuple(
        describe_account_route(settings(query_definition(name).stored()), DEFINITION.id) for name in ("ticket", "token")
    )
    assert not any(isinstance(descriptor, GrantFailure) for descriptor in fingerprints)
    assert len({descriptor.fingerprint for descriptor in fingerprints if not isinstance(descriptor, GrantFailure)}) == 2
