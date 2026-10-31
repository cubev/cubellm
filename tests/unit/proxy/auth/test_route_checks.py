from typing import Final

import pytest
from fastapi import HTTPException

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.route_checks import RouteChecks


@pytest.mark.parametrize(
    ("route", "allowed"),
    (
        ("/fixture/quota", True),
        ("/fixture/quota/child", False),
        ("/fixture/quota/", False),
        ("/fixture/quota-extra", False),
        ("/fixture/other", False),
        ("/key/info", False),
    ),
)
def test_exact_key_route_does_not_grant_children_or_inherited_team_routes(route: str, allowed: bool) -> None:
    token: Final = UserAPIKeyAuth(
        allowed_routes=["exact:/fixture/quota"],
        metadata={},
        team_metadata={"allowed_passthrough_routes": [route]},
    )
    assert RouteChecks.check_passthrough_route_access(route, token)
    if allowed:
        assert RouteChecks.is_virtual_key_allowed_to_call_route(route, token)
        return
    with pytest.raises(HTTPException) as denied:
        RouteChecks.is_virtual_key_allowed_to_call_route(route, token)
    assert denied.value.status_code == 403


@pytest.mark.parametrize(
    ("entry", "route", "allowed"),
    (
        ("/fixture/quota", "/fixture/quota/child", True),
        ("/fixture/quota", "/fixture/quota-extra", False),
        ("exact:/fixture/quota", "/fixture/quota", True),
        ("exact:/fixture/quota", "/fixture/quota/child", False),
        ("exact:/fixture/*", "/fixture/quota", False),
        ("exact:openai_routes", "/v1/chat/completions", False),
        ("exact:", "/fixture/quota", False),
    ),
)
def test_exact_route_entries_preserve_existing_prefix_semantics(entry: str, route: str, allowed: bool) -> None:
    token: Final = UserAPIKeyAuth(allowed_routes=[entry])
    if allowed:
        assert RouteChecks.is_virtual_key_allowed_to_call_route(route, token)
        return
    with pytest.raises(HTTPException) as denied:
        RouteChecks.is_virtual_key_allowed_to_call_route(route, token)
    assert denied.value.status_code == 403
