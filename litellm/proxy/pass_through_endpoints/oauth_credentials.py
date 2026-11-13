import asyncio
import time
from base64 import b64encode
from collections import OrderedDict
from collections.abc import Callable, Generator
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Final, Literal, TypeAlias
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from .credential_transport import Redaction, marked

TOKEN_RESPONSE_LIMIT: Final = 64 * 1024
TOKEN_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_LIFETIME_SECONDS: Final = 300
MAXIMUM_LIFETIME_SECONDS: Final = 24 * 60 * 60
REFRESH_MARGIN_SECONDS: Final = 60
CACHE_LIMIT: Final = 256
ClientAuthentication: TypeAlias = Literal["client_secret_basic", "client_secret_post"]


class _TokenResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    access_token: Annotated[StrictStr, Field(pattern=r"^[\x21-\x7e]{1,8192}$")]
    token_type: StrictStr
    expires_in: Annotated[StrictInt, Field(gt=0)] | None = None


class OAuthTokenError(httpx.RequestError):
    """The token endpoint did not issue a usable access token. Never carries endpoint output."""


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    expires_at: float


# One process-wide cache: route resolution builds a new auth object per request, the token must outlive it.
_TOKENS: Final[OrderedDict[str, _Token]] = OrderedDict()  # mutable-ok: bounded LRU token cache shared by requests
_LOCKS: Final[dict[str, asyncio.Lock]] = {}  # mutable-ok: one fetch lock per cached token key


def clear_token_cache() -> None:
    _TOKENS.clear()
    _LOCKS.clear()


def _remember(key: str, token: _Token) -> None:
    _TOKENS[key] = token
    _TOKENS.move_to_end(key)
    while len(_TOKENS) > CACHE_LIMIT:
        evicted, _ = _TOKENS.popitem(last=False)
        _ = _LOCKS.pop(evicted, None)


def _form(pairs: tuple[tuple[str, str], ...]) -> bytes:
    return "&".join(quote(name, safe="") + "=" + quote(value, safe="") for name, value in pairs).encode()


class OAuthClientCredentialsAuth(httpx.Auth):
    """OAuth 2.0 client credentials grant (RFC 6749 section 4.4) with a bearer access token (RFC 6750)."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        client_authentication: ClientAuthentication = "client_secret_basic",
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token_url: Final = token_url
        self._client_id: Final = client_id
        self._client_secret: Final = client_secret
        self._scope: Final = scope
        self._client_authentication: Final = client_authentication
        self._clock: Final = clock
        self._transport: Final = transport
        self._key: Final = sha256(
            "\0".join((token_url, client_id, client_secret, scope or "", client_authentication)).encode()
        ).hexdigest()
        self._base: Final = Redaction((client_secret,))
        self._token: str | None = None

    def __repr__(self) -> str:
        return "OAuthClientCredentialsAuth(<redacted>)"

    @property
    def redaction(self) -> Redaction:
        # The access token is only known after prepare(); from then on it is redacted as well.
        return self._base if self._token is None else Redaction((self._client_secret, self._token))

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        yield marked(request, self)

    def rejected(self) -> None:
        """The provider refused the token (HTTP 401): the next request fetches a new one."""
        cached: Final = _TOKENS.get(self._key)
        if cached is not None and cached.value == self._token:
            del _TOKENS[self._key]

    async def prepare(self, request: httpx.Request) -> httpx.Request:
        self._token = await self._access_token()
        return httpx.Request(
            request.method,
            request.url,
            headers=(
                *((name, value) for name, value in request.headers.raw if name.lower() != b"authorization"),
                (b"Authorization", b"Bearer " + self._token.encode("ascii")),
            ),
            stream=request.stream,
            extensions=request.extensions,
        )

    def redact(self, text: str) -> str:
        return self.redaction.redact(text)

    async def _access_token(self) -> str:
        cached: Final = _TOKENS.get(self._key)
        if cached is not None and cached.expires_at > self._clock():
            return cached.value
        lock: Final = _LOCKS.setdefault(self._key, asyncio.Lock())
        async with lock:
            current: Final = _TOKENS.get(self._key)
            if current is not None and current.expires_at > self._clock():
                return current.value
            token: Final = await self._fetch()
            _remember(self._key, token)
            return token.value

    def _request(self) -> httpx.Request:
        basic: Final = self._client_authentication == "client_secret_basic"
        # RFC 6749 section 2.3.1: both parts are form-urlencoded before Basic encoding.
        credentials: Final = quote(self._client_id, safe="") + ":" + quote(self._client_secret, safe="")
        pairs: Final = (
            ("grant_type", "client_credentials"),
            *((("scope", self._scope),) if self._scope is not None else ()),
            *(() if basic else (("client_id", self._client_id), ("client_secret", self._client_secret))),
        )
        headers: Final = (
            ("Accept", "application/json"),
            ("Content-Type", "application/x-www-form-urlencoded"),
            *((("Authorization", "Basic " + b64encode(credentials.encode()).decode()),) if basic else ()),
        )
        return httpx.Request("POST", self._token_url, headers=headers, content=_form(pairs))

    async def _send(self, request: httpx.Request) -> tuple[int, bytes] | None:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=TOKEN_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
            ) as client:
                response: Final = await client.send(request, stream=True)
                try:
                    body = b""  # rebind-ok: bounded read of the token response
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > TOKEN_RESPONSE_LIMIT:
                            break
                finally:
                    await response.aclose()
        except httpx.HTTPError:
            return None
        return response.status_code, body

    async def _fetch(self) -> _Token:
        started: Final = self._clock()
        answer: Final = await self._send(self._request())
        if answer is None:
            # Raised outside any handler: no chained exception keeps the token request or its credentials.
            raise OAuthTokenError("OAuth token request failed")
        token: Final = self._parse(*answer)
        if token is None:
            raise OAuthTokenError("OAuth token endpoint did not issue a bearer token")
        value, lifetime = token
        return _Token(value, started + max(lifetime - REFRESH_MARGIN_SECONDS, lifetime / 2))

    @staticmethod
    def _parse(status: int, body: bytes) -> tuple[str, float] | None:
        if status != 200 or len(body) > TOKEN_RESPONSE_LIMIT:
            return None
        try:
            issued: Final = _TokenResponse.model_validate_json(body)
        except ValidationError:
            return None
        if issued.token_type.lower() != "bearer":
            return None
        lifetime: Final = DEFAULT_LIFETIME_SECONDS if issued.expires_in is None else issued.expires_in
        return issued.access_token, min(lifetime, MAXIMUM_LIFETIME_SECONDS)
