import asyncio
import gzip
import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Final
from urllib.parse import quote, quote_plus

import httpx
import pytest

from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.proxy.pass_through_endpoints.query_credentials import (
    BUFFERED_BODY_LIMIT,
    QueryCredentialAuth,
    QueryCredentialTransport,
)

NAME: Final = "ticket.v2~"
SECRET: Final = "fixture +/ credential&ticket=forged%"
PREFIX: Final = "k:"


def auth(secret: str = SECRET, prefix: str = PREFIX) -> QueryCredentialAuth:
    return QueryCredentialAuth(NAME, prefix + secret, secret)


def forms(value: str) -> tuple[str, ...]:
    percent: Final = (quote(value, safe=""), quote(value), quote_plus(value))
    escaped: Final = json.dumps(value)[1:-1]
    return (value, escaped, escaped.replace("/", "\\/"), *percent, *(form.replace("%2F", "%2f") for form in percent))


def leaks(*texts: str) -> list[str]:
    return [
        form for form in (*forms(PREFIX + SECRET), *forms(SECRET)) for text in texts if form in text
    ]  # comprehension-ok: every encoding against every exposed text


async def send(
    provider: Callable[[httpx.Request], httpx.Response], url: str, credential: QueryCredentialAuth | None = None
) -> httpx.Response:
    async with httpx.AsyncClient(transport=QueryCredentialTransport(httpx.MockTransport(provider))) as client:
        response: Final = await client.send(
            httpx.Request("GET", url), auth=credential or auth(), follow_redirects=False
        )
        await response.aread()
        return response


def wire_report(request: httpx.Request) -> httpx.Response:
    params: Final = request.url.params.multi_items()
    return httpx.Response(
        200,
        json={
            "params": [[name, value] for name, value in params if name != NAME],
            "credential": [value == PREFIX + SECRET for name, value in params if name == NAME],
            "accept_encoding": request.headers.get("accept-encoding"),
        },
    )


async def test_credential_reaches_only_the_wire_and_never_logs_or_response_urls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="httpx")
    response: Final = await send(wire_report, "https://vendor.example/v1?page=2&page=3&TICKET.v2~=forged&tag=a%2Bb")
    assert response.json() == {
        "params": [["page", "2"], ["page", "3"], ["tag", "a+b"]],
        "credential": [True],
        "accept_encoding": "identity",
    }
    assert "HTTP Request" in caplog.text
    assert leaks(caplog.text, str(response.url), str(response.request.url), repr(response.request)) == []


async def test_parallel_routes_keep_their_own_credentials() -> None:
    async def provider(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01 if request.url.params["page"] == "first" else 0)
        return httpx.Response(200, json={"page": request.url.params["page"], "ticket": request.url.params[NAME]})

    first: Final = QueryCredentialAuth(NAME, "first-route-key", "first-route-key")
    second: Final = QueryCredentialAuth(NAME, "second-route-key", "second-route-key")
    async with httpx.AsyncClient(transport=QueryCredentialTransport(httpx.MockTransport(provider))) as client:
        responses: Final = await asyncio.gather(
            client.send(httpx.Request("GET", "https://vendor.example/v1?page=first"), auth=first),
            client.send(httpx.Request("GET", "https://vendor.example/v1?page=second"), auth=second),
        )
    assert [response.json() for response in responses] == [
        {"page": "first", "ticket": "[redacted]"},
        {"page": "second", "ticket": "[redacted]"},
    ]
    assert [response.headers["content-length"] for response in responses] == [
        str(len(response.content)) for response in responses
    ]


async def test_without_the_credential_transport_nothing_is_injected() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=dict(request.url.params)))
    ) as client:
        response: Final = await client.send(httpx.Request("GET", "https://vendor.example/v1?page=1"), auth=auth())
    assert response.json() == {"page": "1"}


async def test_redirects_and_echoed_headers_never_expose_the_credential(caplog: pytest.LogCaptureFixture) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers=(
                (b"Location", str(request.url.copy_with(path="/moved")).encode()),
                (b"X-Echo", ("bare=" + quote_plus(SECRET)).encode()),
                (b"X-Request-Id", b"fixture-request"),
                (b"X-Label", "Grüße".encode()),
            ),
        )

    caplog.set_level(logging.DEBUG, logger="httpx")
    response: Final = await send(provider, "https://vendor.example/v1/items?page=2")
    assert response.status_code == 302 and response.next_request is not None
    assert response.headers["location"].startswith("https://vendor.example/moved?page=2&ticket.v2~=")
    assert response.headers["x-echo"] == "bare=[redacted]"
    assert (response.headers["x-request-id"], response.headers.raw[3][1]) == ("fixture-request", "Grüße".encode())
    assert leaks(caplog.text, str(response.headers.raw), str(response.next_request.url)) == []


def echo_body(request: httpx.Request) -> bytes:
    value: Final = request.url.params[NAME]
    return json.dumps(
        {
            "full": value,
            "bare": value.removeprefix(PREFIX),
            "link": "https://vendor.example/next?" + str(httpx.QueryParams({NAME: value})),
            "lower": quote(value, safe="").lower(),
            "escaped": json.dumps(value)[1:-1].replace("/", "\\/"),
            "usage": {"units": 3},
        }
    ).encode()


async def test_buffered_bodies_are_redacted_with_exact_framing_and_stale_digests_dropped() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=echo_body(request), headers={"Content-Digest": "sha-256=:stale:"})

    response: Final = await send(provider, "https://vendor.example/v1")
    assert leaks(response.text) == []
    assert response.json()["usage"] == {"units": 3} and response.json()["bare"] == "[redacted]"
    assert response.headers["content-length"] == str(len(response.content))
    assert "content-digest" not in response.headers

    clean: Final = await send(
        lambda request: httpx.Response(200, content=b'{"ok":true}', headers={"Content-Digest": "sha-256=:kept:"}),
        "https://vendor.example/v1",
    )
    assert (clean.content, clean.headers["content-digest"], clean.headers["content-length"]) == (
        b'{"ok":true}',
        "sha-256=:kept:",
        "11",
    )


async def chunked(parts: Iterable[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def test_streamed_bodies_are_redacted_when_a_secret_spans_every_chunk_boundary() -> None:
    wire: Final = (PREFIX + SECRET).encode()
    body: Final = b"data: " + wire + b" bare " + SECRET.encode() + b" end\n\n"
    for cut in range(1, len(body)):
        response = await send(
            lambda request, cut=cut: httpx.Response(200, content=chunked((body[:cut], body[cut:]))),
            "https://vendor.example/stream",
        )
        assert response.content == b"data: [redacted] bare [redacted] end\n\n", cut
        assert "content-length" not in response.headers


async def test_lying_length_and_compressed_bodies_switch_to_the_bounded_stream() -> None:
    large: Final = b"x" * BUFFERED_BODY_LIMIT + (PREFIX + SECRET).encode() + b"tail"
    lying: Final = await send(
        lambda request: httpx.Response(
            200, content=chunked((large[:1000], large[1000:])), headers={"Content-Length": "10"}
        ),
        "https://vendor.example/large",
    )
    assert lying.content == b"x" * BUFFERED_BODY_LIMIT + b"[redacted]tail"
    assert "content-length" not in lying.headers

    packed: Final = gzip.compress(b'{"key":"' + SECRET.encode() + b'"}')
    compressed: Final = await send(
        lambda request: httpx.Response(
            200, content=packed, headers={"Content-Encoding": "gzip", "Content-Length": str(len(packed))}
        ),
        "https://vendor.example/packed",
    )
    assert compressed.content == b'{"key":"[redacted]"}'
    assert "content-encoding" not in compressed.headers
    assert compressed.headers["content-length"] == str(len(compressed.content))


async def test_short_credentials_keep_protocol_framing_intact() -> None:
    response: Final = await send(
        lambda request: httpx.Response(200, content=b"abc7", headers={"X-Echo": "7"}),
        "https://vendor.example/short",
        QueryCredentialAuth(NAME, "7", "7"),
    )
    assert (response.content, response.headers["content-length"], response.headers["x-echo"]) == (
        b"abc[redacted]",
        "13",
        "[redacted]",
    )


async def test_transport_errors_carry_neither_the_outbound_url_nor_chained_frames() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(httpx.ConnectError) as raised:
        await send(provider, "https://vendor.example/v1?page=1")
    assert leaks(str(raised.value), str(raised.value.request.url)) == []
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)


def test_wrapped_handler_clients_keep_proxy_mounts_and_their_own_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://egress.example:3128")
    monkeypatch.setattr(AsyncHTTPHandler, "_should_use_aiohttp_transport", staticmethod(lambda: False))
    handlers: Final = tuple(
        AsyncHTTPHandler(transport_wrapper=QueryCredentialTransport, follow_redirects=False) for _ in range(2)
    )
    transports: Final = tuple(handler.client._transport for handler in handlers)  # pyright: ignore[reportPrivateUsage]  # pool ownership is only visible on the client
    mounts: Final = tuple(handler.client._mounts for handler in handlers)  # pyright: ignore[reportPrivateUsage]  # proxy mounts are only visible on the client
    assert all(isinstance(transport, QueryCredentialTransport) for transport in transports)
    assert transports[0]._pool is not None and transports[0]._pool is not transports[1]._pool  # pyright: ignore[reportPrivateUsage]  # forwarded pool used by eviction
    assert [[isinstance(mounted, QueryCredentialTransport) for mounted in mapping.values()] for mapping in mounts] == [
        [True],
        [True],
    ]


async def failing_body(request: httpx.Request) -> AsyncIterator[bytes]:
    yield b"partial "
    raise httpx.ReadError(f"connection lost while reading {request.url}", request=request)


@pytest.mark.parametrize("length", (None, "64"))
async def test_body_read_errors_after_partial_output_are_redacted_without_chained_frames(length: str | None) -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=failing_body(request), headers={"Content-Length": length} if length else {})

    async def read(client: httpx.AsyncClient) -> bytes:
        response: Final = await client.send(httpx.Request("GET", "https://vendor.example/v1"), auth=auth(), stream=True)
        received: Final[list[bytes]] = []  # mutable-ok: records what reached the client before the failure
        try:
            async for chunk in response.aiter_bytes():
                received.append(chunk)
        finally:
            await response.aclose()
        return b"".join(received)

    async with httpx.AsyncClient(transport=QueryCredentialTransport(httpx.MockTransport(provider))) as client:
        with pytest.raises(httpx.ReadError) as raised:
            await read(client)
    assert "connection lost while reading" in str(raised.value)
    assert leaks(str(raised.value), str(raised.value.request.url)) == []
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)


def mixed_case_escapes(form: str) -> str:
    return "".join(part.lower() if index % 2 else part for index, part in enumerate(re.split(r"(%[0-9A-F]{2})", form)))


async def test_mixed_case_percent_escapes_are_redacted_but_raw_key_case_still_matters() -> None:
    credential: Final = auth()
    encoded: Final = quote_plus(SECRET).replace("%2F", "%2f")
    assert credential.redact("echo:" + encoded) == "echo:[redacted]"
    assert credential.redact("echo:" + mixed_case_escapes(quote(PREFIX + SECRET, safe=""))) == "echo:[redacted]"
    other_case: Final = QueryCredentialAuth(NAME, "CaseKey-7", "CaseKey-7")
    assert other_case.redact("casekey-7 CaseKey-7") == "casekey-7 [redacted]"
    body: Final = ("data: " + encoded + " end").encode()
    for cut in range(1, len(body)):
        response = await send(
            lambda request, cut=cut: httpx.Response(200, content=chunked((body[:cut], body[cut:]))),
            "https://vendor.example/stream",
        )
        assert response.content == b"data: [redacted] end", cut
