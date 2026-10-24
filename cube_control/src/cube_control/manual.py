from decimal import Decimal
from itertools import chain, groupby
from typing import Final

from cube_control.models import Account, ManualChange, ManualEntry, ManualFieldView, Observation


def entry_changes(entry: ManualEntry) -> tuple[tuple[ManualEntry, ManualChange], ...]:
    account: Final = entry.command.create_account
    changes: Final = (
        (
            ManualChange(field="name", action="set", value=account.name),
            ManualChange(field="state", action="set", value=account.state),
            ManualChange(field="plan", action="set", value=account.plan),
            ManualChange(field="note", action="set", value=account.note),
        )
        if account is not None
        else entry.command.changes
    )
    return tuple((entry, change) for change in changes)


def active_fields(account_id: str, entries: tuple[ManualEntry, ...]) -> tuple[ManualFieldView, ...]:
    matching: Final = tuple(entry for entry in entries if entry.command.account_id == account_id)
    pairs: Final = tuple(chain.from_iterable(entry_changes(entry) for entry in matching))
    ordered: Final = sorted(pairs, key=lambda pair: (pair[1].field, pair[1].period))
    latest: Final = tuple(
        max(group, key=lambda pair: pair[0].revision)
        for _, group in groupby(ordered, key=lambda pair: (pair[1].field, pair[1].period))
    )
    return tuple(
        ManualFieldView(
            field=change.field,
            period=change.period,
            value=change.value,
            revision=entry.revision,
            actor=entry.actor,
            changed_at=entry.received_at,
        )
        for entry, change in latest
        if change.action == "set" and change.value is not None
    )


def effective_account(account: Account, fields: tuple[ManualFieldView, ...]) -> Account:
    overrides: Final = {
        field.field: field.value for field in fields if field.field in ("name", "state", "plan", "note")
    }
    return Account.model_validate(account.model_dump() | overrides)


def manual_reading(account_id: str, field: ManualFieldView) -> Observation:
    metric: Final = (
        "budget" if field.field == "budget_usd" else "recurring_cost" if field.field == "recurring_usd" else "balance"
    )
    return Observation(
        account_id=account_id,
        external_id=f"managed-{field.revision}-{field.field}-{field.period or 'all'}",
        metric=metric,
        window="Monat" if metric in ("budget", "recurring_cost") else "Manuell erfasst",
        status="manual",
        source="manual",
        observed_at=field.changed_at,
        collected_at=field.changed_at,
        value=Decimal(field.value),
        unit="credits" if field.field == "balance_credits" else "USD",
        period=field.period,
        fresh_for_seconds=2678400 if metric in ("budget", "recurring_cost") else 900,
        message="Manuell gepflegt; Quelle bleibt erhalten bis zur Rückkehr zur Automatik",
    )


def effective_readings(
    account_id: str, observations: tuple[Observation, ...], fields: tuple[ManualFieldView, ...]
) -> tuple[Observation, ...]:
    monetary: Final = tuple(field for field in fields if field.field not in ("name", "state", "plan", "note"))
    manual: Final = tuple(manual_reading(account_id, field) for field in monetary)
    keys: Final = frozenset((reading.metric, reading.unit, reading.period) for reading in manual)
    automatic: Final = tuple(
        reading for reading in observations if (reading.metric, reading.unit, reading.period) not in keys
    )
    return automatic + manual
