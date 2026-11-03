import json
import re
from collections.abc import AsyncIterator, Iterable
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable
from urllib.parse import quote, quote_plus

import httpx

CREDENTIAL_EXTENSION: Final = "cube_account_outbound_credential"
BUFFERED_BODY_LIMIT: Final = 1024 * 1024
REFRAMED_HEADERS: Final = frozenset({b"content-length", b"transfer-encoding", b"content-encoding"})
PROTOCOL_HEADERS: Final = frozenset({b"connection", b"keep-alive", b"trailer", b"te"})
DIGEST_HEADERS: Final = frozenset({b"digest", b"content-md5", b"content-digest", b"repr-digest"})
REDACTED: Final = "[redacted]"
HEX_ESCAPE: Final = re.compile(rb"%[0-9A-Fa-f]{1,2}")


def _encoded_forms(value: str) -> frozenset[str]:
    percent: Final = (
        quote(value, safe=""),
        quote(value),
        quote_plus(value),
        str(httpx.QueryParams((("x", value),)))[2:],
    )
    escaped: Final = json.dumps(value)[1:-1]
    return frozenset((value, escaped, escaped.replace("/", "\\/"), *percent))


def _upper_escapes(data: bytes) -> bytes:
    # Only percent-escape hex digits are case-insensitive; raw key characters keep their exact case.
    return HEX_ESCAPE.sub(lambda match: match.group().upper(), data)


class Redaction:
    def __init__(self, values: Iterable[str]) -> None:
        self._forms: Final = tuple(
            sorted(
                frozenset(
                    _upper_escapes(form.encode()) for value in values for form in _encoded_forms(value)
                ),  # comprehension-ok: every encoding of every secret value
                key=len,
                reverse=True,
            )
        )
        self._pattern: Final = re.compile(b"|".join(re.escape(form) for form in self._forms))

    def __repr__(self) -> str:
        return "Redaction(<redacted>)"

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


@runtime_checkable
class OutboundCredential(Protocol):
    redaction: Redaction

    async def prepare(self, request: httpx.Request) -> httpx.Request: ...


def marked(request: httpx.Request, credential: OutboundCredential) -> httpx.Request:
    # The client-level request keeps clean metadata, so httpx logs, response.url and exceptions never carry a
    # credential; CredentialTransport applies it only to the physical request on the way to the network.
    return httpx.Request(
        request.method,
        request.url,
        headers=request.headers,
        stream=request.stream,
        extensions=MappingProxyType({**request.extensions, CREDENTIAL_EXTENSION: credential}),
    )


def _unmarked(request: httpx.Request) -> httpx.Request:
    return httpx.Request(
        request.method,
        request.url,
        headers=(
            *((name, value) for name, value in request.headers.raw if name.lower() != b"accept-encoding"),
            (b"Accept-Encoding", b"identity"),
        ),
        stream=request.stream,
        extensions=MappingProxyType(
            {
                name: value  # pyright: ignore[reportAny]  # httpx types extension values as Any
                for name, value in request.extensions.items()  # pyright: ignore[reportAny]  # see above
                if name != CREDENTIAL_EXTENSION
            }
        ),
    )


def _sanitized(error: httpx.RequestError, redaction: Redaction, request: httpx.Request) -> httpx.RequestError:
    return type(error)(redaction.redact(str(error)), request=request)


async def _guarded(chunks: AsyncIterator[bytes], redaction: Redaction, request: httpx.Request) -> AsyncIterator[bytes]:
    try:
        async for chunk in chunks:
            yield chunk
    except httpx.RequestError as error:
        failure: Final = _sanitized(error, redaction, request)
    else:
        return
    # Raised outside the handler so no chained exception or frame keeps the outbound request.
    raise failure


class _RedactedStream(httpx.AsyncByteStream):
    def __init__(
        self, first: bytes, rest: AsyncIterator[bytes], response: httpx.Response, redaction: Redaction
    ) -> None:
        self._first: Final = first
        self._rest: Final = rest
        self._response: Final = response
        self._redaction: Final = redaction

    async def _chunks(self) -> AsyncIterator[bytes]:
        if self._first:
            yield self._first
        async for chunk in self._rest:
            yield chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        held = b""  # rebind-ok: bounded tail carried from one upstream chunk to the next
        async for chunk in self._chunks():
            ready, held = self._redaction.split(held + chunk)
            if ready:
                yield ready
        if held:
            yield self._redaction.redact_bytes(held)

    async def aclose(self) -> None:
        await self._response.aclose()


def _headers(
    response: httpx.Response, redaction: Redaction, length: int | None, changed: bool
) -> tuple[tuple[bytes, bytes], ...]:
    kept: Final = tuple(
        (name, value if name.lower() in PROTOCOL_HEADERS else redaction.redact_bytes(value))
        for name, value in response.headers.raw
        if name.lower() not in REFRAMED_HEADERS and not (changed and name.lower() in DIGEST_HEADERS)
    )
    return kept if length is None else (*kept, (b"Content-Length", str(length).encode()))


class CredentialTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner: Final = inner

    @property
    def _pool(self) -> object:
        return getattr(self._inner, "_pool", None)

    @property
    def client(self) -> object:
        return getattr(self._inner, "client", None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        credential: Final = request.extensions.get(CREDENTIAL_EXTENSION)
        if not isinstance(credential, OutboundCredential):
            return await self._inner.handle_async_request(request)
        redaction: Final = credential.redaction
        try:
            upstream: Final = await self._inner.handle_async_request(await credential.prepare(_unmarked(request)))
        except httpx.RequestError as error:
            failure: Final = _sanitized(error, redaction, request)
        else:
            return await self._redacted(upstream, redaction, request)
        # Raised outside the handler so no chained exception or frame keeps the outbound request.
        raise failure

    async def _redacted(self, upstream: httpx.Response, redaction: Redaction, request: httpx.Request) -> httpx.Response:
        declared: Final = next((value for name, value in upstream.headers.items() if name == "content-length"), None)
        chunks: Final = _guarded(upstream.aiter_bytes(), redaction, request)
        if declared is None or not declared.isdigit() or int(declared) > BUFFERED_BODY_LIMIT:
            return self._streamed(upstream, redaction, b"", chunks)
        collected = b""  # rebind-ok: bounded read, at most BUFFERED_BODY_LIMIT plus one chunk
        try:
            async for chunk in chunks:
                collected += chunk
                if len(collected) > BUFFERED_BODY_LIMIT:
                    return self._streamed(upstream, redaction, collected, chunks)
        except httpx.RequestError:
            await upstream.aclose()
            raise
        body: Final = redaction.redact_bytes(collected)
        return httpx.Response(
            upstream.status_code,
            headers=_headers(
                upstream, redaction, len(body), body != collected or "content-encoding" in upstream.headers
            ),
            content=body,
            extensions=upstream.extensions,
        )

    @staticmethod
    def _streamed(
        upstream: httpx.Response, redaction: Redaction, first: bytes, rest: AsyncIterator[bytes]
    ) -> httpx.Response:
        return httpx.Response(
            upstream.status_code,
            headers=_headers(upstream, redaction, None, True),
            stream=_RedactedStream(first, rest, upstream, redaction),
            extensions=upstream.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()
