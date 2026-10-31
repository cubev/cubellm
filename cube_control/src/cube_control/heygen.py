from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from typing import Annotated, Final, Literal, Self

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from cube_control.models import Account, ImportBatch, Observation, Provider
from cube_control.provider_access import Unavailable, read_private_key

Amount = Annotated[Decimal, Field(allow_inf_nan=False)]
NonnegativeAmount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class ProviderModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class Wallet(ProviderModel):
    currency: Literal["usd", "credits"]
    remaining_balance: Amount | None


class CreditPool(ProviderModel):
    remaining: NonnegativeAmount | None
    resets_at: AwareDatetime | None = None


class CreditPools(ProviderModel):
    premium_credits: CreditPool | None = None
    add_on_credits: CreditPool | None = None


class Subscription(ProviderModel):
    plan: Annotated[str, Field(max_length=100)]
    credits: CreditPools


class MeteredUsage(ProviderModel):
    spending_current_usd: NonnegativeAmount | None
    spending_cap_usd: NonnegativeAmount | None


class BillingProfile(ProviderModel):
    billing_type: Literal["wallet", "subscription", "usage_based"]
    wallet: Wallet | None = None
    subscription: Subscription | None = None
    usage_based: MeteredUsage | None = None

    @model_validator(mode="after")
    def active_billing(self) -> Self:
        if getattr(self, self.billing_type) is None:
            raise ValueError("Active billing details are missing")
        return self


class ProfileResponse(ProviderModel):
    data: BillingProfile
    error: None = None


ProfileResult = BillingProfile | Unavailable


def fetch_profile(key: SecretStr, transport: httpx.BaseTransport | None = None) -> ProfileResult:
    try:
        with httpx.Client(transport=transport, timeout=20, follow_redirects=False, trust_env=False) as client:
            with client.stream(
                "GET", "https://api.heygen.com/v3/users/me", headers={"X-Api-Key": key.get_secret_value()}
            ) as response:
                if response.status_code != 200:
                    message: Final = {
                        401: "HeyGen-Zugang ungültig oder abgelaufen",
                        403: "HeyGen-Kontobericht benötigt account:read",
                        429: "HeyGen-Abfragelimit erreicht",
                    }.get(response.status_code, "HeyGen-Kontobericht nicht verfügbar")
                    return Unavailable("error", message)
                raw: Final = BytesIO()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if raw.tell() + len(chunk) > 1_000_000:
                        return Unavailable("error", "HeyGen-Kontobericht überschreitet Größenlimit")
                    raw.write(chunk)
                return ProfileResponse.model_validate(json.loads(raw.getvalue(), parse_float=Decimal)).data
    except (ValidationError, json.JSONDecodeError, UnicodeError):
        return Unavailable("error", "HeyGen-Kontobericht unvollständig oder ungültig")
    except httpx.HTTPError:
        return Unavailable("error", "HeyGen-Netzwerkfehler oder Zeitüberschreitung")


def reading(
    account_id: str,
    result: ProfileResult,
    now: datetime,
    metric: Literal["balance", "usage", "spending_cap"],
    window: str,
    value: Decimal | None = None,
    unit: Literal["USD", "credits"] | None = None,
    resets_at: datetime | None = None,
) -> Observation:
    return Observation(
        account_id=account_id,
        external_id=sha256(f"{now.isoformat()}:{metric}:{window}:{value}:{unit}:{resets_at}".encode()).hexdigest(),
        metric=metric,
        window=window,
        source="provider_api",
        observed_at=now,
        collected_at=now,
        status=result.status if isinstance(result, Unavailable) else "known" if value is not None else "unknown",
        value=value,
        unit=unit if value is not None else None,
        resets_at=resets_at,
        expires_at=resets_at,
        message=result.message
        if isinstance(result, Unavailable)
        else ""
        if value is not None
        else "Für diese Abrechnungsart nicht ausgewiesen oder vom Anbieter unbekannt",
    )


def profile_readings(account_id: str, result: ProfileResult, now: datetime) -> tuple[Observation, ...]:
    wallet: Final = result.wallet if isinstance(result, BillingProfile) and result.billing_type == "wallet" else None
    subscription: Final = (
        result.subscription if isinstance(result, BillingProfile) and result.billing_type == "subscription" else None
    )
    metered: Final = (
        result.usage_based if isinstance(result, BillingProfile) and result.billing_type == "usage_based" else None
    )
    premium: Final = subscription.credits.premium_credits if subscription else None
    addon: Final = subscription.credits.add_on_credits if subscription else None
    return (
        reading(
            account_id,
            result,
            now,
            "balance",
            "API-Wallet",
            wallet.remaining_balance if wallet else None,
            "USD" if wallet and wallet.currency == "usd" else "credits" if wallet else None,
        ),
        reading(
            account_id,
            result,
            now,
            "balance",
            "Premium-Credits",
            premium.remaining if premium else None,
            "credits",
            premium.resets_at if premium else None,
        ),
        reading(
            account_id,
            result,
            now,
            "balance",
            "Zusatz-Credits",
            addon.remaining if addon else None,
            "credits",
            addon.resets_at if addon else None,
        ),
        reading(
            account_id,
            result,
            now,
            "usage",
            "Anbieterperiode (Grenzen nicht ausgewiesen)",
            metered.spending_current_usd if metered else None,
            "USD",
        ),
        reading(
            account_id,
            result,
            now,
            "spending_cap",
            "Anbieterperiode (Grenzen nicht ausgewiesen)",
            metered.spending_cap_usd if metered else None,
            "USD",
        ),
    )


def collect_heygen(
    path: Path,
    now: datetime,
    fetch: Callable[[SecretStr], ProfileResult] = fetch_profile,
    *,
    account_id: str = "heygen-api",
    account_name: str = "HeyGen API",
) -> ImportBatch:
    account: Final = Account(id=account_id, provider=Provider.HEYGEN, name=account_name, kind="api", state="active")
    key: Final = read_private_key(path, "HEYGEN_API_KEY", "HeyGen")
    result: Final = key if isinstance(key, Unavailable) else fetch(key)
    return ImportBatch(
        accounts=()
        if isinstance(result, Unavailable)
        else (
            account.model_copy(
                update={
                    "plan": result.subscription.plan
                    if result.billing_type == "subscription" and result.subscription
                    else ""
                }
            ),
        ),
        observations=profile_readings(account.id, result, now),
    )
