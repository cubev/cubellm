import re
from collections.abc import Mapping
from typing import Final


def application_id_from_key_metadata(metadata: Mapping[str, object] | None) -> str | None:
    candidate: Final = metadata.get("cube_application_id") if metadata is not None else None
    return (
        candidate
        if isinstance(candidate, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", candidate)
        else None
    )
