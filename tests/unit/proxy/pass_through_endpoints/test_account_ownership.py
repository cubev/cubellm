from typing import Final

import pytest

from litellm.proxy.pass_through_endpoints.account_credentials import AccountHeaderCredential, CredentialHeader
from litellm.proxy.pass_through_endpoints.account_ownership import (
    ResourceOwnership,
    resource_access_allowed,
    resource_path_match,
)

ROUTE_ID: Final = "media-route"
ITEM: Final = "/cube_api/media/jobs/{resource_id}"
CANCEL: Final = "/cube_api/media/jobs/{resource_id}/cancel"
CREATOR: Final = "a" * 64
ROTATED: Final = "b" * 64
OWNED: Final = ResourceOwnership(
    route_id=ROUTE_ID, owner_application_id="media-app", provenance="created", creator_key_hash=CREATOR
)


def grant(key_hash: str, path: str, methods: tuple[str, ...]) -> dict[str, object]:
    return {
        "application_id": "media-app",
        "key_hash": key_hash,
        "account_id": "media-account",
        "path": path,
        "methods": list(methods),
    }


POLICY: Final = AccountHeaderCredential.model_validate(
    {
        "account_id": "media-account",
        "credential_name": "media credential",
        "headers": [CredentialHeader(name="X-Api-Key", field="api_key").model_dump()],
        "resource_types": [{"name": "video-job", "item_paths": [ITEM, CANCEL]}],
        "application_grants": [
            grant(CREATOR, ITEM, ("GET",)),
            grant(ROTATED, ITEM, ("GET",)),
            grant(CREATOR, CANCEL, ("POST",)),
        ],
    }
)


class Lookup:
    def __init__(self, ownership: ResourceOwnership | None = OWNED, failure: Exception | None = None) -> None:
        self.ownership: Final = ownership
        self.failure: Final = failure
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, account_id: str, resource_type: str, resource_id: str, /) -> ResourceOwnership | None:
        self.calls.append((account_id, resource_type, resource_id))
        if self.failure is not None:
            raise self.failure
        return self.ownership


async def allowed(
    path: str,
    method: str = "GET",
    key_hash: str | None = CREATOR,
    application_id: str | None = "media-app",
    route_id: str | None = ROUTE_ID,
    lookup: Lookup | None = None,
) -> bool:
    match: Final = resource_path_match(POLICY, path)
    assert match is not None
    return await resource_access_allowed(
        POLICY, match, route_id, application_id, key_hash, method, lookup if lookup is not None else Lookup()
    )


async def test_owner_application_reaches_each_granted_item_operation_with_the_owned_id() -> None:
    lookup: Final = Lookup()
    assert await allowed("/cube_api/media/jobs/vid_1", lookup=lookup)
    assert await allowed("/cube_api/media/jobs/vid_1/cancel", "POST", lookup=lookup)
    assert lookup.calls == [("media-account", "video-job", "vid_1"), ("media-account", "video-job", "vid_1")]


async def test_a_rotated_key_with_its_own_grant_keeps_access_to_resources_created_by_the_old_key() -> None:
    assert await allowed("/cube_api/media/jobs/vid_1", key_hash=ROTATED)
    assert not await allowed("/cube_api/media/jobs/vid_1/cancel", "POST", key_hash=ROTATED)


@pytest.mark.parametrize(
    ("path", "method", "key_hash", "application_id", "route_id"),
    (
        ("/cube_api/media/jobs/vid_1", "POST", CREATOR, "media-app", ROUTE_ID),
        ("/cube_api/media/jobs/vid_1", "GET", "c" * 64, "media-app", ROUTE_ID),
        ("/cube_api/media/jobs/vid_1", "GET", CREATOR, "other-app", ROUTE_ID),
        ("/cube_api/media/jobs/vid_1", "GET", None, "media-app", ROUTE_ID),
        ("/cube_api/media/jobs/vid_1", "GET", CREATOR, None, ROUTE_ID),
        ("/cube_api/media/jobs/vid_1", "GET", CREATOR, "media-app", None),
    ),
)
async def test_missing_or_foreign_grants_are_denied(
    path: str, method: str, key_hash: str | None, application_id: str | None, route_id: str | None
) -> None:
    assert not await allowed(path, method, key_hash, application_id, route_id)


@pytest.mark.parametrize(
    "ownership",
    (
        None,
        OWNED.model_copy(update={"owner_application_id": "other-app"}),
        OWNED.model_copy(update={"route_id": "other-route"}),
    ),
)
async def test_unowned_or_foreign_resources_are_denied(ownership: ResourceOwnership | None) -> None:
    assert not await allowed("/cube_api/media/jobs/vid_1", lookup=Lookup(ownership))


async def test_lookup_failures_deny_access() -> None:
    assert not await allowed("/cube_api/media/jobs/vid_1", lookup=Lookup(failure=RuntimeError("database down")))


@pytest.mark.parametrize("segment", ("a+b", "a!b", "a$b", "", "{resource_id}", "x" * 257))
async def test_non_canonical_item_ids_are_denied_without_an_ownership_lookup(segment: str) -> None:
    lookup: Final = Lookup()
    assert not await allowed(f"/cube_api/media/jobs/{segment}", lookup=lookup)
    assert not await allowed(f"/cube_api/media/jobs/{segment}/cancel", "POST", lookup=lookup)
    assert lookup.calls == []


@pytest.mark.parametrize("path", ("/cube_api/media/jobs", "/cube_api/media/jobs/vid_1/cancelled"))
def test_other_operations_are_not_treated_as_resource_items(path: str) -> None:
    assert resource_path_match(POLICY, path) is None
