"""Who may open a session: the proof of the two tokens of a request.

webauth proves the tokens (`ServiceCallerCheck.person_caller`); this module adds the
policy that is the service's: only a person call is admitted, and the person must
belong to exactly the configured service group. It logs nothing, and nothing taken
from a token leaves it but the `Opener` the session keeps to know its viewers' caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Final

from webauth.admission import AdmissionConfig, GroupPath, NoAdmins
from webauth.service_callers import (
    CallerRefusalClass,
    PersonCaller,
    RegisteredApp,
    ServiceCallerCheck,
    ServiceIdentity,
)

from video_client.wire import (
    AUTHORIZATION_HEADER,
    WORKLOAD_TOKEN_HEADER,
    RefusalCode,
)
from video_service.runtime import in_daemon_thread

if TYPE_CHECKING:
    from starlette.datastructures import Headers
    from webauth.jwt_verification import KeySetFetch, MonotonicClock
    from webauth.ports import WallClock

    from video_service.config import CallerConfig

_BEARER_SCHEME: Final = "bearer"
_REFUSAL_CODE: Final = {
    CallerRefusalClass.UNAUTHENTICATED: RefusalCode.UNAUTHORIZED,
    CallerRefusalClass.UNAVAILABLE: RefusalCode.UNAVAILABLE,
    CallerRefusalClass.FORBIDDEN: RefusalCode.FORBIDDEN,
}


@dataclass(frozen=True, slots=True)
class Opener:
    """The caller of a session: the app's client and the person's subject."""

    client_id: str = field(repr=False)
    subject: str = field(repr=False)


class VideoCallers:
    """The one check of a process, over the configured issuer and apps."""

    def __init__(self, check: ServiceCallerCheck, service_group: GroupPath) -> None:
        """Hold `check` and the person's group."""
        self._check = check
        self._service_group = str(service_group)

    @classmethod
    def from_config(
        cls,
        config: CallerConfig,
        *,
        fetch_key_set: KeySetFetch,
        wall_clock: WallClock,
        monotonic_clock: MonotonicClock,
    ) -> VideoCallers:
        """Build the check from the configuration and the clocks and fetch it uses."""
        identity = config.identity
        registered = {
            app.workload_client: RegisteredApp(
                app=name,
                admission=AdmissionConfig(
                    bound_issuer=identity.issuer,
                    login_group=config.login_groups[name],
                    admin_group=NoAdmins.IN_THIS_APP,
                ),
            )
            for name, app in config.apps.items()
        }
        check = ServiceCallerCheck(
            ServiceIdentity(
                issuer=identity.issuer,
                audience=identity.audience,
                jwks_url=identity.jwks_url,
                workload_role=identity.workload_role,
                group_claim=identity.group_claim,
                leeway_seconds=0,  # the service accepts no clock skew
            ),
            registered_apps=registered,
            fetch_key_set=fetch_key_set,
            wall_clock=wall_clock,
            monotonic_clock=monotonic_clock,
        )
        return cls(check, identity.service_group)

    async def admit(self, headers: Headers) -> Opener | RefusalCode:
        """The caller of a person call, or the refusal the request's tokens earn."""
        # A daemon thread, since a key-set fetch can outlast the drain.
        caller = await asyncio.wrap_future(
            in_daemon_thread(
                partial(
                    self._check.person_caller,
                    headers.get(WORKLOAD_TOKEN_HEADER),
                    _bearer_token(headers.get(AUTHORIZATION_HEADER)),
                ),
                "callers",
            )
        )
        if not isinstance(caller, PersonCaller):
            return _REFUSAL_CODE[caller.refusal_class]
        if self._service_group not in caller.groups:
            return RefusalCode.FORBIDDEN
        return Opener(caller.client_id, caller.principal.subject)


def _bearer_token(authorization: str | None) -> str | None:
    """The token of a `Bearer` authorization; any other scheme counts as none."""
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    return token if scheme.lower() == _BEARER_SCHEME else None
