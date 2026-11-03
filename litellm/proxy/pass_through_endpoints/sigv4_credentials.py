from collections.abc import Callable, Generator
from datetime import UTC, datetime
from typing import Final

import httpx
from botocore.auth import SIGV4_TIMESTAMP, SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from .credential_transport import Redaction, marked

SIGNATURE_HEADERS: Final = frozenset({"authorization", "x-amz-date", "x-amz-security-token", "x-amz-content-sha256"})


class _TimedSigV4(SigV4Auth):
    # SigV4Auth.add_auth reads the wall clock and logs the canonical request, which can contain a session
    # token; this runs the same signing steps with an injected timestamp and without that debug output.
    def __init__(self, credentials: Credentials, service: str, region: str, timestamp: str) -> None:
        super().__init__(credentials, service, region)
        self._timestamp: Final = timestamp

    def add_auth(self, request: AWSRequest) -> None:
        request.context["timestamp"] = self._timestamp
        self._modify_request_before_signing(request)
        self._inject_signature_to_request(
            request, self.signature(self.string_to_sign(request, self.canonical_request(request)), request)
        )


class SigV4CredentialAuth(httpx.Auth):
    def __init__(
        self,
        region: str,
        service: str,
        access_key: str,
        secret_key: str,
        session_token: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._region: Final = region
        self._service: Final = service
        self._credentials: Final = Credentials(access_key, secret_key, session_token)
        self._clock: Final = clock
        self.redaction: Final = Redaction(value for value in (access_key, secret_key, session_token) if value)

    def __repr__(self) -> str:
        return f"SigV4CredentialAuth({self._service}, {self._region}, <redacted>)"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        yield marked(request, self)

    async def prepare(self, request: httpx.Request) -> httpx.Request:
        body: Final = await request.aread()
        unsigned: Final = {
            name: value for name, value in request.headers.items() if name.lower() not in SIGNATURE_HEADERS
        }
        signed: Final = AWSRequest(method=request.method, url=str(request.url), data=body, headers=unsigned)
        _TimedSigV4(
            self._credentials, self._service, self._region, self._clock().astimezone(UTC).strftime(SIGV4_TIMESTAMP)
        ).add_auth(signed)
        return httpx.Request(
            request.method,
            request.url,
            headers=tuple(signed.headers.items()),
            content=body,
            extensions=request.extensions,
        )

    def redact(self, text: str) -> str:
        return self.redaction.redact(text)
