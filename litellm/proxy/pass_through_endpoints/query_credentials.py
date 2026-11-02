import json
import re
from collections.abc import AsyncIterator, Generator
from types import MappingProxyType
from typing import Final
from urllib.parse import quote, quote_plus

import httpx

QUERY_CREDENTIAL_EXTENSION: Final = "cube_account_query_credential"
BUFFERED_BODY_LIMIT: Final = 1024 * 1024
REFRAMED_HEADERS: Final = frozenset({b"content-length", b"transfer-encoding", b"content-encoding"})
PROTOCOL_HEADERS: Final = frozenset({b"connection", b"keep-alive", b"trailer", b"te"})
DIGEST_HEADERS: Final = frozenset({b"digest", b"content-md5", b"content-digest", b"repr-digest"})
REDACTED: Final = "[redacted]"
HEX_ESCAPE: Final = re.compile(rb"%[0-9A-Fa-f]{1,2}")


def _encoded_forms(name: str, value: str) -> frozenset[str]:
    percent: Final = (
        quote(value, safe=""),
        quote(value),
        quote_plus(value),
        str(httpx.QueryParams(((name, value),)))[len(name) + 1 :],
    )
    escaped: Final = json.dumps(value)[1:-1]
    return frozenset(
        (
            value,
            escaped,
            escaped.replace("/", "\\/"),
            *percent,
        )
    )


def _upper_escapes(data: bytes) -> bytes:
    # Only percent-escape hex digits are case-insensitive; raw key characters keep their exact case.
    return HEX_ESCAPE.sub(lambda match: match.group().upper(), data)


class QueryCredentialAuth(httpx.Auth):
    # The client-level request keeps a clean URL, so httpx logs, response.url and exceptions never carry the
    # value; QueryCredentialTransport adds the parameter only on the way to the network.
    def __init__(self, name: str, value: str, secret: str) -> None:
        self._name: Final = name
        self._value: Final = value
        self._forms: Final = tuple(
            sorted(
                frozenset(
                    _upper_escapes(form.encode()) for form in _encoded_forms(name, value) | _encoded_forms(name, secret)
                ),
                key=len,
                reverse=True,
            )
        )
        self._pattern: Final = re.compile(b"|".join(re.escape(form) for form in self._forms))

    def __repr__(self) -> str:
        return "QueryCredentialAuth(<redacted>)"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        yield httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            stream=request.stream,
            extensions=MappingProxyType({**request.extensions, QUERY_CREDENTIAL_EXTENSION: self}),
        )

    def outbound(self, request: httpx.Request) -> httpx.Request:
        retained: Final = tuple(
            (name, value)
            for name, value in request.url.params.multi_items()
            if name.casefold() != self._name.casefold()
        )
        return httpx.Request(
            request.method,
            request.url.copy_with(params=httpx.QueryParams((*retained, (self._name, self._value)))),
            headers=(
                *((name, value) for name, value in request.headers.raw if name.lower() != b"accept-encoding"),
                (b"Accept-Encoding", b"identity"),
            ),
            stream=request.stream,
            extensions=MappingProxyType(
                {name: value for name, value in request.extensions.items() if name != QUERY_CREDENTIAL_EXTENSION}
            ),
        )

    def redact(self, text: str) -> str:
        return self.redact_bytes(text.encode("utf-8", "surrogatepass")).decode("utf-8", "surrogatepass")

    def redact_bytes(self, data: bytes) -> bytes:
        # Normalizing escapes keeps the length, so match positions in the copy are positions in the original.
        matches: Final = tuple(self._pattern.finditer(_upper_escapes(data)))
        starts: Final = (0, *(match.end() for match in matches))
        ends: Final = (*(match.start() for match in matches), len(data))
        return REDACTED.encode().join(data[start:end] for start, end in zip(starts, ends, strict=True))

    def split(self, window: bytes) -> tuple[bytes, bytes]:
        redacted: Final = self.redact_bytes(window)
        normalized: Final = _upper_escapes(redacted)
        held: Final = next(
            (
                size
                for size in range(min(len(normalized), len(self._forms[0]) - 1), 0, -1)
                if any(form.startswith(normalized[-size:]) for form in self._forms)
            ),
            0,
        )
        return redacted[: len(redacted) - held], redacted[len(redacted) - held :]


async def _guarded(
    chunks: AsyncIterator[bytes], credential: QueryCredentialAuth, request: httpx.Request
) -> AsyncIterator[bytes]:
    while True:
        try:
            chunk = await anext(chunks)
        except StopAsyncIteration:
            return
        except httpx.RequestError as error:
            kind: Final = type(error)
            message: Final = credential.redact(str(error))
            break
        yield chunk
    # Raised outside the handler so no chained exception or frame keeps the outbound request.
    raise kind(message, request=request)


class _RedactedStream(httpx.AsyncByteStream):
    def __init__(
        self, first: bytes, rest: AsyncIterator[bytes], response: httpx.Response, credential: QueryCredentialAuth
    ) -> None:
        self._first: Final = first
        self._rest: Final = rest
        self._response: Final = response
        self._credential: Final = credential

    async def _chunks(self) -> AsyncIterator[bytes]:
        if self._first:
            yield self._first
        async for chunk in self._rest:
            yield chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        held = b""  # rebind-ok: bounded tail carried from one upstream chunk to the next
        async for chunk in self._chunks():
            ready, held = self._credential.split(held + chunk)
            if ready:
                yield ready
        if held:
            yield self._credential.redact_bytes(held)

    async def aclose(self) -> None:
        await self._response.aclose()


def _headers(
    response: httpx.Response, credential: QueryCredentialAuth, length: int | None, changed: bool
) -> tuple[tuple[bytes, bytes], ...]:
    kept: Final = tuple(
        (name, value if name.lower() in PROTOCOL_HEADERS else credential.redact_bytes(value))
        for name, value in response.headers.raw
        if name.lower() not in REFRAMED_HEADERS and not (changed and name.lower() in DIGEST_HEADERS)
    )
    return kept if length is None else (*kept, (b"Content-Length", str(length).encode()))


class QueryCredentialTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner: Final = inner

    @property
    def _pool(self) -> object:
        return getattr(self._inner, "_pool", None)

    @property
    def client(self) -> object:
        return getattr(self._inner, "client", None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        credential: Final = request.extensions.get(QUERY_CREDENTIAL_EXTENSION)
        if not isinstance(credential, QueryCredentialAuth):
            return await self._inner.handle_async_request(request)
        try:
            upstream: Final = await self._inner.handle_async_request(credential.outbound(request))
        except httpx.RequestError as error:
            kind: Final = type(error)
            message: Final = credential.redact(str(error))
        else:
            return await self._redacted(upstream, credential, request)
        # Raised outside the handler so no chained exception or frame keeps the outbound request.
        raise kind(message, request=request)

    async def _redacted(
        self, upstream: httpx.Response, credential: QueryCredentialAuth, request: httpx.Request
    ) -> httpx.Response:
        declared: Final = upstream.headers.get("content-length")
        chunks: Final = _guarded(upstream.aiter_bytes(), credential, request)
        if declared is None or not declared.isdigit() or int(declared) > BUFFERED_BODY_LIMIT:
            return self._streamed(upstream, credential, b"", chunks)
        collected = b""  # rebind-ok: bounded read, at most BUFFERED_BODY_LIMIT plus one chunk
        try:
            async for chunk in chunks:
                collected += chunk
                if len(collected) > BUFFERED_BODY_LIMIT:
                    return self._streamed(upstream, credential, collected, chunks)
        except httpx.RequestError:
            await upstream.aclose()
            raise
        body: Final = credential.redact_bytes(collected)
        return httpx.Response(
            upstream.status_code,
            headers=_headers(
                upstream, credential, len(body), body != collected or "content-encoding" in upstream.headers
            ),
            content=body,
            extensions=upstream.extensions,
        )

    @staticmethod
    def _streamed(
        upstream: httpx.Response, credential: QueryCredentialAuth, first: bytes, rest: AsyncIterator[bytes]
    ) -> httpx.Response:
        return httpx.Response(
            upstream.status_code,
            headers=_headers(upstream, credential, None, True),
            stream=_RedactedStream(first, rest, upstream, credential),
            extensions=upstream.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()
