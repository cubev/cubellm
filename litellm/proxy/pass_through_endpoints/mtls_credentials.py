import os
import ssl
import tempfile
from collections import OrderedDict
from collections.abc import Generator
from hashlib import sha256
from typing import Final

import httpx

from .credential_transport import OutboundCredential, Redaction, marked

TRANSPORT_CACHE_LIMIT: Final = 64
PEM_LIMIT: Final = 64 * 1024


# One TLS transport per client certificate: its connection pool must outlive the per-request auth object.
_TRANSPORTS: Final[OrderedDict[str, httpx.AsyncHTTPTransport]] = OrderedDict()  # mutable-ok: bounded LRU pool cache


def clear_transport_cache() -> None:
    _TRANSPORTS.clear()


def _context(certificate: str, private_key: str, ca_certificate: str | None) -> ssl.SSLContext:
    # A provided CA replaces the system trust store: a private provider CA must not widen trust for public hosts.
    context: Final = (
        ssl.create_default_context(cadata=ca_certificate)
        if ca_certificate is not None
        else ssl.create_default_context()
    )
    # load_cert_chain only reads files; the material lives in a private directory for the duration of the call.
    with tempfile.TemporaryDirectory(prefix="cube-mtls-") as directory:
        os.chmod(directory, 0o700)
        chain: Final = os.path.join(directory, "chain.pem")
        descriptor: Final = os.open(chain, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            _ = handle.write(certificate.strip() + "\n" + private_key.strip() + "\n")
        context.load_cert_chain(chain)
    return context


def _loaded(certificate: str, private_key: str, ca_certificate: str | None) -> ssl.SSLContext | None:
    try:
        return _context(certificate, private_key, ca_certificate)
    except (ssl.SSLError, ValueError, OSError):
        return None


def validate_tls_material(certificate: str, private_key: str, ca_certificate: str | None) -> str:
    """Builds the client TLS context once; returns its cache key. Errors never carry the material."""
    if (
        "-----BEGIN CERTIFICATE-----" not in certificate
        or "PRIVATE KEY-----" not in private_key
        or (ca_certificate is not None and "-----BEGIN CERTIFICATE-----" not in ca_certificate)
    ):
        raise ValueError("Named account credential TLS material is not PEM encoded")
    key: Final = sha256("\0".join((certificate, private_key, ca_certificate or "")).encode()).hexdigest()
    if key in _TRANSPORTS:
        _TRANSPORTS.move_to_end(key)
        return key
    context: Final = _loaded(certificate, private_key, ca_certificate)
    if context is None:
        # Raised outside any handler: no chained exception keeps the private key in a frame.
        raise ValueError("Named account credential TLS material is invalid")
    _TRANSPORTS[key] = httpx.AsyncHTTPTransport(verify=context, trust_env=False)
    while len(_TRANSPORTS) > TRANSPORT_CACHE_LIMIT:
        _ = _TRANSPORTS.popitem(last=False)
    return key


class MTLSCredentialAuth(httpx.Auth):
    """Sends the request over a TLS connection that presents the account's client certificate.

    It can wrap another outbound credential (query, SigV4 or OAuth) that still prepares the request itself.
    """

    def __init__(self, key: str, inner: OutboundCredential | None = None, private_key: str = "") -> None:
        self._key: Final = key
        self._inner: Final = inner
        self._own: Final = Redaction((private_key,)) if private_key else None

    def __repr__(self) -> str:
        return "MTLSCredentialAuth(<redacted>)"

    @property
    def redaction(self) -> Redaction:
        if self._inner is not None:
            return self._inner.redaction
        if self._own is None:
            raise ValueError("mTLS credential without material")
        return self._own

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        yield marked(request, self)

    async def prepare(self, request: httpx.Request) -> httpx.Request:
        return request if self._inner is None else await self._inner.prepare(request)

    def transport(self) -> httpx.AsyncBaseTransport:
        found: Final = _TRANSPORTS.get(self._key)
        if found is None:
            raise httpx.ConnectError("Account TLS client certificate is no longer loaded")
        _TRANSPORTS.move_to_end(self._key)
        return found

    def rejected(self) -> None:
        rejected: Final = getattr(self._inner, "rejected", None)
        if callable(rejected):
            _ = rejected()

    def redact(self, text: str) -> str:
        return self.redaction.redact(text)
