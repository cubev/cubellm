import logging
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote, quote_plus

import httpx
import pytest

from litellm.proxy.pass_through_endpoints.credential_transport import CredentialTransport
from litellm.proxy.pass_through_endpoints.sigv4_credentials import SIGNATURE_HEADERS, SigV4CredentialAuth

# Vectors from the AWS SigV4 test suite, awslabs/aws-c-auth tests/aws-signing-test-suite/v4 (read 2026-11-02):
# get-vanilla, get-vanilla-with-session-token and get-vanilla-query-order-key-case.
ACCESS_KEY: Final = "AKIDEXAMPLE"
SECRET_KEY: Final = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
SESSION_TOKEN: Final = "6e86291e8372ff2a2260956d9b8aae1d763fbf315fa00fa31553b73ebf194267"


def vector_clock() -> datetime:
    return datetime(2015, 8, 30, 12, 36, tzinfo=UTC)


def signer(session_token: str | None = None) -> SigV4CredentialAuth:
    return SigV4CredentialAuth("us-east-1", "service", ACCESS_KEY, SECRET_KEY, session_token, clock=vector_clock)


@pytest.mark.parametrize(
    ("url", "token", "signed_headers", "signature"),
    (
        (
            "https://example.amazonaws.com/",
            None,
            "host;x-amz-date",
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
        ),
        (
            "https://example.amazonaws.com/",
            SESSION_TOKEN,
            "host;x-amz-date;x-amz-security-token",
            "07ec1639c89043aa0e3e2de82b96708f198cceab042d4a97044c66dd9f74e7f8",
        ),
        (
            "https://example.amazonaws.com/?Param2=value2&Param1=value1",
            None,
            "host;x-amz-date",
            "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500",
        ),
    ),
)
async def test_signatures_match_the_official_aws_test_vectors(
    url: str, token: str | None, signed_headers: str, signature: str
) -> None:
    signed: Final = await signer(token).prepare(httpx.Request("GET", url))
    assert signed.headers["x-amz-date"] == "20150830T123600Z"
    assert signed.headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    assert signed.url == httpx.URL(url)


def secret_forms() -> tuple[str, ...]:
    return tuple(
        form
        for value in (ACCESS_KEY, SECRET_KEY, SESSION_TOKEN)
        for form in (value, quote(value, safe=""), quote_plus(value))  # comprehension-ok: every encoding of each value
    )


async def verifying_provider(request: httpx.Request) -> httpx.Response:
    received: Final = await request.aread()
    unsigned: Final = httpx.Request(
        request.method,
        request.url,
        headers=tuple((name, value) for name, value in request.headers.items() if name not in SIGNATURE_HEADERS),
        content=received,
    )
    expected: Final = await signer(SESSION_TOKEN).prepare(unsigned)
    return httpx.Response(
        200,
        json={
            "signature_matches": request.headers["authorization"] == expected.headers["authorization"],
            "signed_headers": request.headers["authorization"].split("SignedHeaders=")[1].split(",")[0],
            "token_sent": request.headers.get("x-amz-security-token") == SESSION_TOKEN,
            "echo": f"key {ACCESS_KEY} rejected",
        },
    )


async def test_the_physical_request_is_signed_over_exactly_what_is_sent_and_forged_fields_are_replaced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="botocore")
    async with httpx.AsyncClient(transport=CredentialTransport(httpx.MockTransport(verifying_provider))) as client:
        response: Final = await client.send(
            httpx.Request(
                "POST",
                "https://service.us-east-1.amazonaws.com/v1/items?page=2",
                headers={
                    "Authorization": "AWS4-HMAC-SHA256 forged",
                    "X-Amz-Date": "19990101T000000Z",
                    "X-Amz-Security-Token": "forged-token",
                    "X-Amz-Content-Sha256": "forged",
                },
                json={"name": "item"},
            ),
            auth=signer(SESSION_TOKEN),
        )
    assert response.json() == {
        "signature_matches": True,
        "signed_headers": "accept-encoding;content-length;content-type;host;x-amz-date;x-amz-security-token",
        "token_sent": True,
        "echo": "key [redacted] rejected",
    }
    assert "HTTP Request" in caplog.text
    exposed: Final = (caplog.text, str(response.request.headers), str(response.url), repr(response.request))
    assert [
        form for form in secret_forms() for text in exposed if form in text
    ] == []  # comprehension-ok: every encoding against every exposed text
    assert response.request.headers["authorization"] == "AWS4-HMAC-SHA256 forged"


async def test_transport_errors_carry_no_credential_and_no_chained_frames() -> None:
    def provider(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {request.headers['authorization']}", request=request)

    async with httpx.AsyncClient(transport=CredentialTransport(httpx.MockTransport(provider))) as client:
        with pytest.raises(httpx.ConnectError) as raised:
            await client.send(httpx.Request("GET", "https://service.example/v1"), auth=signer(SESSION_TOKEN))
    assert "AKIDEXAMPLE" not in str(raised.value)
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)


def test_repr_keeps_the_scope_but_no_credential() -> None:
    assert repr(signer(SESSION_TOKEN)) == "SigV4CredentialAuth(service, us-east-1, <redacted>)"
