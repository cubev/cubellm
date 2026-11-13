import asyncio
import ssl
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Final

import httpx
import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError

from litellm.models.credentials import CredentialItem
from litellm.proxy.pass_through_endpoints.account_credentials import (
    AccountHeaderCredential,
    CredentialTLS,
    resolve_account_credential,
    validate_account_route,
)
from litellm.proxy.pass_through_endpoints.credential_transport import CredentialTransport
from litellm.proxy.pass_through_endpoints.mtls_credentials import (
    MTLSCredentialAuth,
    clear_transport_cache,
    validate_tls_material,
)
from litellm.proxy.pass_through_endpoints.query_credentials import QueryCredentialAuth


@dataclass(frozen=True)
class Identity:
    certificate: str
    private_key: str


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _pem_key(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _issue(
    name: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: x509.Name,
    signer: ec.EllipticCurvePrivateKey,
    ca: bool,
    server: bool,
) -> x509.Certificate:
    now: Final = datetime.now(UTC)
    builder = (  # rebind-ok: certificate builder chain
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if not ca:
        builder = builder.add_extension(  # rebind-ok: certificate builder chain
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
    if server:
        builder = builder.add_extension(  # rebind-ok: certificate builder chain
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False
        )
    return builder.sign(signer, hashes.SHA256())


@dataclass(frozen=True)
class Authority:
    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    @classmethod
    def create(cls, name: str) -> "Authority":
        key: Final = _key()
        subject: Final = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        return cls(_issue(name, key, subject, key, ca=True, server=False), key)

    @property
    def pem(self) -> str:
        return self.certificate.public_bytes(serialization.Encoding.PEM).decode()

    def issue(self, name: str, server: bool = False) -> Identity:
        key: Final = _key()
        certificate: Final = _issue(name, key, self.certificate.subject, self.key, ca=False, server=server)
        return Identity(certificate.public_bytes(serialization.Encoding.PEM).decode(), _pem_key(key))


SERVER_CA: Final = Authority.create("fixture provider root")
CLIENT_CA: Final = Authority.create("fixture client root")
SERVER: Final = SERVER_CA.issue("127.0.0.1", server=True)
CLIENT: Final = CLIENT_CA.issue("fixture-account-client")
STRANGER: Final = Authority.create("fixture stranger root").issue("fixture-stranger")


@pytest.fixture(autouse=True)
def empty_cache() -> Iterator[None]:
    clear_transport_cache()
    yield
    clear_transport_cache()


@pytest_asyncio.fixture(loop_scope="function")
async def provider() -> AsyncIterator[str]:
    """HTTPS provider that requires a client certificate issued by CLIENT_CA and echoes its common name."""
    context: Final = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    with tempfile.TemporaryDirectory() as directory:
        chain: Final = Path(directory) / "server.pem"
        chain.write_text(SERVER.certificate + SERVER.private_key)
        context.load_cert_chain(chain)
    context.load_verify_locations(cadata=CLIENT_CA.pem)
    context.verify_mode = ssl.CERT_REQUIRED

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head: Final = await reader.readuntil(b"\r\n\r\n")
            peer: Final = writer.get_extra_info("peercert") or {}
            subject: Final = dict(item[0] for item in peer.get("subject", ()))
            headers: Final = dict(line.decode().split(": ", 1) for line in head.split(b"\r\n")[1:] if b": " in line)
            authorization: Final = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
            body: Final = (
                f'{{"client": "{subject.get("commonName", "")}", "authorization": "{authorization}"}}'.encode()
            )
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nconnection: close\r\ncontent-length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server: Final = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port: Final = server.sockets[0].getsockname()[1]
    try:
        yield f"https://127.0.0.1:{port}"
    finally:
        server.close()


def credential(
    identity: Identity = CLIENT, ca: str | None = SERVER_CA.pem, origin: str = "https://127.0.0.1", **extra: str
) -> CredentialItem:
    values: dict[str, str] = {  # mutable-ok: credential fixture assembled once
        "client certificate": identity.certificate,
        "client key": identity.private_key,
        **extra,
    }
    if ca is not None:
        values["provider ca"] = ca
    return CredentialItem(
        credential_name="fixture credential",
        credential_values=values,
        credential_info={"cube_account_id": "fixture-account", "cube_allowed_origins": [origin]},
    )


TLS: Final = CredentialTLS(
    certificate_field="client certificate", private_key_field="client key", ca_certificate_field="provider ca"
)
POLICY: Final = AccountHeaderCredential(account_id="fixture-account", credential_name="fixture credential", mtls=TLS)


async def send(url: str, auth: httpx.Auth | None) -> httpx.Response:
    async with httpx.AsyncClient(transport=CredentialTransport(httpx.AsyncHTTPTransport())) as client:
        return await client.get(url + "/v1/status", auth=auth)


def resolved(item: CredentialItem, url: str, policy: AccountHeaderCredential = POLICY) -> MTLSCredentialAuth:
    outbound: Final = resolve_account_credential(policy, item, url + "/v1").outbound
    assert isinstance(outbound, MTLSCredentialAuth)
    return outbound


async def test_the_provider_sees_the_account_client_certificate(provider: str) -> None:
    response: Final = await send(provider, resolved(credential(origin=provider), provider))
    assert (response.status_code, response.json()["client"]) == (200, "fixture-account-client")


async def test_without_the_account_certificate_the_provider_refuses_the_handshake(provider: str) -> None:
    with pytest.raises(httpx.HTTPError):
        await send(provider, None)


async def test_a_certificate_from_another_authority_is_refused(provider: str) -> None:
    with pytest.raises(httpx.HTTPError):
        await send(provider, resolved(credential(STRANGER, origin=provider), provider))


async def test_a_private_provider_ca_replaces_the_system_trust_store(provider: str) -> None:
    other_root: Final = Authority.create("unrelated root").pem
    with pytest.raises(httpx.ConnectError) as raised:
        await send(provider, resolved(credential(ca=other_root, origin=provider), provider))
    assert CLIENT.private_key.splitlines()[1] not in str(raised.value)


async def test_tls_combines_with_a_header_credential_and_a_query_destination(provider: str) -> None:
    policy: Final = AccountHeaderCredential.model_validate(
        POLICY.model_dump() | {"headers": [{"name": "Authorization", "field": "api key", "prefix": "Bearer "}]}
    )
    item: Final = credential(origin=provider, **{"api key": "fixture-api-key"})
    resolution: Final = resolve_account_credential(policy, item, provider + "/v1")
    assert resolution.headers == {"Authorization": "Bearer fixture-api-key"}
    assert isinstance(resolution.outbound, MTLSCredentialAuth)
    async with httpx.AsyncClient(transport=CredentialTransport(httpx.AsyncHTTPTransport())) as client:
        response: Final = await client.get(
            provider + "/v1/status", headers=resolution.headers, auth=resolution.outbound
        )
    assert response.json() == {"client": "fixture-account-client", "authorization": "Bearer fixture-api-key"}
    wrapped: Final = MTLSCredentialAuth(
        validate_tls_material(CLIENT.certificate, CLIENT.private_key, SERVER_CA.pem),
        QueryCredentialAuth("key", "fixture-query-key", "fixture-query-key"),
    )
    prepared: Final = await wrapped.prepare(httpx.Request("GET", provider + "/v1"))
    assert prepared.url.params["key"] == "fixture-query-key"
    assert wrapped.redact("fixture-query-key") == "[redacted]"


def test_one_transport_is_shared_per_client_certificate() -> None:
    first: Final = validate_tls_material(CLIENT.certificate, CLIENT.private_key, SERVER_CA.pem)
    pooled: Final = MTLSCredentialAuth(first).transport()
    again: Final = validate_tls_material(CLIENT.certificate, CLIENT.private_key, SERVER_CA.pem)
    assert MTLSCredentialAuth(again).transport() is pooled
    other: Final = validate_tls_material(STRANGER.certificate, STRANGER.private_key, SERVER_CA.pem)
    assert first == again != other
    assert MTLSCredentialAuth(first).transport() is MTLSCredentialAuth(again, private_key="x").transport()


@pytest.mark.parametrize(
    ("certificate", "private_key", "ca"),
    (
        ("not pem", CLIENT.private_key, None),
        (CLIENT.certificate, "not pem", None),
        (CLIENT.certificate, STRANGER.private_key, None),
        (CLIENT.certificate, CLIENT.private_key, "not pem"),
        (CLIENT.certificate.replace("\nMII", "\nMIX", 1), CLIENT.private_key, None),
    ),
)
def test_invalid_or_mismatched_material_fails_without_echoing_it(
    certificate: str, private_key: str, ca: str | None
) -> None:
    with pytest.raises(
        ValueError, match="not PEM encoded" if "not pem" in (certificate, private_key, ca) else "invalid"
    ) as raised:
        _ = validate_tls_material(certificate, private_key, ca)
    assert "PRIVATE KEY" not in str(raised.value) and "CERTIFICATE-----" not in str(raised.value)
    assert (raised.value.__cause__, raised.value.__context__) == (None, None)


def test_the_policy_validates_field_names_and_tls_alone_is_a_destination() -> None:
    assert AccountHeaderCredential.model_validate(POLICY.model_dump()).mtls == TLS
    with pytest.raises(ValidationError):
        CredentialTLS.model_validate(TLS.model_dump() | {"certificate_field": "bad\nname"})
    with pytest.raises(ValidationError):
        CredentialTLS.model_validate(TLS.model_dump() | {"unknown": "field"})


def test_missing_or_control_character_pem_values_are_rejected() -> None:
    item: Final = CredentialItem(
        credential_name="fixture credential",
        credential_values={
            "client certificate": CLIENT.certificate,
            "client key": CLIENT.private_key.replace("\n", "\x00", 2),
            "provider ca": SERVER_CA.pem,
        },
        credential_info={"cube_account_id": "fixture-account", "cube_allowed_origins": ["https://127.0.0.1"]},
    )
    with pytest.raises(ValueError, match="unavailable or invalid"):
        resolve_account_credential(POLICY, item, "https://127.0.0.1/v1")


def test_repr_has_no_material() -> None:
    assert repr(MTLSCredentialAuth("key", private_key=CLIENT.private_key)) == "MTLSCredentialAuth(<redacted>)"


def test_client_certificates_are_only_used_over_https() -> None:
    with pytest.raises(ValueError, match="HTTPS target"):
        validate_account_route("http://127.0.0.1:9/v1", True, False, {}, None, POLICY, ("GET",))
    validate_account_route("https://vendor.example/v1", True, False, {}, None, POLICY, ("GET",))
