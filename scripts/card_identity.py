"""The throwaway issuer the card proof's service trusts, served over its own HTTPS.

One local HTTPS server answers the issuer's key set and the look files. Its
certificate comes from a certificate authority made for the run and handed to the
service as `SSL_CERT_FILE`; every file lives under the directory the caller gives.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import ipaddress
import json
import ssl
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Final

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from PIL import Image

from video_client.wire import caller_headers

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

LOOK_NAMES: Final = ("alpha", "beta")
LOOKS_PATH: Final = "/looks/"
_KEY_SET_PATH: Final = "/certs"
_ISSUER_PATH: Final = "/realm"
_AUDIENCE: Final = "video-service"
_APP: Final = "proof"
_WORKLOAD_CLIENT: Final = "proof-workload"
_WORKLOAD_ROLE: Final = "workload"
_GROUP_CLAIM: Final = "groups"
_SERVICE_GROUP: Final = "/services/video-service"
_KEY_IDENTIFIER: Final = "proof-key"
_ALGORITHM: Final = "RS256"
_KEY_BITS: Final = 2048
_TOKEN_SECONDS: Final = 300
_CERTIFICATE_DAYS: Final = 1
_HOST: Final = "127.0.0.1"
_LOOK_SIZE: Final = (16, 16)
_AUTHORITY_USAGE: Final = dict.fromkeys(
    (
        "digital_signature",
        "content_commitment",
        "key_encipherment",
        "data_encipherment",
        "key_agreement",
        "key_cert_sign",
        "crl_sign",
        "encipher_only",
        "decipher_only",
    ),
    False,
) | {"key_cert_sign": True}
_LOOK_COLOURS: Final = {"alpha": (200, 30, 90), "beta": (30, 90, 200)}


@dataclass(frozen=True)
class Identity:
    """What the service is configured with, and the key the tokens are minted from."""

    ca_file: Path
    config_file: Path
    looks_file: Path
    look_sources: str
    issuer: str
    _private_key_pem: str

    def headers(self) -> dict[str, str]:
        """Fresh caller headers for one session upgrade."""
        issued = int(time.time())
        common = {
            "iss": self.issuer,
            "aud": _AUDIENCE,
            "azp": _WORKLOAD_CLIENT,
            "iat": issued,
            "exp": issued + _TOKEN_SECONDS,
        }
        workload = common | {
            "client_id": _WORKLOAD_CLIENT,
            "sub": "proof-service-account",
            "realm_access": {"roles": [_WORKLOAD_ROLE]},
        }
        person = common | {
            "sub": "proof-person",
            _GROUP_CLAIM: [f"/apps/{_APP}", _SERVICE_GROUP],
        }
        return caller_headers(
            workload_token=self._sign(workload), person_token=self._sign(person)
        )

    def _sign(self, claims: dict[str, object]) -> str:
        return jwt.encode(
            claims,
            self._private_key_pem,
            _ALGORITHM,
            headers={"kid": _KEY_IDENTIFIER},
        )


def _look_png(name: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", _LOOK_SIZE, _LOOK_COLOURS[name]).save(buffer, "PNG")
    return buffer.getvalue()


def _name(text: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, text)])


def _builder(
    subject: x509.Name, issuer: x509.Name, key: ec.EllipticCurvePrivateKey
) -> x509.CertificateBuilder:
    now = dt.datetime.now(tz=dt.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=_CERTIFICATE_DAYS))
    )


def _write_chain(directory: Path) -> tuple[Path, Path, Path]:
    """Write the authority's certificate and a server certificate and key it signed."""
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority = (
        _builder(_name("proof CA"), _name("proof CA"), authority_key)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(**_AUTHORITY_USAGE), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(authority_key.public_key()),
            critical=False,
        )
        .sign(authority_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = (
        _builder(_name(_HOST), authority.subject, server_key)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(_HOST))]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                authority_key.public_key()
            ),
            critical=False,
        )
        .sign(authority_key, hashes.SHA256())
    )
    ca_file = directory / "ca.pem"
    chain_file = directory / "server.pem"
    key_file = directory / "server-key.pem"
    ca_file.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    chain_file.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    return ca_file, chain_file, key_file


def _handler(files: dict[str, bytes]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            del args

        def do_GET(self) -> None:
            body = files.get(self.path)
            self.send_response(HTTPStatus.NOT_FOUND if body is None else HTTPStatus.OK)
            body = body or b""
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _configuration(issuer: str, key_set_url: str) -> str:
    return "\n".join(
        [
            "[identity]",
            f"issuer = {json.dumps(issuer)}",
            f"jwks_url = {json.dumps(key_set_url)}",
            f"audience = {json.dumps(_AUDIENCE)}",
            f"workload_role = {json.dumps(_WORKLOAD_ROLE)}",
            f"group_claim = {json.dumps(_GROUP_CLAIM)}",
            f"service_group = {json.dumps(_SERVICE_GROUP)}",
            "",
            f"[apps.{_APP}]",
            f"workload_client = {json.dumps(_WORKLOAD_CLIENT)}",
            "",
        ]
    )


@contextmanager
def throwaway_identity(directory: Path) -> Iterator[Identity]:
    """Serve the issuer and the looks under `directory` until the block ends."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_BITS)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk |= {"kid": _KEY_IDENTIFIER, "use": "sig", "alg": _ALGORITHM}
    looks = {name: _look_png(name) for name in LOOK_NAMES}
    files = {_KEY_SET_PATH: json.dumps({"keys": [jwk]}).encode()} | {
        f"{LOOKS_PATH}{name}": data for name, data in looks.items()
    }
    ca_file, chain_file, key_file = _write_chain(directory)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(chain_file, key_file)
    server = ThreadingHTTPServer((_HOST, 0), _handler(files))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    base = f"https://{_HOST}:{server.server_address[1]}"
    config_file = directory / "callers.toml"
    issuer = base + _ISSUER_PATH
    config_file.write_text(_configuration(issuer, base + _KEY_SET_PATH))
    looks_file = directory / "looks.toml"
    looks_file.write_text(
        "".join(
            f'[{name}]\nurl = "{base}{LOOKS_PATH}{name}"\n'
            f'sha256 = "{hashlib.sha256(data).hexdigest()}"\n'
            for name, data in looks.items()
        )
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield Identity(
            ca_file=ca_file,
            config_file=config_file,
            looks_file=looks_file,
            look_sources=json.dumps([f"{base}{LOOKS_PATH}"]),
            issuer=issuer,
            _private_key_pem=private_pem,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
