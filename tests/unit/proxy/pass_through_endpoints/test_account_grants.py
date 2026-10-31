import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

import pytest
from pydantic import JsonValue

from litellm.proxy.pass_through_endpoints.account_credentials import AccountApplicationGrant
from litellm.proxy.pass_through_endpoints.account_grants import (
    AccountGrantCommand,
    AccountRouteDescriptor,
    GrantFailure,
    GrantPlan,
    describe_account_route,
    plan_account_grant,
)

ROUTE: Final[Mapping[str, JsonValue]] = MappingProxyType(
    {
        "id": "fixture-route",
        "path": "/fixture/quota",
        "target": "https://vendor.example/quota",
        "methods": ["GET", "POST"],
        "auth": True,
        "headers": {"X-Fixture-Option": "inert-private-value"},
        "account_credential": {
            "account_id": "fixture-account",
            "credential_name": "fixture-credential",
            "headers": [{"name": "X-Api-Key", "field": "api_key"}],
        },
    }
)
SETTINGS: Final[Mapping[str, JsonValue]] = MappingProxyType(
    {
        "unrelated": {"nested": ["preserve", 3]},
        "pass_through_endpoints": [dict(ROUTE), {"id": "another-route", "opaque": "preserve"}],
    }
)
GRANT: Final = AccountApplicationGrant(
    application_id="fixture-app",
    key_hash="a" * 64,
    account_id="fixture-account",
    path="/fixture/quota",
    methods=("GET",),
)


def settings_with_route(route: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return MappingProxyType({**SETTINGS, "pass_through_endpoints": [dict(route)]})


def settings_with_grants(grants: tuple[AccountApplicationGrant, ...]) -> Mapping[str, JsonValue]:
    return settings_with_route(
        {
            **ROUTE,
            "account_credential": {
                "account_id": "fixture-account",
                "credential_name": "fixture-credential",
                "headers": [{"name": "X-Api-Key", "field": "api_key"}],
                "application_grants": [grant.model_dump(mode="json") for grant in grants],
            },
        }
    )


def command(settings: Mapping[str, JsonValue], grant: AccountApplicationGrant = GRANT) -> AccountGrantCommand:
    descriptor: Final = describe_account_route(settings, "fixture-route")
    assert isinstance(descriptor, AccountRouteDescriptor)
    return AccountGrantCommand(expected_fingerprint=descriptor.fingerprint, grant=grant)


def test_account_grant_append_preserves_other_settings_and_fingerprint_without_exposing_source_values() -> None:
    original: Final = json.dumps(dict(SETTINGS), sort_keys=True)
    request: Final = command(SETTINGS)
    plan: Final = plan_account_grant(SETTINGS, "fixture-route", request)
    assert isinstance(plan, GrantPlan) and plan.changed
    assert plan.descriptor.grants == (GRANT,)
    assert plan.descriptor.fingerprint == request.expected_fingerprint
    assert plan.route_index == 0
    assert json.dumps(dict(SETTINGS), sort_keys=True) == original
    assert "inert-private-value" not in plan.descriptor.model_dump_json()
    assert "inert-private-value" not in repr(plan)


def test_account_grant_retry_is_an_identity_noop_and_other_applications_can_be_added() -> None:
    request: Final = command(SETTINGS)
    first: Final = plan_account_grant(SETTINGS, "fixture-route", request)
    assert isinstance(first, GrantPlan)
    settings: Final = settings_with_grants(first.descriptor.grants)
    duplicate: Final = plan_account_grant(settings, "fixture-route", request)
    assert isinstance(duplicate, GrantPlan) and not duplicate.changed
    assert duplicate.descriptor == first.descriptor
    other: Final = GRANT.model_copy(update={"application_id": "other-app", "key_hash": "b" * 64})
    second: Final = plan_account_grant(
        settings, "fixture-route", AccountGrantCommand(expected_fingerprint=request.expected_fingerprint, grant=other)
    )
    assert isinstance(second, GrantPlan)
    assert second.descriptor.grants == (GRANT, other)


@pytest.mark.parametrize(
    "changes",
    (
        {"target": "https://other.example/quota"},
        {"methods": ["GET"]},
        {"headers": {"X-Fixture-Option": "changed"}},
        {"include_subpath": True},
    ),
)
def test_account_grant_refuses_a_changed_route_snapshot(changes: Mapping[str, JsonValue]) -> None:
    changed: Final = settings_with_route({**ROUTE, **changes})
    assert plan_account_grant(changed, "fixture-route", command(SETTINGS)) == GrantFailure("conflict")


@pytest.mark.parametrize(
    "grant",
    (
        GRANT.model_copy(update={"account_id": "other-account"}),
        GRANT.model_copy(update={"path": "/other/quota"}),
        GRANT.model_copy(update={"path": "/fixture/quota-extra"}),
        GRANT.model_copy(update={"path": "/fixture/quota/child"}),
        GRANT.model_copy(update={"methods": ("DELETE",)}),
    ),
)
def test_account_grant_refuses_scope_outside_the_registered_route(grant: AccountApplicationGrant) -> None:
    assert plan_account_grant(SETTINGS, "fixture-route", command(SETTINGS, grant)) == GrantFailure("invalid")


def test_account_grant_can_name_one_concrete_registered_subpath_without_granting_siblings() -> None:
    settings: Final = settings_with_route({**ROUTE, "include_subpath": True})
    child: Final = GRANT.model_copy(update={"path": "/fixture/quota/child"})
    plan: Final = plan_account_grant(settings, "fixture-route", command(settings, child))
    assert isinstance(plan, GrantPlan)
    assert plan.descriptor.grants == (child,)
    sibling: Final = GRANT.model_copy(update={"path": "/fixture/quota-extra/child"})
    assert plan_account_grant(settings, "fixture-route", command(settings, sibling)) == GrantFailure("invalid")


def test_account_grant_cannot_silently_expand_the_same_key_path_after_creation() -> None:
    first: Final = plan_account_grant(SETTINGS, "fixture-route", command(SETTINGS))
    assert isinstance(first, GrantPlan)
    settings: Final = settings_with_grants(first.descriptor.grants)
    expanded: Final = GRANT.model_copy(update={"methods": ("GET", "POST")})
    assert plan_account_grant(settings, "fixture-route", command(settings, expanded)) == GrantFailure("conflict")


def test_account_grant_refuses_scope_outside_the_registered_route_with_an_existing_key_path() -> None:
    settings: Final = settings_with_grants((GRANT,))
    foreign: Final = GRANT.model_copy(update={"account_id": "other-account"})
    assert plan_account_grant(settings, "fixture-route", command(settings, foreign)) == GrantFailure("invalid")


def test_account_grant_refuses_missing_and_duplicate_route_ids() -> None:
    assert describe_account_route(SETTINGS, "absent-route") == GrantFailure("not_found")
    duplicated: Final[Mapping[str, JsonValue]] = {"pass_through_endpoints": [dict(ROUTE), dict(ROUTE)]}
    assert describe_account_route(duplicated, "fixture-route") == GrantFailure("conflict")


@pytest.mark.parametrize("changes", ({"auth": False}, {"forward_headers": True}, {"account_credential": None}))
def test_account_grant_refuses_unsafe_or_unbound_route_configuration(changes: Mapping[str, JsonValue]) -> None:
    assert describe_account_route(settings_with_route({**ROUTE, **changes}), "fixture-route") == GrantFailure("invalid")
