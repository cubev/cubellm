from collections.abc import Callable
from itertools import chain
from typing import Final, Literal, Self, TypeVar

from pydantic import AwareDatetime, model_validator

from cube_control.gateway_inventory import (
    SOURCE_PAIRS,
    GatewayInventoryAttempt,
    GatewayInventorySnapshot,
    InventoryCredential,
    InventoryDeployment,
    InventoryRejection,
    InventoryRoute,
    InventorySource,
    Origin,
    SourceName,
)
from cube_control.models import FrozenModel

Presence = Literal["latest_collection", "retained"]


class SourceEvidence(FrozenModel):
    name: SourceName
    origin: Origin
    status: Literal["complete", "partial", "unavailable"]
    detail: str = ""
    observed_at: AwareDatetime
    last_readable_at: AwareDatetime | None = None


class KnownCredential(FrozenModel):
    credential: InventoryCredential
    observed_at: AwareDatetime
    presence: Presence


class KnownRoute(FrozenModel):
    route: InventoryRoute
    observed_at: AwareDatetime
    presence: Presence


class KnownDeployment(FrozenModel):
    deployment: InventoryDeployment
    observed_at: AwareDatetime
    presence: Presence


class GatewayEvidence(FrozenModel):
    checked_at: AwareDatetime
    observed_at: AwareDatetime
    sources: tuple[SourceEvidence, ...]
    credentials: tuple[KnownCredential, ...] = ()
    routes: tuple[KnownRoute, ...] = ()
    deployments: tuple[KnownDeployment, ...] = ()
    rejected: tuple[InventoryRejection, ...] = ()

    @model_validator(mode="after")
    def every_source_exactly_once(self) -> Self:
        pairs: Final = tuple((source.name, source.origin) for source in self.sources)
        if len(pairs) != len(SOURCE_PAIRS) or frozenset(pairs) != SOURCE_PAIRS:
            raise ValueError("Gateway evidence must describe every inventory source exactly once")
        return self


class GatewayInventoryState(FrozenModel):
    gateway_origin: str
    attempt: GatewayInventoryAttempt
    evidence: GatewayEvidence | None = None


ORIGINS: Final[tuple[Origin, ...]] = ("runtime", "database")
Known = TypeVar("Known", KnownCredential, KnownRoute, KnownDeployment)


def _credential_identity(item: KnownCredential) -> str | None:
    return item.credential.credential_name


def _route_identity(item: KnownRoute) -> str | None:
    return item.route.route_id or item.route.path


def _deployment_identity(item: KnownDeployment) -> str | None:
    return item.deployment.model_id


def _merged(
    source: InventorySource,
    previous: tuple[Known, ...],
    readable: tuple[Known, ...],
    identity: Callable[[Known], str | None],
) -> tuple[Known, ...]:
    if source.status == "complete":
        return readable
    superseded: Final = frozenset(key for key in map(identity, readable) if key is not None)
    return (
        *readable,
        *(
            item.model_copy(update={"presence": "retained"})
            for item in previous
            if identity(item) is None or identity(item) not in superseded
        ),
    )


def _by_origin(
    sources: dict[tuple[SourceName, Origin], InventorySource],
    name: SourceName,
    previous: tuple[Known, ...],
    readable: tuple[Known, ...],
    origin_of: Callable[[Known], Origin],
    identity: Callable[[Known], str | None],
) -> tuple[Known, ...]:
    return tuple(
        chain.from_iterable(
            _merged(
                sources[(name, origin)],
                tuple(item for item in previous if origin_of(item) == origin),
                tuple(item for item in readable if origin_of(item) == origin),
                identity,
            )
            for origin in ORIGINS
        )
    )


def _source(source: InventorySource, previous: SourceEvidence | None, observed_at: AwareDatetime) -> SourceEvidence:
    return SourceEvidence(
        name=source.name,
        origin=source.origin,
        status=source.status,
        detail=source.detail,
        observed_at=observed_at,
        last_readable_at=observed_at
        if source.status != "unavailable"
        else previous.last_readable_at
        if previous is not None
        else None,
    )


def merge_evidence(previous: GatewayEvidence | None, snapshot: GatewayInventorySnapshot) -> GatewayEvidence:
    inventory: Final = snapshot.inventory
    if previous is not None and inventory.observed_at <= previous.observed_at:
        return previous
    observed: Final = inventory.observed_at
    sources: Final[dict[tuple[SourceName, Origin], InventorySource]] = {
        (source.name, source.origin): source for source in inventory.sources
    }
    prior_sources: Final = {(item.name, item.origin): item for item in previous.sources} if previous else {}
    return GatewayEvidence(
        checked_at=snapshot.checked_at,
        observed_at=observed,
        sources=tuple(_source(sources[pair], prior_sources.get(pair), observed) for pair in sorted(SOURCE_PAIRS)),
        credentials=_by_origin(
            sources,
            "credentials",
            previous.credentials if previous else (),
            tuple(
                KnownCredential(credential=item, observed_at=observed, presence="latest_collection")
                for item in inventory.credentials
            ),
            lambda item: item.credential.origin,
            _credential_identity,
        ),
        routes=_by_origin(
            sources,
            "routes",
            previous.routes if previous else (),
            tuple(
                KnownRoute(route=item, observed_at=observed, presence="latest_collection") for item in inventory.routes
            ),
            lambda item: item.route.origin,
            _route_identity,
        ),
        deployments=_by_origin(
            sources,
            "deployments",
            previous.deployments if previous else (),
            tuple(
                KnownDeployment(deployment=item, observed_at=observed, presence="latest_collection")
                for item in inventory.deployments
            ),
            lambda item: item.deployment.origin,
            _deployment_identity,
        ),
        rejected=inventory.rejected,
    )
