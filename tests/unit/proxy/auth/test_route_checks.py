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


RESOURCE_ENTRY: Final = "resource:/cube_api/media/jobs/{resource_id}/cancel"


@pytest.mark.parametrize(
    ("route", "allowed"),
    (
        ("/cube_api/media/jobs/vid_1/cancel", True),
        ("/cube_api/media/jobs/a:b@c-d.e~f/cancel", True),
        ("/cube_api/media/jobs/vid_1", False),
        ("/cube_api/media/jobs/vid_1/cancel/now", False),
        ("/cube_api/media/jobs/vid_1/cancelled", False),
        ("/cube_api/media/jobs//cancel", False),
        ("/cube_api/media/jobs/a+b/cancel", False),
        ("/cube_api/media/jobs/a%2fb/cancel", False),
        ("/cube_api/media/jobs/{resource_id}/cancel", False),
        ("/cube_api/media/jobs/../cancel", False),
        ("/cube_api/media/jobs/vid_1/../cancel", False),
    ),
)
def test_resource_key_routes_allow_one_canonical_id_segment_between_literal_segments(route: str, allowed: bool) -> None:
    token: Final = UserAPIKeyAuth(
        allowed_routes=[RESOURCE_ENTRY], metadata={"allowed_passthrough_routes": [RESOURCE_ENTRY]}
    )
    assert RouteChecks.check_passthrough_route_access(route, token) is allowed
    if allowed:
        assert RouteChecks.is_virtual_key_allowed_to_call_route(route, token)
        return
    with pytest.raises(HTTPException) as denied:
        RouteChecks.is_virtual_key_allowed_to_call_route(route, token)
    assert denied.value.status_code == 403


@pytest.mark.parametrize(
    "entry",
    (
        "resource:/cube_api/media/jobs/*",
        "resource:/cube_api/media/jobs/{id}",
        "resource:/cube_api/media/jobs/x{resource_id}",
        "resource:/cube_api/media/{resource_id}/{resource_id}",
        "resource:cube_api/media/jobs/{resource_id}",
        "resource:",
    ),
)
def test_malformed_resource_entries_grant_nothing(entry: str) -> None:
    token: Final = UserAPIKeyAuth(allowed_routes=[entry], metadata={"allowed_passthrough_routes": [entry]})
    for route in ("/cube_api/media/jobs/vid_1", "/cube_api/media/vid_1/vid_1", "/cube_api/media/jobs/*"):
        assert not RouteChecks.check_passthrough_route_access(route, token)
        with pytest.raises(HTTPException):
            RouteChecks.is_virtual_key_allowed_to_call_route(route, token)
