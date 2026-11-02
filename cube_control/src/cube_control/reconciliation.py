import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from pydantic import AwareDatetime

from cube_control.amounts import exact_sum
from cube_control.gateway_store import GatewayPeriod
from cube_control.models import Account, FrozenModel, GatewayRecord, GatewayScanView, Observation, SpendCoverage
from cube_control.store import StoredState
from cube_control.summary import latest_measurements, superseded
from cube_control.usage import MANAGEMENT_CALL_TYPES, PROVIDER_CALL_TYPES

PERIOD_PATTERN: Final = r"[0-9]{4}-(0[1-9]|1[0-2])"
PERIOD_REJECTED: Final = "Period must be a calendar month YYYY-MM whose following month is representable"
SCOPE: Final = (
    "Anbieterausgaben des Kontos (gesamt) gegenüber den über das Gateway erfassten Anbieteranfragen (Teilmenge)"
)
SOURCE_PRIORITY: Final = {"provider_api": 0, "import": 1, "manual": 2, "local_collector": 3}
SCAN_STALE_AFTER: Final = timedelta(minutes=15)
CLOCK_SKEW: Final = timedelta(seconds=60)
Reason = Literal[
    "account_not_in_catalogue",
    "month_not_closed",
    "provider_missing",
    "provider_not_current",
    "provider_conflict",
    "provider_not_provider_reported",
    "provider_coverage_unstructured",
    "provider_coverage_not_final",
    "provider_coverage_other_window",
    "currency_mismatch",
    "gateway_not_loaded",
    "gateway_missing",
    "gateway_scan_missing",
    "gateway_scan_incomplete",
    "gateway_scan_rejected_rows",
    "gateway_scan_clock_skew",
    "gateway_scan_stale",
    "gateway_scan_before_period_end",
    "gateway_coverage_unknown",
    "gateway_coverage_after_period_start",
    "gateway_costs_unknown",
    "gateway_traffic_unknown",
    "gateway_provider_mismatch",
]
Comparison = Literal["consistent_with_provider_total", "gateway_exceeds_provider_total", "not_comparable"]
Direction = Literal["equal", "provider_higher", "gateway_higher"]
ScanStatus = Literal[
    "complete", "partial", "error", "rejected_rows", "clock_skew", "stale", "before_period_end", "coverage_unknown"
]


class ProviderEvidence(FrozenModel):
    value: Decimal | None = None
    unit: str | None = None
    source: str
    status: str
    external_id: str
    observed_at: AwareDatetime
    freshness: str
    current: bool
    coverage: SpendCoverage | None = None


class GatewaySide(FrozenModel):
    provider_request_usd: Decimal
    provider_request_calls: int
    unknown_cost_calls: int
    unknown_traffic_calls: int
    unknown_traffic_usd: Decimal
    management_calls: int
    management_usd: Decimal
    failures: int
    provider_mismatch_calls: int
    first_call_at: AwareDatetime | None = None
    last_call_at: AwareDatetime | None = None


class ScanEvidence(FrozenModel):
    source_id: str
    state: str
    status: ScanStatus
    checked_at: AwareDatetime
    coverage_start: AwareDatetime | None = None
    rejected_rows: int


class ReconciliationRow(FrozenModel):
    account_id: str
    provider: str | None = None
    period: str
    provider_reading: ProviderEvidence | None = None
    provider_evidence: tuple[ProviderEvidence, ...] = ()
    gateway: GatewaySide
    delta: Decimal | None = None
    direction: Direction | None = None
    comparison: Comparison
    reasons: tuple[Reason, ...]
    scope: str = SCOPE


class UnattributedGateway(FrozenModel):
    provider: str
    gateway: GatewaySide


class ReconciliationReport(FrozenModel):
    period: str
    generated_at: AwareDatetime
    gateway_loaded: bool
    scans: tuple[ScanEvidence, ...]
    rows: tuple[ReconciliationRow, ...]
    unattributed: tuple[UnattributedGateway, ...]


def period_bounds(period: str) -> tuple[datetime, datetime]:
    start: Final = datetime.strptime(period + "-01", "%Y-%m-%d").replace(tzinfo=UTC)
    return start, (start + timedelta(days=32)).replace(day=1)


def parse_period(period: str) -> tuple[datetime, datetime] | None:
    if re.fullmatch(PERIOD_PATTERN, period) is None:
        return None
    try:
        return period_bounds(period)
    except (ValueError, OverflowError):
        return None


def _traffic(record: GatewayRecord) -> Literal["provider_request", "gateway_management", "unknown"]:
    if record.call_type in PROVIDER_CALL_TYPES:
        return "provider_request"
    return "gateway_management" if record.call_type in MANAGEMENT_CALL_TYPES else "unknown"


def _mismatched(record: GatewayRecord, provider: str | None) -> bool:
    return provider is not None and (record.provider is None or str(record.provider) != provider)


def _gateway(records: tuple[GatewayRecord, ...], provider: str | None) -> GatewaySide:
    requests: Final = tuple(record for record in records if _traffic(record) == "provider_request")
    unknown: Final = tuple(record for record in records if _traffic(record) == "unknown")
    management: Final = tuple(record for record in records if _traffic(record) == "gateway_management")
    times: Final = tuple(sorted(record.occurred_at for record in records))
    return GatewaySide(
        provider_request_usd=exact_sum(record.amount_usd for record in requests if record.amount_usd is not None),
        provider_request_calls=len(requests),
        unknown_cost_calls=sum(1 for record in requests if record.amount_usd is None),
        unknown_traffic_calls=len(unknown),
        unknown_traffic_usd=exact_sum(record.amount_usd for record in unknown if record.amount_usd is not None),
        management_calls=len(management),
        management_usd=exact_sum(record.amount_usd for record in management if record.amount_usd is not None),
        failures=sum(1 for record in records if record.status == "failed"),
        provider_mismatch_calls=sum(1 for record in records if _mismatched(record, provider)),
        first_call_at=times[0] if times else None,
        last_call_at=times[-1] if times else None,
    )


def _scan(view: GatewayScanView, period: str, now: datetime) -> ScanEvidence:
    start, end = period_bounds(period)
    scan: Final = view.scan
    status: Final[ScanStatus] = (
        "clock_skew"
        if scan.checked_at > now + CLOCK_SKEW
        else "error"
        if scan.state == "error"
        else "partial"
        if scan.state == "partial"
        else "rejected_rows"
        if scan.rejected_rows
        else "before_period_end"
        if scan.checked_at < end and now >= end
        else "stale"
        if now < end and now - scan.checked_at > SCAN_STALE_AFTER
        else "coverage_unknown"
        if scan.coverage_start is None or scan.coverage_start > start
        else "complete"
    )
    return ScanEvidence(
        source_id=scan.source_id,
        state=scan.state,
        status=status,
        checked_at=scan.checked_at,
        coverage_start=scan.coverage_start,
        rejected_rows=scan.rejected_rows,
    )


def _usable(reading: Observation, observations: tuple[Observation, ...], now: datetime) -> bool:
    freshness: Final = reading.freshness(now)
    final: Final = reading.coverage is not None and reading.coverage.basis == "final_billing_statement"
    return (
        reading.value is not None
        and reading.status in ("known", "manual", "estimated")
        and (freshness == "fresh" or (final and freshness == "stale"))
        and not superseded(reading, observations)
    )


def _evidence(reading: Observation, observations: tuple[Observation, ...], now: datetime) -> ProviderEvidence:
    return ProviderEvidence(
        value=reading.value,
        unit=reading.unit,
        source=reading.source,
        status=reading.status,
        external_id=reading.external_id,
        observed_at=reading.observed_at,
        freshness=reading.freshness(now),
        current=_usable(reading, observations, now),
        coverage=reading.coverage,
    )


def _scan_reasons(scans: tuple[ScanEvidence, ...], period: str) -> tuple[Reason, ...]:
    start, _ = period_bounds(period)
    statuses: Final = frozenset(scan.status for scan in scans)
    checks: Final[tuple[tuple[Reason, bool], ...]] = (
        ("gateway_scan_missing", not scans),
        ("gateway_scan_incomplete", bool(statuses & {"partial", "error"})),
        ("gateway_scan_rejected_rows", any(scan.rejected_rows for scan in scans)),
        ("gateway_scan_clock_skew", "clock_skew" in statuses),
        ("gateway_scan_stale", "stale" in statuses),
        ("gateway_scan_before_period_end", "before_period_end" in statuses),
        ("gateway_coverage_unknown", any(scan.coverage_start is None for scan in scans)),
        (
            "gateway_coverage_after_period_start",
            any(scan.coverage_start is not None and scan.coverage_start > start for scan in scans),
        ),
    )
    return tuple(reason for reason, applies in checks if applies)


def _row(
    account_id: str,
    account: Account | None,
    observations: tuple[Observation, ...],
    records: tuple[GatewayRecord, ...],
    scans: tuple[ScanEvidence, ...],
    period: str,
    now: datetime,
    gateway_loaded: bool,
) -> ReconciliationRow:
    start, end = period_bounds(period)
    provider: Final = str(account.provider) if account is not None else None
    spend: Final = tuple(
        reading
        for reading in latest_measurements(account_id, observations)
        if reading.metric == "spend" and reading.period == period
    )
    evidence: Final = tuple(_evidence(reading, observations, now) for reading in spend)
    current: Final = sorted(
        (reading for reading in spend if _usable(reading, observations, now)),
        key=lambda reading: (SOURCE_PRIORITY[reading.source], -reading.observed_at.timestamp()),
    )
    selected: Final = current[0] if current else None
    coverage: Final = selected.coverage if selected is not None else None
    gateway: Final = _gateway(records, provider)
    delta: Final = (
        exact_sum((selected.value, gateway.provider_request_usd.copy_negate()))
        if selected is not None and selected.value is not None and selected.unit == "USD"
        else None
    )
    checks: Final[tuple[tuple[Reason, bool], ...]] = (
        ("account_not_in_catalogue", account is None),
        ("month_not_closed", now < end),
        ("provider_missing", not any(reading.value is not None for reading in spend)),
        ("provider_not_current", any(reading.value is not None for reading in spend) and selected is None),
        ("provider_conflict", len(frozenset((reading.value, reading.unit) for reading in current)) > 1),
        (
            "provider_not_provider_reported",
            selected is not None and (selected.source not in ("provider_api", "import") or selected.status != "known"),
        ),
        ("provider_coverage_unstructured", selected is not None and coverage is None),
        ("provider_coverage_not_final", coverage is not None and coverage.basis != "final_billing_statement"),
        ("provider_coverage_other_window", coverage is not None and (coverage.start != start or coverage.end != end)),
        ("currency_mismatch", selected is not None and selected.unit != "USD"),
        ("gateway_not_loaded", not gateway_loaded),
        ("gateway_missing", gateway_loaded and not records),
        ("gateway_costs_unknown", gateway.unknown_cost_calls > 0),
        ("gateway_traffic_unknown", gateway.unknown_traffic_calls > 0),
        ("gateway_provider_mismatch", gateway.provider_mismatch_calls > 0),
    )
    reasons: Final[tuple[Reason, ...]] = (
        *(reason for reason, applies in checks if applies),
        *_scan_reasons(scans, period),
    )
    direction: Final[Direction | None] = (
        None if delta is None else "equal" if delta == 0 else "provider_higher" if delta > 0 else "gateway_higher"
    )
    comparison: Final[Comparison] = (
        "not_comparable"
        if reasons or direction is None
        else "gateway_exceeds_provider_total"
        if direction == "gateway_higher"
        else "consistent_with_provider_total"
    )
    return ReconciliationRow(
        account_id=account_id,
        provider=provider,
        period=period,
        provider_reading=_evidence(selected, observations, now) if selected is not None else None,
        provider_evidence=evidence,
        gateway=gateway,
        delta=delta,
        direction=direction,
        comparison=comparison,
        reasons=reasons,
    )


def _provider_label(record: GatewayRecord) -> str:
    return str(record.provider) if record.provider is not None else "unknown"


def reconcile(
    accounts: tuple[Account, ...],
    observations: tuple[Observation, ...],
    records: tuple[GatewayRecord, ...],
    scans: tuple[GatewayScanView, ...],
    period: str,
    now: datetime,
    gateway_loaded: bool = True,
) -> ReconciliationReport:
    start, end = period_bounds(period)
    in_period: Final = tuple(record for record in records if start <= record.occurred_at < end)
    scan_evidence: Final = tuple(
        _scan(view, period, now) for view in sorted(scans, key=lambda item: item.scan.source_id)
    )
    catalogued: Final = {account.id: account for account in accounts}
    attributed: Final = frozenset(record.account_id for record in in_period if record.account_id is not None)
    account_ids: Final = sorted(frozenset(account.id for account in accounts if account.kind == "api") | attributed)
    return ReconciliationReport(
        period=period,
        generated_at=now,
        gateway_loaded=gateway_loaded,
        scans=scan_evidence,
        rows=tuple(
            _row(
                account_id,
                catalogued.get(account_id),
                observations,
                tuple(record for record in in_period if record.account_id == account_id),
                scan_evidence,
                period,
                now,
                gateway_loaded,
            )
            for account_id in account_ids
        ),
        unattributed=tuple(
            UnattributedGateway(
                provider=provider,
                gateway=_gateway(
                    tuple(
                        record
                        for record in in_period
                        if record.account_id is None and _provider_label(record) == provider
                    ),
                    None,
                ),
            )
            for provider in sorted(
                frozenset(_provider_label(record) for record in in_period if record.account_id is None)
            )
        ),
    )


def reconcile_stored(
    state: StoredState, gateway: GatewayPeriod | None, period: str, now: datetime
) -> ReconciliationReport:
    start, end = period_bounds(period)
    current: Final = start <= now < end
    records: Final = gateway.records if gateway is not None else state.gateway_records if current else ()
    return reconcile(
        state.accounts,
        state.observations,
        records,
        gateway.scans if gateway is not None else state.gateway_scans,
        period,
        now,
        gateway is not None or current,
    )
