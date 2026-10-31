from collections.abc import Mapping
from typing import Final

import httpx


def request_gateway(
    client: httpx.Client,
    method: str,
    path: str,
    body: Mapping[str, object] | None = None,
    params: Mapping[str, str] | None = None,
) -> tuple[int, bytes]:
    with client.stream(method, path, json=body, params=params) as response:
        content: Final = bytearray()
        for chunk in response.iter_bytes():
            if len(content) + len(chunk) > 1_000_000:
                raise ValueError("Gateway response exceeds size limit")
            content.extend(chunk)
        return response.status_code, bytes(content)
