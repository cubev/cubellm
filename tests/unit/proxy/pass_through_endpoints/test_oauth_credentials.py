import asyncio
import json
import logging
from base64 import b64decode
from collections.abc import Callable, Iterator
from typing import Final
from urllib.parse import parse_qsl, quote, quote_plus

import httpx
import pytest

from litellm.proxy.pass_through_endpoints.credential_transport import CredentialTransport
from litellm.proxy.pass_through_endpoints.oauth_credentials import (
    OAuthClientCredentialsAuth,
    OAuthTokenError,
    clear_token_cache,
)

TOKEN_URL: Final = "https://login.vendor.example/oauth/token"
CLIENT_ID: Final = "fixture client:id"
CLIENT_SECRET: Final = "fixture/secret+value=="
ACCESS: Final = "fixture-access-token.AbC_123~"


@pytest.fixture(autouse=True)
def empty_cache() -> Iterator[None]:
    clear_token_cache()
    yield
    clear_token_cache()


class TokenEndpoint:
    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[tuple[httpx.Request, bytes]] = []  # mutable-ok: test recorder
        self.responses: Final = list(responses)  # mutable-ok: scripted responses, consumed in order

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request, await request.aread()))
        await asyncio.sleep(0.01)  # a real endpoint answers later; concurrent callers must wait for this fetch
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def issued(token: str = ACCESS, lifetime: int | None = 3600, kind: str = "Bearer") -> httpx.Response:
    body: dict[str, object] = {"access_token": token, "token_type": kind}  # mutable-ok: response fixture
    if lifetime is not None:
        body["expires_in"] = lifetime
    return httpx.Response(200, json=body)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0  # rebind-ok: advanced by tests

    def __call__(self) -> float:
        return self.now


def auth(
    endpoint: TokenEndpoint,
    clock: Callable[[], float] | None = None,
    scope: str | None = "read write",
    method: str = "client_secret_basic",
) -> OAuthClientCredentialsAuth:
    return OAuthClientCredentialsAuth(
        TOKEN_URL,
        CLIENT_ID,
        CLIENT_SECRET,
        scope,
        method,  # pyright: ignore[reportArgumentType]  # test passes the literal values
        **({"clock": clock} if clock is not None else {}),
        transport=httpx.MockTransport(endpoint),
    )


async def test_basic_client_authentication_follows_rfc_6749_and_replaces_the_client_authorization() -> None:
    endpoint: Final = TokenEndpoint(issued())
    prepared: Final = await auth(endpoint).prepare(
        httpx.Request("POST", "https://api.vendor.example/v1/items", headers={"Authorization": "Bearer forged"})
    )
    assert prepared.headers.get_list("authorization") == [f"Bearer {ACCESS}"]
    ((request, body),) = endpoint.requests
    assert request.method == "POST" and str(request.url) == TOKEN_URL
    assert dict(parse_qsl(body.decode())) == {"grant_type": "client_credentials", "scope": "read write"}
    scheme, encoded = request.headers["authorization"].split(" ")
    user, password = b64decode(encoded).decode().split(":")
    assert (scheme, user, password) == ("Basic", quote(CLIENT_ID, safe=""), quote(CLIENT_SECRET, safe=""))
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"


async def test_post_client_authentication_sends_the_client_in_the_form_only() -> None:
    endpoint: Final = TokenEndpoint(issued())
    _ = await auth(endpoint, scope=None, method="client_secret_post").prepare(
        httpx.Request("GET", "https://api.vendor.example/")
    )
    ((request, body),) = endpoint.requests
    assert "authorization" not in request.headers
    assert dict(parse_qsl(body.decode())) == {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }


async def test_tokens_are_shared_across_requests_and_refreshed_before_they_expire() -> None:
    endpoint: Final = TokenEndpoint(issued("first-token", 600), issued("second-token", 600))
    clock: Final = Clock()
    for _ in range(3):
        prepared = await auth(endpoint, clock).prepare(httpx.Request("GET", "https://api.vendor.example/"))
        assert prepared.headers["authorization"] == "Bearer first-token"
    clock.now += 539
    assert (await auth(endpoint, clock).prepare(httpx.Request("GET", "https://api.vendor.example/"))).headers[
        "authorization"
    ] == "Bearer first-token"
    clock.now += 1
    assert (await auth(endpoint, clock).prepare(httpx.Request("GET", "https://api.vendor.example/"))).headers[
        "authorization"
    ] == "Bearer second-token"
    assert len(endpoint.requests) == 2


async def test_another_secret_or_scope_never_reuses_a_cached_token() -> None:
    endpoint: Final = TokenEndpoint(issued("one"), issued("two"), issued("three"))
    _ = await auth(endpoint).prepare(httpx.Request("GET", "https://api.vendor.example/"))
    _ = await auth(endpoint, scope="other").prepare(httpx.Request("GET", "https://api.vendor.example/"))
    other: Final = OAuthClientCredentialsAuth(
        TOKEN_URL, CLIENT_ID, "rotated-secret", "read write", transport=httpx.MockTransport(endpoint)
    )
    assert (await other.prepare(httpx.Request("GET", "https://api.vendor.example/"))).headers[
        "authorization"
    ] == "Bearer three"
    assert len(endpoint.requests) == 3


async def test_concurrent_requests_fetch_a_single_token() -> None:
    endpoint: Final = TokenEndpoint(issued())
    prepared: Final = await asyncio.gather(
        *(auth(endpoint).prepare(httpx.Request("GET", "https://api.vendor.example/")) for _ in range(20))
    )
    assert {request.headers["authorization"] for request in prepared} == {f"Bearer {ACCESS}"}
    assert len(endpoint.requests) == 1


@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(401, json={"error": "invalid_client", "detail": CLIENT_SECRET}),
        httpx.Response(302, headers={"Location": "https://elsewhere.example/token"}),
        httpx.Response(200, content=b"not json " + CLIENT_SECRET.encode()),
        httpx.Response(200, json=[ACCESS]),
        httpx.Response(200, json={"access_token": ACCESS}),
        httpx.Response(200, json={"access_token": ACCESS, "token_type": "mac"}),
        httpx.Response(200, json={"access_token": "with space", "token_type": "bearer"}),
        httpx.Response(200, json={"access_token": "", "token_type": "bearer"}),
        httpx.Response(200, json={"access_token": 1, "token_type": "bearer"}),
        httpx.Response(200, json={"access_token": ACCESS, "token_type": "bearer", "expires_in": 0}),
        httpx.Response(200, json={"access_token": ACCESS, "token_type": "bearer", "expires_in": "3600"}),
        httpx.Response(200, json={"access_token": ACCESS, "token_type": "bearer", "expires_in": True}),
        httpx.Response(200, content=json.dumps({"access_token": ACCESS, "token_type": "bearer", "pad": "x" * 70000})),
    ),
)
async def test_unusable_token_responses_fail_closed_without_echoing_the_endpoint(response: httpx.Response) -> None:
    endpoint: Final = TokenEndpoint(response)
    with pytest.raises(OAuthTokenError) as raised:
        _ = await auth(endpoint).prepare(httpx.Request("GET", "https://api.vendor.example/"))
    assert CLIENT_SECRET not in str(raised.value) and ACCESS not in str(raised.value)
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)
    assert len(endpoint.requests) == 1


async def test_missing_lifetime_uses_a_short_default_and_long_lifetimes_are_capped() -> None:
    clock: Final = Clock()
    endpoint: Final = TokenEndpoint(issued("short", None), issued("long", 10**9), issued("later", 10**9))
    _ = await auth(endpoint, clock).prepare(httpx.Request("GET", "https://api.vendor.example/"))
    clock.now += 241
    assert (await auth(endpoint, clock).prepare(httpx.Request("GET", "https://api.vendor.example/"))).headers[
        "authorization"
    ] == "Bearer long"
    clock.now += 24 * 60 * 60
    assert (await auth(endpoint, clock).prepare(httpx.Request("GET", "https://api.vendor.example/"))).headers[
        "authorization"
    ] == "Bearer later"


async def test_unreachable_token_endpoint_raises_a_clean_request_error() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.headers['authorization']}", request=request)

    failing: Final = OAuthClientCredentialsAuth(
        TOKEN_URL, CLIENT_ID, CLIENT_SECRET, transport=httpx.MockTransport(refused)
    )
    with pytest.raises(OAuthTokenError) as raised:
        _ = await failing.prepare(httpx.Request("GET", "https://api.vendor.example/"))
    assert str(raised.value) == "OAuth token request failed"
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)


def secret_forms() -> tuple[str, ...]:
    return tuple(
        form
        for value in (CLIENT_SECRET, ACCESS)
        for form in (value, quote(value, safe=""), quote_plus(value))  # comprehension-ok: every encoding
    )


async def test_provider_responses_and_logs_never_show_the_secret_or_the_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="httpx")

    def provider(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Echo": request.headers["authorization"]},
            json={"echo": request.headers["authorization"], "secret": quote_plus(CLIENT_SECRET)},
        )

    endpoint: Final = TokenEndpoint(issued())
    async with httpx.AsyncClient(transport=CredentialTransport(httpx.MockTransport(provider))) as client:
        response: Final = await client.send(httpx.Request("GET", "https://api.vendor.example/v1"), auth=auth(endpoint))
    assert response.json() == {"echo": "Bearer [redacted]", "secret": "[redacted]"}
    assert response.headers["x-echo"] == "Bearer [redacted]"
    exposed: Final = (caplog.text, str(response.request.headers), repr(response.request), str(response.headers))
    assert [form for form in secret_forms() for text in exposed if form in text] == []  # comprehension-ok: all pairs


async def test_a_rejected_token_is_dropped_and_the_next_request_fetches_a_new_one() -> None:
    statuses: Final = [401, 200]  # mutable-ok: scripted provider statuses
    seen: Final[list[str]] = []  # mutable-ok: recorder

    def provider(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(statuses.pop(0))

    endpoint: Final = TokenEndpoint(issued("revoked"), issued("fresh"))
    async with httpx.AsyncClient(transport=CredentialTransport(httpx.MockTransport(provider))) as client:
        first: Final = await client.send(httpx.Request("GET", "https://api.vendor.example/"), auth=auth(endpoint))
        second: Final = await client.send(httpx.Request("GET", "https://api.vendor.example/"), auth=auth(endpoint))
    assert (first.status_code, second.status_code) == (401, 200)
    assert seen == ["Bearer revoked", "Bearer fresh"]


async def test_transport_errors_after_issuing_hide_the_token_and_the_secret() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError(f"reset after {request.headers['authorization']} {CLIENT_SECRET}", request=request)

    async with httpx.AsyncClient(transport=CredentialTransport(httpx.MockTransport(provider))) as client:
        with pytest.raises(httpx.ReadError) as raised:
            _ = await client.send(
                httpx.Request("GET", "https://api.vendor.example/"), auth=auth(TokenEndpoint(issued()))
            )
    assert ACCESS not in str(raised.value) and CLIENT_SECRET not in str(raised.value)


def test_repr_has_no_credential() -> None:
    assert repr(auth(TokenEndpoint(issued()))) == "OAuthClientCredentialsAuth(<redacted>)"
