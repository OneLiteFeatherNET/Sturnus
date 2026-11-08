"""The signed cookie that is the console's entire notion of a session.

There is no server-side session store. The cookie *is* the session, which
buys a stateless API -- a restart signs nobody out, a second replica needs
no shared state, and there is no table to expire -- and costs exactly one
thing: the signature has to be right. The signature is `itsdangerous`'s
timed serializer, the library under Flask's session cookies, and it is not
written here.

The threat is not subtle. A forgeable cookie is an unauthenticated person
choosing whose recordings to listen to.

Three decisions carry that weight:

- **The lifetime is bound to the signed timestamp**, not beside it. The
  issue time is inside the signature, so a holder cannot move it forward.
  A lifetime that lived only in the browser's `Max-Age` would be extended
  by anyone who kept the value, and a stolen session would then never end.
- **Signatures are compared in constant time**, by the library.
- **Expiry and forgery are different exceptions**, because they need
  different responses: an expired session is sent back through the login
  flow, a forged one is refused. Collapsing them would also make an
  ordinary user's expiry indistinguishable from an attack in the logs.

The format is deliberately not JWT. A JWT carries its own algorithm in a
header the verifier is then invited to trust, which is a family of
vulnerabilities this has no use for -- there is one algorithm here, it is
not negotiable, and it is not written down anywhere the holder can reach.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from itsdangerous import BadData, SignatureExpired, TimestampSigner, URLSafeTimedSerializer

#: HMAC-SHA256 produces 32 bytes; a key shorter than its output adds no
#: strength and signals a placeholder that was never replaced. Refusing at
#: construction makes a misconfigured deployment fail at startup rather
#: than serve forgeable sessions.
_MINIMUM_SECRET_BYTES = 32

#: Names this use, so a token signed with the same secret for another
#: purpose is not accepted here.
_SALT = "sturnus.console.session"


class InvalidSession(Exception):
    """The token was not issued by this signer, or is not a token at all."""


class ExpiredSession(Exception):
    """The token was genuine and its lifetime has passed.

    Deliberately not a subclass of `InvalidSession`: see the module
    docstring on why the two must stay distinguishable.
    """


@dataclass(frozen=True)
class SignedSession:
    """Who the holder of a valid token is.

    One field, and it is the Discord user id rather than the Outline
    identity that authenticated: every query in the console is scoped by
    Discord id, because that is what `session_participant` names. The
    bridge between the two is made once, at login, against `account_link`.
    """

    discord_user_id: int


class _SignerAt(TimestampSigner):
    """A timestamp signer that reads the instant from its caller.

    The library reads the system clock by default. Here the instant is the
    `now` each call receives, so tests and the request path agree on time
    without any shared clock state.
    """

    def __init__(self, *args: Any, instant: datetime, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._instant = instant

    def get_timestamp(self) -> int:
        return int(self._instant.timestamp())


class SessionCookie:
    """Issues and verifies the console's session tokens.

    `lifetime` is a constructor argument rather than a module constant so
    the deployment decides it, and so a test can exercise expiry without
    waiting twelve hours.
    """

    def __init__(self, secret: str, lifetime: timedelta) -> None:
        if len(secret.encode("utf-8")) < _MINIMUM_SECRET_BYTES:
            raise ValueError(
                f"the session secret must be at least {_MINIMUM_SECRET_BYTES} bytes; "
                "a shorter one adds no strength over the hash it keys and is "
                "almost always a placeholder that was never replaced"
            )
        self._secret = secret
        self._lifetime = lifetime

    def _serializer(self, now: datetime) -> URLSafeTimedSerializer:
        # Built per call: constructing one is cheap, and it means the instant
        # is never held as state between calls.
        return URLSafeTimedSerializer(
            self._secret,
            salt=_SALT,
            signer=_SignerAt,
            signer_kwargs={
                "instant": now,
                "digest_method": hashlib.sha256,
                "key_derivation": "hmac",
            },
        )

    def issue(self, session: SignedSession, now: datetime) -> str:
        """Signs one session, valid from `now` for this signer's lifetime.

        Deterministic: the same session at the same instant produces the
        same token. There is no nonce, because there is nothing for one to
        do here -- and because a random component would hide a signature
        that ignored its payload from every test that could catch it.
        """
        return self._serializer(now).dumps({"sub": session.discord_user_id})

    def read(self, token: str, now: datetime) -> SignedSession:
        """Verifies a token and returns who it names.

        Raises `InvalidSession` for anything that is not a token this
        signer issued -- including every malformed shape, which all come
        back as the same refusal rather than as a parse error that would
        tell a prober which step it reached.
        """
        try:
            claims = self._serializer(now).loads(token, max_age=int(self._lifetime.total_seconds()))
        except SignatureExpired as exc:
            raise ExpiredSession("session has expired") from exc
        except BadData as exc:
            # BadData rather than BadSignature: a validly signed payload that
            # is not JSON raises BadPayload, which is not a BadSignature. It is
            # still not a session, so it is refused the same way.
            raise InvalidSession("not a session token this signer issued") from exc

        try:
            discord_user_id = int(claims["sub"])
        except (KeyError, TypeError, ValueError) as exc:
            # Reachable only for a payload this signer signed, so this is a
            # bug in issuing rather than an attack -- but it is still not
            # something to raise a KeyError out of.
            raise InvalidSession("session payload is malformed") from exc
        return SignedSession(discord_user_id=discord_user_id)
