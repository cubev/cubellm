from collections.abc import Sequence
from typing import Final

import pytest
from pydantic import JsonValue, ValidationError

from litellm.proxy.pass_through_endpoints.account_resources import (
    AccountResourceType,
    ResourceCreation,
    ResourcePathMatch,
    canonical_resource_id,
    find_resource,
    resource_id_in_path,
    select_resource_id,
    validate_resource_types,
)

ROUTE: Final = "/cube_api/media"
JOB_CREATION: Final = ResourceCreation(path=f"{ROUTE}/jobs", methods=("POST",), id_selector=("data", 0, "video_id"))
JOBS: Final = AccountResourceType(
    name="video-job",
    item_paths=(f"{ROUTE}/jobs/{{resource_id}}", f"{ROUTE}/jobs/{{resource_id}}/cancel"),
    creation=JOB_CREATION,
)
FILES: Final = AccountResourceType(name="file", item_paths=(f"{ROUTE}/files/{{resource_id}}/content",))


@pytest.mark.parametrize("value", ("vid_123", "a:b@c-d.e~f", "x" * 256, "0"))
def test_canonical_resource_ids_are_single_safe_segments(value: str) -> None:
    assert canonical_resource_id(value)


@pytest.mark.parametrize("value", ("", ".", "..", "a/b", "a%2fb", "a b", "x" * 257, "ü", "a\nb", "a?b", "a#b"))
def test_non_canonical_resource_ids_are_rejected(value: str) -> None:
    assert not canonical_resource_id(value)


@pytest.mark.parametrize(
    ("template", "path", "expected"),
    (
        (f"{ROUTE}/jobs/{{resource_id}}", f"{ROUTE}/jobs/abc", "abc"),
        (f"{ROUTE}/jobs/{{resource_id}}/cancel", f"{ROUTE}/jobs/abc/cancel", "abc"),
        (f"{ROUTE}/files/{{resource_id}}/content", f"{ROUTE}/files/f-1/content", "f-1"),
        (f"{ROUTE}/jobs/{{resource_id}}", f"{ROUTE}/jobs/abc/cancel", None),
        (f"{ROUTE}/jobs/{{resource_id}}/cancel", f"{ROUTE}/jobs/abc", None),
        (f"{ROUTE}/jobs/{{resource_id}}/cancel", f"{ROUTE}/jobs/abc/cancel/", None),
        (f"{ROUTE}/jobs/{{resource_id}}/cancel", f"{ROUTE}/jobs//cancel", None),
        (f"{ROUTE}/jobs/{{resource_id}}/cancel", f"{ROUTE}/jobs/a%2fb/cancel", None),
        (f"{ROUTE}/jobs/{{resource_id}}/cancel", f"{ROUTE}/jobs/../cancel", None),
        (f"{ROUTE}/jobs/{{resource_id}}/cancel", f"{ROUTE}/jobs/abc/cancelled", None),
        (f"{ROUTE}/jobs/{{resource_id}}", f"{ROUTE}/jobsx/abc", None),
        (f"{ROUTE}/jobs/{{resource_id}}", "/cube_api/other/jobs/abc", None),
    ),
)
def test_template_matches_exactly_one_segment_between_literal_segments(
    template: str, path: str, expected: str | None
) -> None:
    assert resource_id_in_path(template, path) == expected


@pytest.mark.parametrize(
    "item_path",
    (
        f"{ROUTE}/jobs",
        f"{ROUTE}/jobs/{{resource_id}}/{{resource_id}}",
        f"{ROUTE}/jobs/x{{resource_id}}",
        f"{ROUTE}/jobs/{{id}}",
        f"{ROUTE}//{{resource_id}}",
        f"{ROUTE}/../{{resource_id}}",
        f"{ROUTE}/*/{{resource_id}}",
        f"{ROUTE}/%2f/{{resource_id}}",
        "cube_api/jobs/{resource_id}",
    ),
)
def test_item_paths_need_one_whole_placeholder_segment_and_canonical_literals(item_path: str) -> None:
    with pytest.raises(ValidationError):
        AccountResourceType(name="job", item_paths=(item_path,))


@pytest.mark.parametrize(
    ("document", "selector", "expected"),
    (
        ({"data": [{"video_id": "v1"}]}, ("data", 0, "video_id"), "v1"),
        ({"id": "top"}, ("id",), "top"),
        ({"data": [{"video_id": "v1"}]}, ("data", 1, "video_id"), None),
        ({"data": [{"video_id": "v1"}]}, ("data", "0", "video_id"), None),
        ({"data": {"0": {"video_id": "v1"}}}, ("data", 0, "video_id"), None),
        ({"data": [{"video_id": 7}]}, ("data", 0, "video_id"), None),
        ({"data": [{"video_id": "a/b"}]}, ("data", 0, "video_id"), None),
        ({"data": [{"video_id": None}]}, ("data", 0, "video_id"), None),
        ({"data": [{"other": "v1"}]}, ("data", 0, "video_id"), None),
        (["v1"], ("id",), None),
    ),
)
def test_selector_walks_explicit_keys_and_indexes_to_one_canonical_string(
    document: JsonValue, selector: Sequence[str | int], expected: str | None
) -> None:
    assert select_resource_id(document, selector) == expected


@pytest.mark.parametrize("selector", ((), (True,), (-1,), ("",), ("a",) * 9, (1.5,)))
def test_selector_definitions_are_bounded_and_strictly_typed(selector: tuple[object, ...]) -> None:
    with pytest.raises(ValidationError):
        ResourceCreation.model_validate({"path": f"{ROUTE}/jobs", "methods": ["POST"], "id_selector": selector})


def test_route_with_multiple_operations_per_resource_type_is_accepted_and_resolves_paths() -> None:
    validate_resource_types(ROUTE, True, (JOBS, FILES))
    assert find_resource((JOBS, FILES), f"{ROUTE}/jobs/abc/cancel") == ResourcePathMatch(
        "video-job", JOBS.item_paths[1], "abc"
    )
    assert find_resource((JOBS, FILES), f"{ROUTE}/files/f-1/content") == ResourcePathMatch(
        "file", FILES.item_paths[0], "f-1"
    )
    assert find_resource((JOBS, FILES), f"{ROUTE}/jobs") is None
    assert find_resource((JOBS, FILES), f"{ROUTE}/files/f-1") is None


@pytest.mark.parametrize(
    ("include_subpath", "resource_types"),
    (
        (False, (JOBS,)),
        (True, (JOBS, JOBS.model_copy(update={"item_paths": (f"{ROUTE}/other/{{resource_id}}",)}))),
        (True, (AccountResourceType(name="outside", item_paths=("/cube_api/other/{resource_id}",)),)),
        (True, (AccountResourceType(name="prefix", item_paths=(f"{ROUTE}x/{{resource_id}}",)),)),
        (
            True,
            (
                AccountResourceType(name="first", item_paths=(f"{ROUTE}/{{resource_id}}/content",)),
                AccountResourceType(name="second", item_paths=(f"{ROUTE}/files/{{resource_id}}",)),
            ),
        ),
        (True, (JOBS.model_copy(update={"creation": JOB_CREATION.model_copy(update={"path": f"{ROUTE}/jobs/new"})}),)),
        (
            True,
            (JOBS.model_copy(update={"creation": JOB_CREATION.model_copy(update={"path": "/cube_api/elsewhere"})}),),
        ),
    ),
)
def test_route_rejects_resource_types_that_could_be_ambiguous_or_escape_the_route(
    include_subpath: bool, resource_types: tuple[AccountResourceType, ...]
) -> None:
    with pytest.raises(ValueError):
        validate_resource_types(ROUTE, include_subpath, resource_types)


def test_routes_without_resource_types_keep_their_existing_rules() -> None:
    validate_resource_types(ROUTE, False, ())
    assert find_resource((), f"{ROUTE}/jobs/abc") is None


@pytest.mark.parametrize("segment", ("a+b", "a!b", "a$b", "", "{resource_id}", "a%2fb", "x" * 257))
def test_non_canonical_segments_still_match_the_item_path_structurally(segment: str) -> None:
    match: Final = find_resource((JOBS, FILES), f"{ROUTE}/jobs/{segment}/cancel")
    assert match == ResourcePathMatch("video-job", JOBS.item_paths[1], segment)
    assert match.resource_id is None


@pytest.mark.parametrize(
    "path", (f"{ROUTE}/jobs", f"{ROUTE}/jobs/abc/cancelled", f"{ROUTE}/jobs/abc/cancel/now", f"{ROUTE}/reports")
)
def test_paths_outside_item_shapes_are_not_resource_paths(path: str) -> None:
    assert find_resource((JOBS, FILES), path) is None
