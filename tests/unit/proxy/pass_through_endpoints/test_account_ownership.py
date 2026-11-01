import asyncio
from typing import Final

import pytest

from litellm.proxy.pass_through_endpoints.account_credentials import (
    AccountApplicationGrant,
    AccountHeaderCredential,
    CredentialHeader,
)
from litellm.proxy.pass_through_endpoints.account_ownership import (
    CapturedResource,
    CaptureSkip,
    OwnershipConflict,
    OwnershipRecorded,
    ResourceClaim,
    ResourceNotCaptured,
    ResourceOwnership,
    capture_created_resources,
    resource_access_allowed,
    resource_path_match,
)
from litellm.proxy.pass_through_endpoints.account_resources import AccountResourceType, ResourceCreation

ROUTE_ID: Final = "media-route"
ITEM: Final = "/cube_api/media/jobs/{resource_id}"
CANCEL: Final = "/cube_api/media/jobs/{resource_id}/cancel"
CREATOR: Final = "a" * 64
ROTATED: Final = "b" * 64
OWNED: Final = ResourceOwnership(
    route_id=ROUTE_ID, owner_application_id="media-app", provenance="created", creator_key_hash=CREATOR
)


def grant(key_hash: str, path: str, methods: tuple[str, ...]) -> AccountApplicationGrant:
    return AccountApplicationGrant(
        application_id="media-app", key_hash=key_hash, account_id="media-account", path=path, methods=methods
    )


POLICY: Final = AccountHeaderCredential(
    account_id="media-account",
    credential_name="media credential",
    headers=(CredentialHeader(name="X-Api-Key", field="api_key"),),
    resource_types=(AccountResourceType(name="video-job", item_paths=(ITEM, CANCEL)),),
    application_grants=(
        grant(CREATOR, ITEM, ("GET",)),
        grant(ROTATED, ITEM, ("GET",)),
        grant(CREATOR, CANCEL, ("POST",)),
    ),
)
FOREIGN: Final = ResourceOwnership(
    route_id=ROUTE_ID, owner_application_id="other-app", provenance="created", creator_key_hash=CREATOR
)


class Lookup:
    def __init__(self, ownership: ResourceOwnership | None = OWNED, failure: Exception | None = None) -> None:
        self.ownership: Final = ownership
        self.failure: Final = failure
        self.received: Final[asyncio.Queue[tuple[str, str, str]]] = asyncio.Queue()

    def calls(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(self.received.get_nowait() for _ in range(self.received.qsize()))

    async def __call__(self, account_id: str, resource_type: str, resource_id: str, /) -> ResourceOwnership | None:
        self.received.put_nowait((account_id, resource_type, resource_id))
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
    assert lookup.calls() == (("media-account", "video-job", "vid_1"), ("media-account", "video-job", "vid_1"))


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
        FOREIGN,
        ResourceOwnership(
            route_id="other-route", owner_application_id="media-app", provenance="created", creator_key_hash=CREATOR
        ),
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
    assert lookup.calls() == ()


@pytest.mark.parametrize("path", ("/cube_api/media/jobs", "/cube_api/media/jobs/vid_1/cancelled"))
def test_other_operations_are_not_treated_as_resource_items(path: str) -> None:
    assert resource_path_match(POLICY, path) is None


JOBS_CREATE: Final = "/cube_api/media/jobs/create"
CAPTURE_POLICY: Final = AccountHeaderCredential(
    account_id="media-account",
    credential_name="media credential",
    headers=(CredentialHeader(name="X-Api-Key", field="api_key"),),
    resource_types=(
        AccountResourceType(
            name="video-job",
            item_paths=("/cube_api/media/jobs/video/{resource_id}",),
            creation=ResourceCreation(path=JOBS_CREATE, methods=("POST",), id_selector=("video", "id")),
        ),
        AccountResourceType(
            name="audio-job",
            item_paths=("/cube_api/media/jobs/audio/{resource_id}",),
            creation=ResourceCreation(path=JOBS_CREATE, methods=("POST", "PUT"), id_selector=("audio", 0, "id")),
        ),
        AccountResourceType(
            name="file",
            item_paths=("/cube_api/media/files/{resource_id}",),
            creation=ResourceCreation(path="/cube_api/media/files", methods=("POST",), id_selector=("id",)),
        ),
    ),
)
BOTH_CREATED: Final = b'{"video": {"id": "vid_1"}, "audio": [{"id": "aud_1"}]}'


class Recorder:
    def __init__(self, failure: Exception | None = None, conflict_types: frozenset[str] = frozenset()) -> None:
        self.failure: Final = failure
        self.conflict_types: Final = conflict_types
        self.received: Final[asyncio.Queue[tuple[ResourceClaim, ...]]] = asyncio.Queue()

    def calls(self) -> tuple[tuple[ResourceClaim, ...], ...]:
        return tuple(self.received.get_nowait() for _ in range(self.received.qsize()))

    async def __call__(self, claims: tuple[ResourceClaim, ...], /) -> tuple[OwnershipRecorded | OwnershipConflict, ...]:
        self.received.put_nowait(claims)
        if self.failure is not None:
            raise self.failure
        return tuple(
            OwnershipConflict(FOREIGN)
            if claim.resource_type in self.conflict_types
            else OwnershipRecorded(
                ResourceOwnership(
                    route_id=claim.route_id,
                    owner_application_id=claim.owner_application_id,
                    provenance="created",
                    creator_key_hash=claim.creator_key_hash,
                ),
                True,
            )
            for claim in claims
        )


async def capture(
    recorder: Recorder,
    body: bytes | None = BOTH_CREATED,
    status_code: int = 201,
    method: str = "POST",
    path: str = JOBS_CREATE,
    route_id: str | None = ROUTE_ID,
    application_id: str | None = "media-app",
) -> tuple[CapturedResource, ...]:
    return await capture_created_resources(
        CAPTURE_POLICY, route_id, application_id, CREATOR, path, method, status_code, body, recorder
    )


def claim(resource_type: str, resource_id: str) -> ResourceClaim:
    return ResourceClaim(
        account_id="media-account",
        resource_type=resource_type,
        resource_id=resource_id,
        route_id=ROUTE_ID,
        owner_application_id="media-app",
        provenance="created",
        creator_key_hash=CREATOR,
    )


def recorded(resource_type: str) -> CapturedResource:
    return CapturedResource(
        resource_type,
        OwnershipRecorded(
            ResourceOwnership(
                route_id=ROUTE_ID, owner_application_id="media-app", provenance="created", creator_key_hash=CREATOR
            ),
            True,
        ),
    )


async def test_one_creation_response_records_every_resource_type_declared_for_that_request() -> None:
    recorder: Final = Recorder()
    assert await capture(recorder) == (recorded("video-job"), recorded("audio-job"))
    assert recorder.calls() == ((claim("video-job", "vid_1"), claim("audio-job", "aud_1")),)


async def test_types_sharing_a_creation_path_are_only_captured_for_their_own_methods() -> None:
    recorder: Final = Recorder()
    assert await capture(recorder, method="PUT") == (recorded("audio-job"),)
    assert recorder.calls() == ((claim("audio-job", "aud_1"),),)


async def test_a_type_whose_selector_finds_no_id_stays_unowned_while_the_others_are_recorded() -> None:
    recorder: Final = Recorder()
    captured: Final = await capture(recorder, b'{"video": {"id": "vid_1"}, "audio": []}')
    assert captured == (recorded("video-job"), CapturedResource("audio-job", ResourceNotCaptured("missing_id")))
    assert recorder.calls() == ((claim("video-job", "vid_1"),),)


async def test_conflicts_are_reported_per_resource_type() -> None:
    captured: Final = await capture(Recorder(conflict_types=frozenset({"audio-job"})))
    assert captured[0] == recorded("video-job")
    assert isinstance(captured[1].outcome, OwnershipConflict)


@pytest.mark.parametrize(
    ("body", "status_code", "route_id", "application_id", "reason"),
    (
        (BOTH_CREATED, 400, ROUTE_ID, "media-app", "unsuccessful_response"),
        (BOTH_CREATED, 302, ROUTE_ID, "media-app", "unsuccessful_response"),
        (None, 201, ROUTE_ID, "media-app", "unbuffered_response"),
        (b"<html>created</html>", 201, ROUTE_ID, "media-app", "invalid_json"),
        (BOTH_CREATED, 201, None, "media-app", "missing_identity"),
        (BOTH_CREATED, 201, ROUTE_ID, None, "missing_identity"),
        (BOTH_CREATED, 201, "registered route with spaces", "media-app", "capture_failed"),
        (b'{"video": {"id": "a/b"}, "audio": [{"id": ".."}]}', 201, ROUTE_ID, "media-app", "missing_id"),
    ),
)
async def test_responses_without_a_trustworthy_creation_record_nothing(
    body: bytes | None, status_code: int, route_id: str | None, application_id: str | None, reason: CaptureSkip
) -> None:
    recorder: Final = Recorder()
    captured: Final = await capture(recorder, body, status_code, route_id=route_id, application_id=application_id)
    assert captured == tuple(CapturedResource(name, ResourceNotCaptured(reason)) for name in ("video-job", "audio-job"))
    assert recorder.calls() == ()


async def test_a_failing_ownership_store_is_reported_instead_of_raised() -> None:
    recorder: Final = Recorder(failure=RuntimeError("database down"))
    captured: Final = await capture(recorder)
    assert captured == tuple(
        CapturedResource(name, ResourceNotCaptured("capture_failed")) for name in ("video-job", "audio-job")
    )
    assert len(recorder.calls()) == 1


@pytest.mark.parametrize(
    ("path", "method"),
    (
        ("/cube_api/media/jobs/create", "GET"),
        ("/cube_api/media/jobs", "POST"),
        ("/cube_api/media/jobs/video/v", "POST"),
    ),
)
async def test_requests_that_are_not_declared_creations_are_ignored(path: str, method: str) -> None:
    recorder: Final = Recorder()
    assert await capture(recorder, path=path, method=method) == ()
    assert recorder.calls() == ()
