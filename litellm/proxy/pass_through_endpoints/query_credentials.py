from collections.abc import Generator
from typing import Final

import httpx

from .credential_transport import Redaction, marked


class QueryCredentialAuth(httpx.Auth):
    def __init__(self, name: str, value: str, secret: str) -> None:
        self._name: Final = name
        self._value: Final = value
        self.redaction: Final = Redaction((value, secret))

    def __repr__(self) -> str:
        return "QueryCredentialAuth(<redacted>)"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        yield marked(request, self)

    async def prepare(self, request: httpx.Request) -> httpx.Request:
        retained: Final = tuple(
            (name, value)
            for name, value in request.url.params.multi_items()
            if name.casefold() != self._name.casefold()
        )
        return httpx.Request(
            request.method,
            request.url.copy_with(params=httpx.QueryParams((*retained, (self._name, self._value)))),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )

    def redact(self, text: str) -> str:
        return self.redaction.redact(text)
