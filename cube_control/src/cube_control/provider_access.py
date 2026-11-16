import os
import stat
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Final, Literal

import httpx
from pydantic import BaseModel, SecretStr


@dataclass(frozen=True, slots=True)
class Unavailable:
    status: Literal["error", "not_configured"]
    message: str


def fetch_report[Report: BaseModel](
    endpoint: str,
    headers: Mapping[str, str],
    response_type: type[Report],
    provider: str,
    forbidden: str,
    transport: httpx.BaseTransport | None = None,
    transform: Callable[[Report, Mapping[str, str]], Report] | None = None,
) -> Report | Unavailable:
    try:
        with httpx.Client(transport=transport, timeout=20, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", endpoint, headers=headers) as response:
                if response.status_code != 200:
                    message: Final = {
                        401: f"{provider}-Zugang ungültig oder abgelaufen",
                        403: forbidden,
                        429: f"{provider}-Abfragelimit erreicht",
                    }.get(response.status_code, f"{provider}-Kontobericht nicht verfügbar")
                    return Unavailable("error", message)
                raw: Final = BytesIO()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if raw.tell() + len(chunk) > 1_000_000:
                        return Unavailable("error", f"{provider}-Kontobericht überschreitet Größenlimit")
                    raw.write(chunk)
                report: Final = response_type.model_validate(json.loads(raw.getvalue(), parse_float=Decimal))
                return transform(report, response.headers) if transform is not None else report
    except ValueError:
        return Unavailable("error", f"{provider}-Kontobericht unvollständig oder ungültig")
    except httpx.HTTPError:
        return Unavailable("error", f"{provider}-Netzwerkfehler oder Zeitüberschreitung")


def read_private_key(path: Path, variable: str, provider: str) -> SecretStr | Unavailable:
    try:
        descriptor: Final = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            metadata: Final = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                return Unavailable("error", "Zugangsdatei muss regulär, privat und im Besitz des Benutzers sein")
            raw_file: Final = stream.read(65537)
        if len(raw_file) > 65536:
            return Unavailable("error", "Zugangsdatei überschreitet Größenlimit")
    except (OSError, UnicodeError):
        return Unavailable("error", "Zugangsdatei nicht lesbar")
    values: Final = tuple(
        line.partition("=")[2].strip() for line in raw_file.splitlines() if line.partition("=")[0].strip() == variable
    )
    if len(values) > 1:
        return Unavailable("error", f"{provider}-Zugang mehrfach konfiguriert")
    if not values or not values[0]:
        return Unavailable("not_configured", f"{provider}-Zugang fehlt")
    raw: Final = values[0]
    value: Final = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'" else raw
    if not value:
        return Unavailable("not_configured", f"{provider}-Zugang fehlt")
    if len(value) > 8192 or any(ord(character) < 33 or ord(character) > 126 for character in value):
        return Unavailable("error", f"{provider}-Zugangsformat ungültig")
    return SecretStr(value)
