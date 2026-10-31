from collections.abc import Callable
import json
from datetime import datetime
from decimal import Decimal, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from cube_control.models import Account, ImportBatch, Observation, Provider
from cube_control.provider_access import Unavailable, read_private_key


class Credits(BaseModel):
    model_config = ConfigDict(extra="ignore")
    total_credits: Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=1000, decimal_places=500)]
    total_usage: Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=1000, decimal_places=500)]


class CreditsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: Credits


CreditsResult = Credits | Unavailable


def read_management_key(path: Path) -> SecretStr | Unavailable:
    return read_private_key(path, "OPENROUTER_MANAGEMENT_KEY", "OpenRouter")


def fetch_credits(key: SecretStr, transport: httpx.BaseTransport | None = None) -> CreditsResult:
    try:
        with httpx.Client(transport=transport, timeout=20, follow_redirects=False, trust_env=False) as client:
            response: Final[httpx.Response] = client.get(
                "https://openrouter.ai/api/v1/credits", headers={"Authorization": "Bearer " + key.get_secret_value()}
            )
        if response.status_code != 200:
            message: Final[str] = {
                401: "OpenRouter-Zugang ungültig oder abgelaufen",
                403: "OpenRouter-Kontoguthaben benötigt Management-Berechtigung",
                429: "OpenRouter-Abfragelimit erreicht",
            }.get(response.status_code, "OpenRouter-Kontobericht nicht verfügbar")
            return Unavailable("error", message)
        if len(response.content) > 1_000_000:
            return Unavailable("error", "OpenRouter-Kontobericht überschreitet Größenlimit")
        return CreditsResponse.model_validate(json.loads(response.content, parse_float=Decimal)).data
    except (ValidationError, json.JSONDecodeError, UnicodeError):
        return Unavailable("error", "OpenRouter-Kontobericht unvollständig oder ungültig")
    except httpx.HTTPError:
        return Unavailable("error", "OpenRouter-Netzwerkfehler oder Zeitüberschreitung")


def observation(
    metric: Literal["balance", "usage"], result: CreditsResult, now: datetime, account_id: str
) -> Observation:
    unavailable: Final[bool] = isinstance(result, Unavailable)
    value: Final[Decimal | None] = (
        None
        if isinstance(result, Unavailable)
        else remaining_credits(result)
        if metric == "balance"
        else result.total_usage
    )
    return Observation(
        account_id=account_id,
        external_id=sha256(f"{metric}:{now.isoformat()}:{value}".encode()).hexdigest(),
        metric=metric,
        window="Kontoguthaben" if metric == "balance" else "Gesamtnutzung seit Kontoeröffnung",
        status=result.status if isinstance(result, Unavailable) else "known",
        source="provider_api",
        observed_at=now,
        collected_at=now,
        value=value,
        unit=None if unavailable else "USD",
        message=result.message if isinstance(result, Unavailable) else "",
    )


def remaining_credits(result: Credits) -> Decimal:
    credits: Final = result.total_credits.as_tuple()
    usage: Final = result.total_usage.as_tuple()
    with localcontext() as context:
        context.prec = (
            max(len(credits.digits), len(usage.digits)) + abs(int(credits.exponent) - int(usage.exponent)) + 1
        )
        return result.total_credits - result.total_usage


def collect_openrouter(
    path: Path,
    now: datetime,
    fetch: Callable[[SecretStr], CreditsResult] = fetch_credits,
    *,
    account_id: str = "openrouter-api",
    account_name: str = "OpenRouter API",
) -> ImportBatch:
    account: Final = Account(id=account_id, provider=Provider.OPENROUTER, name=account_name, kind="api", state="active")
    key: Final = read_management_key(path)
    result: Final[CreditsResult] = key if isinstance(key, Unavailable) else fetch(key)
    accounts: Final[tuple[Account, ...]] = () if isinstance(result, Unavailable) else (account,)
    return ImportBatch(
        accounts=accounts,
        observations=tuple(observation(metric, result, now, account.id) for metric in ("balance", "usage")),
    )
