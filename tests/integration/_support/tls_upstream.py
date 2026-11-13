"""HTTPS provider that requires a TLS client certificate, for client certificate account route contracts.

The scripted upstream starts it next to its plain HTTP port. All material is generated at start-up and handed to
tests through the plain control endpoint, so nothing is stored in the repository.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True)
class Identity:
    certificate: str
    private_key: str


def _certificate(
    name: str, key: ec.EllipticCurvePrivateKey, issuer: x509.Name, signer: ec.EllipticCurvePrivateKey, kind: str
) -> x509.Certificate:
    now: Final = datetime.now(UTC)
    ca: Final = kind == "ca"
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
        usage: Final = ExtendedKeyUsageOID.SERVER_AUTH if kind == "server" else ExtendedKeyUsageOID.CLIENT_AUTH
        builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)  # rebind-ok: builder chain
    if kind == "server":
        builder = builder.add_extension(  # rebind-ok: builder chain
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False
        )
    return builder.sign(signer, hashes.SHA256())


def _pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def _key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


class Authority:
    def __init__(self, name: str) -> None:
        self.key: Final = ec.generate_private_key(ec.SECP256R1())
        self.name: Final = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        self.certificate: Final = _certificate(name, self.key, self.name, self.key, "ca")

    def issue(self, name: str, kind: str) -> Identity:
        key: Final = ec.generate_private_key(ec.SECP256R1())
        return Identity(_pem(_certificate(name, key, self.name, self.key, kind)), _key_pem(key))


class TlsProvider:
    """Answers every request with the client certificate's common name, the Authorization header and the path."""

    def __init__(self, port: int) -> None:
        self.port: Final = port
        self.server_ca: Final = Authority("integration provider root")
        self.client_ca: Final = Authority("integration client root")
        self.server: Final = self.server_ca.issue("127.0.0.1", "server")
        self.client: Final = self.client_ca.issue("integration-account-client", "client")
        self.stranger: Final = Authority("integration stranger root").issue("integration-stranger", "client")

    def material(self) -> dict[str, object]:
        return {
            "port": self.port,
            "provider_ca": _pem(self.server_ca.certificate),
            "client_certificate": self.client.certificate,
            "client_key": self.client.private_key,
            "stranger_certificate": self.stranger.certificate,
            "stranger_key": self.stranger.private_key,
        }

    def _context(self) -> ssl.SSLContext:
        context: Final = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        with tempfile.TemporaryDirectory() as directory:
            chain: Final = Path(directory) / "server.pem"
            _ = chain.write_text(self.server.certificate + self.server.private_key)
            context.load_cert_chain(chain)
        context.load_verify_locations(cadata=_pem(self.client_ca.certificate))
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head: Final = await reader.readuntil(b"\r\n\r\n")
            lines: Final = head.decode("latin-1").split("\r\n")
            headers: Final = {
                name.strip().lower(): value.strip()
                for name, _, value in (line.partition(":") for line in lines[1:] if ":" in line)
            }
            length: Final = int(headers.get("content-length", "0") or "0")
            if length:
                _ = await reader.readexactly(length)
            peer: Final = writer.get_extra_info("peercert") or {}
            subject: Final = dict(item[0] for item in peer.get("subject", ()))
            body: Final = json.dumps(
                {
                    "client": subject.get("commonName", ""),
                    "authorization": headers.get("authorization", ""),
                    "path": lines[0].split(" ")[1] if len(lines[0].split(" ")) > 1 else "",
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nconnection: close\r\ncontent-length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ssl.SSLError, ValueError):
            pass
        finally:
            writer.close()

    async def serve(self) -> None:
        server: Final = await asyncio.start_server(self._handle, "127.0.0.1", self.port, ssl=self._context())
        async with server:
            await server.serve_forever()
