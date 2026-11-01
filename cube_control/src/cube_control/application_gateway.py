from collections.abc import Mapping
from typing import Final
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr


def require_admin_origin(origin: str) -> None:
    parsed: Final = urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "::1")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise ValueError("Administrative gateway access requires an explicit loopback HTTP origin and port")


def gateway_client(origin: str, master_key: SecretStr, transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        base_url=origin,
        headers={"Authorization": "Bearer " + master_key.get_secret_value()},
        timeout=20,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


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
