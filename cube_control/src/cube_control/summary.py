from datetime import datetime
from decimal import Decimal
from itertools import groupby
from typing import Final

from cube_control.gateway import gateway_metrics
from cube_control.manual import active_fields, effective_account, effective_readings
from cube_control.models import Account, AccountView, MediaJob, Observation, UsageEvent
from cube_control.store import StoredState


def measurement_key(observation: Observation) -> tuple[str, str, str, str, str]:
    return (
        observation.metric,
        observation.window,
        observation.period,
        observation.source,
        observation.snapshot_scope or "",
    )


def snapshot_key(observation: Observation) -> tuple[str, str]:
    return observation.source, observation.snapshot_scope or ""


def latest_measurements(account_id: str, observations: tuple[Observation, ...]) -> tuple[Observation, ...]:
    account_readings: Final = tuple(o for o in observations if o.account_id == account_id)
    scoped: Final = tuple(o for o in account_readings if o.snapshot_scope is not None)
    generations: Final = {
        key: max(o.collected_at for o in group)
        for key, group in groupby(sorted(scoped, key=snapshot_key), key=snapshot_key)
    }
    matching: Final = tuple(
        o for o in account_readings if o.snapshot_scope is None or o.collected_at == generations[snapshot_key(o)]
    )
    return tuple(
        max(group, key=lambda o: (o.observed_at, o.collected_at))
        for _, group in groupby(sorted(matching, key=measurement_key), key=measurement_key)
    )


def current_measurement(
    reading: Observation, observations: tuple[Observation, ...], now: datetime, period: str
) -> bool:
    if reading.status not in ("known", "manual", "estimated") or reading.freshness(now) != "fresh":
        return False
    if reading.metric in ("spend", "budget") and reading.period != period:
        return False
    return not any(
        o.account_id == reading.account_id
        and o.source == reading.source
        and o.snapshot_scope == reading.snapshot_scope
        and (
            (reading.snapshot_scope is not None and o.collected_at > reading.collected_at)
            or (
                o.metric == reading.metric
                and o.window == "all"
                and o.status in ("error", "unknown", "not_configured")
                and o.observed_at >= reading.observed_at
            )
        )
        for o in observations
    )


def selected_spend(observations: tuple[Observation, ...], now: datetime, period: str) -> Observation | None:
    candidates: Final[tuple[Observation, ...]] = tuple(
        o for o in observations if o.metric == "spend" and o.period == period
    )
    priority: Final[dict[str, int]] = {"provider_api": 0, "import": 1, "manual": 2, "local_collector": 3}
    ordered: Final[list[Observation]] = sorted(
        candidates, key=lambda o: (priority[o.source], -o.observed_at.timestamp())
    )
    return next((o for o in ordered if current_measurement(o, observations, now, period)), None)


def proxy_amounts(events: tuple[UsageEvent, ...]) -> dict[str, Decimal]:
    return {
        currency: sum((e.amount for e in events if e.currency == currency and e.amount is not None), Decimal(0))
        for currency in sorted({e.currency for e in events if e.amount is not None})
    }


def reservations(jobs: tuple[MediaJob, ...], account_id: str, now: datetime) -> dict[str, Decimal]:
    pending: Final[tuple[MediaJob, ...]] = tuple(
        j for j in jobs if j.account_id == account_id and j.holds_reservation(now)
    )
    return {
        unit: sum((j.reserved_amount for j in pending if j.unit == unit), Decimal(0))
        for unit in sorted({j.unit for j in pending})
    }


def account_view(account: Account, state: StoredState, now: datetime, period: str) -> AccountView:
    fields: Final = active_fields(account.id, state.manual_entries)
    latest: Final[tuple[Observation, ...]] = effective_readings(
        account.id, latest_measurements(account.id, state.observations), fields
    )
    events: Final[tuple[UsageEvent, ...]] = tuple(
        e for e in state.events if e.account_id == account.id and e.period == period
    )
    gateway_records: Final = tuple(
        record
        for record in state.gateway_records
        if record.account_id == account.id and record.provider == account.provider and account.kind == "api"
    )
    return AccountView(
        account=effective_account(account, fields),
        gateway=gateway_metrics(gateway_records, account.provider, period) if gateway_records else None,
        automatic_account=account,
        manual_fields=fields,
        manually_created=any(
            entry.command.account_id == account.id and entry.command.create_account is not None
            for entry in state.manual_entries
        ),
        manual_version=max(
            (entry.revision for entry in state.manual_entries if entry.command.account_id == account.id), default=0
        ),
        observations=latest,
        proxy_events=len(events),
        proxy_amounts=proxy_amounts(events),
        selected_spend=selected_spend(latest, now, period),
        open_reservations=reservations(state.jobs, account.id, now),
    )
