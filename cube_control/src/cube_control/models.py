from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Provider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    XAI = "xai"
    HEYGEN = "heygen"
    SUNO = "suno"
    ELEVENLABS = "elevenlabs"


class Account(FrozenModel):
    id: Identifier
    provider: Provider
    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: Literal["api", "subscription"]
    state: Literal["active", "paused", "cancelled", "unknown", "not_configured"] = "unknown"
    plan: Annotated[str, Field(max_length=100)] = ""
    note: Annotated[str, Field(max_length=300)] = ""


class Observation(FrozenModel):
    account_id: Identifier
    external_id: Identifier
    metric: Literal["spend", "budget", "balance", "limit", "recurring_cost", "usage"]
    window: Annotated[str, Field(min_length=1, max_length=80)]
    status: Literal["known", "manual", "estimated", "unknown", "error", "not_configured"]
    source: Literal["provider_api", "local_collector", "manual", "import"]
    observed_at: AwareDatetime
    collected_at: AwareDatetime
    value: Decimal | None = None
    unit: Literal["USD", "EUR", "credits", "tokens", "minutes", "ratio", "characters"] | None = None
    period: Annotated[str, Field(max_length=80)] = ""
    resets_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    fresh_for_seconds: Annotated[int, Field(ge=30, le=2678400)] = 900
    message: Annotated[str, Field(max_length=300)] = ""

    @model_validator(mode="after")
    def validate_measurement(self) -> Self:
        if self.status in ("unknown", "error", "not_configured") and self.value is not None:
            raise ValueError("Unavailable observations cannot carry a current value")
        if self.status in ("known", "manual", "estimated") and (self.value is None or self.unit is None):
            raise ValueError("Measured observations require a value and unit")
        if self.value is not None and not self.value.is_finite():
            raise ValueError("Measurements must be finite")
        if (
            self.metric in ("spend", "budget", "recurring_cost")
            and self.value is not None
            and self.unit not in ("USD", "EUR")
        ):
            raise ValueError("Monetary costs require a currency")
        if self.metric in ("spend", "budget") and not self.period:
            raise ValueError("Spending requires an explicit reporting period")
        if (
            self.metric == "limit"
            and self.value is not None
            and (self.unit != "ratio" or not Decimal(0) <= self.value <= Decimal(1))
        ):
            raise ValueError("Limit usage must be a ratio between zero and one")
        return self

    @property
    def id(self) -> str:
        return sha256(f"{self.account_id}\0{self.source}\0{self.external_id}".encode()).hexdigest()

    def freshness(self, now: datetime) -> Literal["fresh", "stale", "clock_skew", "expired"]:
        age: Final[float] = (now - self.observed_at).total_seconds()
        if age < -60:
            return "clock_skew"
        if self.expires_at is not None and self.expires_at <= now:
            return "expired"
        if self.metric == "limit" and self.resets_at is not None and self.resets_at <= now:
            return "expired"
        return "stale" if age > self.fresh_for_seconds else "fresh"


class UsageEvent(FrozenModel):
    account_id: Identifier
    event_id: Identifier
    occurred_at: AwareDatetime
    collected_at: AwareDatetime
    model: Annotated[str, Field(min_length=1, max_length=150)]
    period: Annotated[str, Field(min_length=1, max_length=80)]
    amount: Decimal | None = None
    currency: Literal["USD", "EUR"] = "USD"
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    cache_read_tokens: Annotated[int, Field(ge=0)] = 0
    cache_write_tokens: Annotated[int, Field(ge=0)] = 0
    status: Literal["succeeded", "failed", "cancelled"] = "succeeded"

    @model_validator(mode="after")
    def validate_cost(self) -> Self:
        if self.amount is not None and (not self.amount.is_finite() or self.amount < 0):
            raise ValueError("Observed event costs must be finite and non-negative")
        return self

    @property
    def id(self) -> str:
        return sha256(f"{self.account_id}\0{self.event_id}".encode()).hexdigest()


class MediaJob(FrozenModel):
    account_id: Identifier
    job_id: Identifier
    state: Literal["reserved", "submitted", "finalized", "failed", "expired"]
    updated_at: AwareDatetime
    reserved_amount: Annotated[Decimal, Field(ge=0)]
    unit: Literal["USD", "EUR", "credits", "minutes"]
    expires_at: AwareDatetime
    actual_amount: Annotated[Decimal, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def validate_amounts(self) -> Self:
        if not self.reserved_amount.is_finite():
            raise ValueError("Reservation must be finite")
        if self.actual_amount is not None and not self.actual_amount.is_finite():
            raise ValueError("Actual usage must be finite")
        if self.state == "finalized" and self.actual_amount is None:
            raise ValueError("Finalized jobs require actual usage")
        return self

    @property
    def id(self) -> str:
        return sha256(f"{self.account_id}\0{self.job_id}".encode()).hexdigest()

    def can_replace(self, previous: "MediaJob") -> bool:
        allowed: Final[dict[str, tuple[str, ...]]] = {
            "reserved": ("submitted", "failed", "expired"),
            "submitted": ("finalized", "failed", "expired"),
            "finalized": (),
            "failed": (),
            "expired": (),
        }
        return (
            self.account_id == previous.account_id
            and self.job_id == previous.job_id
            and self.unit == previous.unit
            and self.reserved_amount == previous.reserved_amount
            and self.updated_at >= previous.updated_at
            and (self == previous or self.state in allowed[previous.state])
        )


class RepositoryState(FrozenModel):
    name: str
    path: str
    branch: str | None = None
    commit: str | None = None
    tracked_changes: int | None = None


class FleetHost(FrozenModel):
    id: Identifier
    kind: Literal["mac", "proxmox", "router", "application"]
    status: Literal["ok", "unavailable", "snapshot_only", "error"]
    checked_at: AwareDatetime
    hostname: str = ""
    address: str = ""
    repositories: tuple[RepositoryState, ...] = ()
    services: tuple[str, ...] = ()
    listeners: tuple[str, ...] = ()
    message: str = ""


class FleetSnapshot(FrozenModel):
    checked_at: AwareDatetime
    hosts: tuple[FleetHost, ...]


class ImportBatch(FrozenModel):
    accounts: tuple[Account, ...] = ()
    observations: tuple[Observation, ...] = ()
    events: tuple[UsageEvent, ...] = ()
    jobs: tuple[MediaJob, ...] = ()
    fleet: FleetSnapshot | None = None


ManualField = Literal["name", "state", "plan", "note", "budget_usd", "recurring_usd", "balance_usd", "balance_credits"]


class ManualChange(FrozenModel):
    field: ManualField
    action: Literal["set", "automatic"]
    value: Annotated[str, Field(max_length=300)] | None = None
    period: Annotated[str, Field(pattern=r"^(?:|[0-9]{4}-(?:0[1-9]|1[0-2]))$")] = ""

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        if (self.action == "set") != (self.value is not None):
            raise ValueError("Set requires a value; automatic cannot carry a value")
        if (self.field == "budget_usd") != bool(self.period):
            raise ValueError("Only monthly budgets require a period")
        if self.value is None:
            return self
        if self.field in ("name", "state", "plan", "note"):
            Account.model_validate(
                {"id": "validation", "provider": "openai", "kind": "api", "name": "validation", self.field: self.value}
            )
            return self
        try:
            amount: Final[Decimal] = Decimal(self.value)
        except ArithmeticError:
            raise ValueError("Money and credit values must be decimals") from None
        if (
            not amount.is_finite()
            or abs(amount) > Decimal("1e15")
            or (self.field in ("budget_usd", "recurring_usd") and amount < 0)
        ):
            raise ValueError("Invalid manual amount")
        if self.field.endswith("usd") and amount != amount.quantize(Decimal("0.01")):
            raise ValueError("Monetary values require at most two decimal places")
        return self


class ManualCommand(FrozenModel):
    schema_version: Literal[1] = 1
    command_id: Identifier
    account_id: Identifier
    expected_version: Annotated[int, Field(ge=0)]
    changes: Annotated[tuple[ManualChange, ...], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def unique_fields(self) -> Self:
        if len({(change.field, change.period) for change in self.changes}) != len(self.changes):
            raise ValueError("Each field and period can only be changed once per command")
        return self


class ManualEntry(FrozenModel):
    command: ManualCommand
    revision: Annotated[int, Field(ge=1)]
    actor: Annotated[str, Field(min_length=1, max_length=100)]
    received_at: AwareDatetime


class ManualFieldView(FrozenModel):
    field: ManualField
    period: str
    value: str
    revision: int
    actor: str
    changed_at: AwareDatetime


class GatewayRecord(FrozenModel):
    source_key: Identifier
    provider: Provider | None = None
    model: Annotated[str, Field(min_length=1, max_length=150)]
    occurred_at: AwareDatetime
    source_updated_at: AwareDatetime | None = None
    status: Literal["succeeded", "failed", "unknown"]
    amount_usd: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = None
    input_tokens: Annotated[int, Field(ge=0, strict=True)] | None = None
    output_tokens: Annotated[int, Field(ge=0, strict=True)] | None = None
    cache_read_tokens: Annotated[int, Field(ge=0, strict=True)] | None = None
    cache_write_tokens: Annotated[int, Field(ge=0, strict=True)] | None = None


class GatewayScan(FrozenModel):
    scan_id: Identifier
    source_id: Identifier
    expected_sequence: Annotated[int, Field(ge=0)]
    checked_at: AwareDatetime
    state: Literal["ok", "partial", "error"]
    scope: Literal["stored_gateway_rows"] = "stored_gateway_rows"
    available_rows: Annotated[int, Field(ge=0)] | None = None
    scanned_rows: Annotated[int, Field(ge=0)] = 0
    unassigned_rows: Annotated[int, Field(ge=0)] = 0
    rejected_rows: Annotated[int, Field(ge=0)] = 0
    message: Annotated[str, Field(max_length=300)] = ""


class GatewayBatch(FrozenModel):
    scan: GatewayScan
    records: Annotated[tuple[GatewayRecord, ...], Field(max_length=1000)] = ()

    @model_validator(mode="after")
    def validate_scan(self) -> Self:
        if self.scan.state == "error" and self.records:
            raise ValueError("Failed scans cannot update records")
        if len({record.source_key for record in self.records}) != len(self.records):
            raise ValueError("Duplicate source row in scan")
        if self.scan.state == "ok" and (self.scan.available_rows is None or self.scan.rejected_rows):
            raise ValueError("Successful scans require a row count and no rejected rows")
        return self


class GatewayScanView(FrozenModel):
    scan: GatewayScan
    sequence: int


class GatewayMetrics(FrozenModel):
    calls: int
    failures: int
    unknown_status: int
    reported_usd: Decimal
    unknown_cost_calls: int
    input_tokens: int
    unknown_input_calls: int
    output_tokens: int
    unknown_output_calls: int
    cache_read_tokens: int
    unknown_cache_read_calls: int
    cache_write_tokens: int
    unknown_cache_write_calls: int


class AccountView(FrozenModel):
    account: Account
    observations: tuple[Observation, ...]
    automatic_account: Account | None = None
    manual_fields: tuple[ManualFieldView, ...] = ()
    manual_version: int = 0
    gateway: GatewayMetrics | None = None
    proxy_events: int
    proxy_amounts: dict[str, Decimal]
    selected_spend: Observation | None
    open_reservations: dict[str, Decimal]


CATALOG: Final[tuple[Account, ...]] = (
    Account(id="codex-subscription", provider=Provider.OPENAI, name="Codex", kind="subscription"),
    Account(id="openai-api", provider=Provider.OPENAI, name="OpenAI API", kind="api"),
    Account(id="claude-subscription", provider=Provider.ANTHROPIC, name="Claude", kind="subscription"),
    Account(id="anthropic-api", provider=Provider.ANTHROPIC, name="Anthropic API", kind="api"),
    Account(id="openrouter-api", provider=Provider.OPENROUTER, name="OpenRouter", kind="api", state="not_configured"),
    Account(
        id="grok-subscription",
        provider=Provider.XAI,
        name="Grok",
        kind="subscription",
        note="Status im Konto noch nicht verifiziert",
    ),
    Account(id="xai-api", provider=Provider.XAI, name="xAI API", kind="api", state="not_configured"),
    Account(id="heygen-subscription", provider=Provider.HEYGEN, name="HeyGen Abo", kind="subscription"),
    Account(id="heygen-api", provider=Provider.HEYGEN, name="HeyGen API", kind="api", state="not_configured"),
    Account(id="suno-subscription", provider=Provider.SUNO, name="Suno Abo", kind="subscription"),
    Account(id="suno-api", provider=Provider.SUNO, name="Suno API", kind="api", state="not_configured"),
)


def utc_now() -> datetime:
    return datetime.now(UTC)
