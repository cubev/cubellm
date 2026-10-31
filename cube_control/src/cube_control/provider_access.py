import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class Unavailable:
    status: Literal["error", "not_configured"]
    message: str


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
