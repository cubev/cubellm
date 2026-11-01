import re
from collections.abc import Iterator, Sequence
from itertools import chain, combinations, product
from typing import Annotated, Final, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt, StrictStr, model_validator
from typing_extensions import Self

RESOURCE_PLACEHOLDER: Final = "{resource_id}"
_RESOURCE_ID: Final = re.compile(r"[A-Za-z0-9._~:@-]{1,256}")
_LITERAL_SEGMENT: Final = re.compile(r"[A-Za-z0-9._~!$&'()+,;=:@-]+")
_METHOD: Final = r"^[!#$%&'+.^_`|~0-9A-Z-]{1,32}$"

ResourceTypeName: TypeAlias = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")]
ResourceMethods: TypeAlias = Annotated[
    tuple[Annotated[str, Field(pattern=_METHOD)], ...], Field(min_length=1, max_length=16)
]
ResourceSelector: TypeAlias = Annotated[
    tuple[Annotated[StrictStr, Field(min_length=1, max_length=128)] | Annotated[StrictInt, Field(ge=0, le=1024)], ...],
    Field(min_length=1, max_length=8),
]


def canonical_resource_id(value: str) -> bool:
    return _RESOURCE_ID.fullmatch(value) is not None and value not in (".", "..")


def _literal_path(path: str) -> bool:
    segments: Final = path.split("/")
    return (
        path.startswith("/")
        and len(path) <= 1024
        and all(_LITERAL_SEGMENT.fullmatch(segment) is not None for segment in segments[1:])
        and all(segment not in (".", "..") for segment in segments[1:])
    )


def is_resource_template(path: str) -> bool:
    segments: Final = path.split("/")
    literals: Final = tuple(segment for segment in segments[1:] if segment != RESOURCE_PLACEHOLDER)
    return (
        path.startswith("/")
        and len(path) <= 1024
        and segments.count(RESOURCE_PLACEHOLDER) == 1
        and all(_LITERAL_SEGMENT.fullmatch(segment) is not None for segment in literals)
        and all(segment not in (".", "..") for segment in literals)
    )


def resource_id_in_path(template: str, path: str) -> str | None:
    expected: Final = template.split("/")
    actual: Final = path.split("/")
    if len(expected) != len(actual):
        return None
    if any(want != got for want, got in zip(expected, actual) if want != RESOURCE_PLACEHOLDER):
        return None
    candidate: Final = actual[expected.index(RESOURCE_PLACEHOLDER)]
    return candidate if canonical_resource_id(candidate) else None


def _templates_overlap(first: str, second: str) -> bool:
    left: Final = first.split("/")
    right: Final = second.split("/")
    return len(left) == len(right) and all(a == b or RESOURCE_PLACEHOLDER in (a, b) for a, b in zip(left, right))


def select_resource_id(document: JsonValue, selector: Sequence[str | int]) -> str | None:
    if not selector:
        return document if isinstance(document, str) and canonical_resource_id(document) else None
    step: Final = selector[0]
    if isinstance(step, str) and isinstance(document, dict):
        return select_resource_id(document.get(step), selector[1:]) if step in document else None
    if isinstance(step, int) and isinstance(document, list) and step < len(document):
        return select_resource_id(document[step], selector[1:])
    return None


class ResourceCreation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: Annotated[str, Field(min_length=1, max_length=1024)]
    methods: ResourceMethods
    id_selector: ResourceSelector

    @model_validator(mode="after")
    def validate_creation(self) -> Self:
        if not _literal_path(self.path):
            raise ValueError("Resource creation requires a canonical concrete path")
        if len(frozenset(self.methods)) != len(self.methods):
            raise ValueError("Resource creation methods must be unique")
        return self


class AccountResourceType(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: ResourceTypeName
    item_paths: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=1024)], ...], Field(min_length=1, max_length=16)
    ]
    creation: ResourceCreation | None = None

    @model_validator(mode="after")
    def validate_resource_type(self) -> Self:
        if not all(is_resource_template(path) for path in self.item_paths):
            raise ValueError("Resource item paths need exactly one {resource_id} segment between literal segments")
        if len(frozenset(self.item_paths)) != len(self.item_paths):
            raise ValueError("Resource item paths must be unique")
        return self


def validate_resource_types(
    route_path: str, include_subpath: bool, resource_types: Sequence[AccountResourceType]
) -> None:
    if not resource_types:
        return
    if not include_subpath:
        raise ValueError("Account resources require a route that forwards subpaths")
    if len(frozenset(resource.name for resource in resource_types)) != len(resource_types):
        raise ValueError("Account resource type names must be unique")
    children: Final = route_path.rstrip("/") + "/"
    templates: Final = tuple(chain.from_iterable(resource.item_paths for resource in resource_types))
    if not all(path.startswith(children) for path in templates):
        raise ValueError("Account resource item paths must be below the route path")
    if any(_templates_overlap(first, second) for first, second in combinations(templates, 2)):
        raise ValueError("Account resource item paths must not match the same request path")
    creations: Final = tuple(resource.creation for resource in resource_types if resource.creation is not None)
    if not all(creation.path == route_path or creation.path.startswith(children) for creation in creations):
        raise ValueError("Account resource creation paths must belong to the route")
    if any(_templates_overlap(creation.path, template) for creation, template in product(creations, templates)):
        raise ValueError("Account resource creation paths must not match an item path")


def _candidates(resource_types: Sequence[AccountResourceType], path: str) -> Iterator[tuple[str, str, str]]:
    for resource in resource_types:
        for template in resource.item_paths:
            resource_id = resource_id_in_path(template, path)
            if resource_id is not None:
                yield resource.name, template, resource_id


def find_resource(resource_types: Sequence[AccountResourceType], path: str) -> tuple[str, str, str] | None:
    matches: Final = tuple(_candidates(resource_types, path))
    return matches[0] if len(matches) == 1 else None
