from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from cube_control.credential_inventory import (
    SERVICE_PROVIDER_HINTS,
    CredentialCandidate,
    CredentialInventorySnapshot,
    CredentialKind,
)
from cube_control.models import FrozenModel, Identifier

CandidateReference = Annotated[str, Field(pattern=r"^cand-[0-9a-f]{16}$")]


class CredentialBindingCommand(FrozenModel):
    schema_version: Literal[1] = 1
    command_id: Identifier
    reference: CandidateReference
    expected_version: Annotated[int, Field(ge=0)]
    action: Literal["bind", "unbind"]
    account_id: Identifier | None = None

    @model_validator(mode="after")
    def account_only_for_binding(self) -> Self:
        if (self.action == "bind") != (self.account_id is not None):
            raise ValueError("Binding requires an account; unbinding takes none")
        return self


class BindingOrigin(FrozenModel):
    source_id: Identifier
    service: str
    provider_hint: str | None = None
    state: Literal["current", "archived_only"]
    kinds: tuple[CredentialKind, ...]
    hosts: tuple[str, ...]
    projects: tuple[str, ...]
    variables: tuple[str, ...]
    last_observed_at: AwareDatetime
    scanned_at: AwareDatetime


class CredentialBindingEntry(FrozenModel):
    command: CredentialBindingCommand
    revision: Annotated[int, Field(ge=1)]
    actor: Annotated[str, Field(min_length=1, max_length=100)]
    received_at: AwareDatetime
    origin: BindingOrigin | None = None


def binding_origin(snapshot: CredentialInventorySnapshot, candidate: CredentialCandidate) -> BindingOrigin:
    return BindingOrigin(
        source_id=snapshot.source_id,
        service=candidate.service,
        provider_hint=SERVICE_PROVIDER_HINTS.get(candidate.service),
        state="current"
        if any(location.source_class == "current" for location in candidate.locations)
        else "archived_only",
        kinds=candidate.kinds,
        hosts=tuple(sorted(frozenset(location.host for location in candidate.locations))),
        projects=tuple(sorted(frozenset(location.project for location in candidate.locations))),
        variables=tuple(sorted(frozenset(location.variable for location in candidate.locations))),
        last_observed_at=max(location.observed_at for location in candidate.locations),
        scanned_at=snapshot.scanned_at,
    )


def bound_account(entries: tuple[CredentialBindingEntry, ...]) -> str | None:
    latest: Final = max(entries, key=lambda entry: entry.revision, default=None)
    return latest.command.account_id if latest is not None else None


def binding_revision(entries: tuple[CredentialBindingEntry, ...]) -> int:
    return max((entry.revision for entry in entries), default=0)
