import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from cube_control.models import Account, ImportBatch, Observation, Provider


class Credits(BaseModel):
    model_config = ConfigDict(extra="ignore")
    total_credits: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    total_usage: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class CreditsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: Credits


@dataclass(frozen=True, slots=True)
class Unavailable:
    status: Literal["error", "not_configured"]
    message: str


CreditsResult = Credits | Unavailable


def read_management_key(path: Path) -> SecretStr | Unavailable:
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            return Unavailable("error", "Zugangsdatei muss privat sein und dem Benutzer gehören")
        lines: Final[tuple[str, ...]] = tuple(path.read_text().splitlines())
    except (OSError, UnicodeError):
        return Unavailable("error", "Zugangsdatei nicht lesbar")
    values: Final[tuple[str, ...]] = tuple(
        line.partition("=")[2].strip()
        for line in lines
        if line.partition("=")[0].strip() == "OPENROUTER_MANAGEMENT_KEY"
    )
    if len(values) > 1:
        return Unavailable("error", "OpenRouter-Zugang mehrfach konfiguriert")
    if not values or not values[0]:
        return Unavailable("not_configured", "OpenRouter-Management-Zugang fehlt")
    raw: Final[str] = values[0]
    value: Final[str] = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'" else raw
    if not value:
        return Unavailable("not_configured", "OpenRouter-Management-Zugang fehlt")
    if len(value) > 8192 or any(ord(character) < 33 or ord(character) > 126 for character in value):
        return Unavailable("error", "OpenRouter-Zugangsformat ungültig")
    return SecretStr(value)


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
        return CreditsResponse.model_validate_json(response.content).data
    except ValidationError:
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
        else result.total_credits - result.total_usage
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
