"""A throwaway issuer for the session wire: a key, a key set, tokens."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Final

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from webauth.https_client import KeySetFetchError

from video_client.wire import caller_headers
from video_service.callers import VideoCallers
from video_service.config import AppConfig, CallerConfig, IdentityConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ISSUER: Final = "https://identity.test/realm"
KEY_SET_ADDRESS: Final = "https://identity.test/certs?credential=SENTINEL"
AUDIENCE: Final = "video-service"
APP: Final = "quiz"
CLIENT: Final = "quiz-client"
OTHER_APP: Final = "chess"
OTHER_CLIENT: Final = "chess-client"
SERVICE_GROUP: Final = "/services/video-service"
WORKLOAD_ROLE: Final = "workload"
GROUP_CLAIM: Final = "groups"
KEY_IDENTIFIER: Final = "key-one"
ALGORITHM: Final = "RS256"
_KEY_BITS: Final = 2048
_LIFETIME_SECONDS: Final = 300


@dataclass(frozen=True)
class _Key:
    private_pem: str = field(repr=False)
    public_jwk: dict[str, str]


@cache
def _key() -> _Key:
    private = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_BITS)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return _Key(pem, jwk | {"kid": KEY_IDENTIFIER, "use": "sig", "alg": ALGORITHM})


class KeySet:
    """The issuer's key-set endpoint: it publishes the key, and can go down."""

    def __init__(self) -> None:
        """Start up."""
        self.down = False

    def __call__(self, _url: str) -> bytes:
        """Answer the fetch like the issuer: the key set, or a failure."""
        if self.down:
            message = "the key set is down"
            raise KeySetFetchError(message)
        return json.dumps({"keys": [_key().public_jwk]}).encode()


def _caller_config() -> CallerConfig:
    return CallerConfig(
        identity=IdentityConfig(
            issuer=ISSUER,
            jwks_url=KEY_SET_ADDRESS,
            audience=AUDIENCE,
            workload_role=WORKLOAD_ROLE,
            group_claim=GROUP_CLAIM,
            service_group=SERVICE_GROUP,
        ),
        apps={
            APP: AppConfig(workload_client=CLIENT),
            OTHER_APP: AppConfig(workload_client=OTHER_CLIENT),
        },
    )


def callers_over(key_set: KeySet | None = None) -> VideoCallers:
    """Callers that verify against `key_set` on the real clocks."""
    return VideoCallers.from_config(
        _caller_config(),
        fetch_key_set=key_set or KeySet(),
        wall_clock=time.time,
        monotonic_clock=time.monotonic,
    )


def _sign(claims: dict[str, object]) -> str:
    return jwt.encode(
        claims, _key().private_pem, ALGORITHM, headers={"kid": KEY_IDENTIFIER}
    )


def workload_token(clock: Callable[[], float] = time.time, **claims: object) -> str:
    """The app's workload token, with `claims` set over the valid ones."""
    issued = int(clock())
    return _sign(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "azp": CLIENT,
            "client_id": CLIENT,
            "iat": issued,
            "exp": issued + _LIFETIME_SECONDS,
            "sub": "quiz-service-account",
            "realm_access": {"roles": [WORKLOAD_ROLE]},
        }
        | claims
    )


def person_token(
    clock: Callable[[], float] = time.time,
    groups: tuple[str, ...] = (f"/apps/{APP}", SERVICE_GROUP),
    **claims: object,
) -> str:
    """The person's token, in `groups`, with `claims` set over the valid ones."""
    issued = int(clock())
    return _sign(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "azp": CLIENT,
            "iat": issued,
            "exp": issued + _LIFETIME_SECONDS,
            "sub": "quiz-player",
            GROUP_CLAIM: list(groups),
        }
        | claims
    )


def valid_headers() -> dict[str, str]:
    """The headers of a session upgrade the scenario issuer vouches for."""
    return caller_headers(workload_token=workload_token(), person_token=person_token())


def headers_of(
    *, app: str = APP, client: str = CLIENT, subject: str = "quiz-player"
) -> dict[str, str]:
    """The headers of a call by `subject` through the registered `client` of `app`."""
    return caller_headers(
        workload_token=workload_token(azp=client, client_id=client),
        person_token=person_token(
            groups=(f"/apps/{app}", SERVICE_GROUP), azp=client, sub=subject
        ),
    )


def caller_config_text(*, identity: str = "", apps: str | None = None) -> str:
    """The TOML file that trusts the scenario issuer, as the service reads it.

    `identity` adds lines to the identity table; `apps` replaces the app tables.
    """
    registered = (
        f"[apps.{APP}]\nworkload_client = {json.dumps(CLIENT)}\n"
        if apps is None
        else apps
    )
    return "\n".join(
        [
            "[identity]",
            f"issuer = {json.dumps(ISSUER)}",
            f"jwks_url = {json.dumps(KEY_SET_ADDRESS)}",
            f"audience = {json.dumps(AUDIENCE)}",
            f"workload_role = {json.dumps(WORKLOAD_ROLE)}",
            f"group_claim = {json.dumps(GROUP_CLAIM)}",
            f"service_group = {json.dumps(SERVICE_GROUP)}",
            identity,
            "",
            registered,
        ]
    )


def write_caller_config(directory: Path, text: str | None = None) -> Path:
    """Write the caller configuration (by default the issuer's) under `directory`."""
    path = directory / "caller-config.toml"
    path.write_text(caller_config_text() if text is None else text, encoding="utf-8")
    return path
