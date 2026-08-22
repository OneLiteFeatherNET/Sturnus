"""The signed cookie that is the console's entire notion of a session.

There is no server-side session store. The cookie *is* the session, which
buys a stateless API -- a restart signs nobody out, a second replica needs
no shared state, and there is no table to expire -- and costs exactly one
thing: the signature has to be right.

The threat is not subtle. A forgeable cookie is an unauthenticated person
choosing whose recordings to listen to.

Three decisions carry that weight:

- **The expiry is inside the signed payload**, not beside it. A lifetime
  that lived only in the browser's `Max-Age` would be extended by anyone
  who kept the value, and a stolen session would then never end.
- **Comparison is constant-time.** A signature check that returns early on
  the first wrong byte leaks, over enough attempts, which byte was wrong.
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

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: HMAC-SHA256 produces 32 bytes; a key shorter than its output adds no
#: strength and signals a placeholder that was never replaced. Refusing at
#: construction makes a misconfigured deployment fail at startup rather
#: than serve forgeable sessions.
_MINIMUM_SECRET_BYTES = 32


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


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    # Padding is stripped on the way out so the cookie carries no `=`,
    # which some proxies and clients handle inconsistently in cookie
    # values. It is restored here rather than being made the caller's
    # problem.
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


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
        self._secret = secret.encode("utf-8")
        self._lifetime = lifetime

    def issue(self, session: SignedSession, now: datetime) -> str:
        """Signs one session, valid from `now` for this signer's lifetime.

        Deterministic: the same session at the same instant produces the
        same token. There is no nonce, because there is nothing for one to
        do here -- and because a random component would hide a signature
        that ignored its payload from every test that could catch it.
        """
        payload = _b64encode(
            json.dumps(
                {
                    "sub": session.discord_user_id,
                    "exp": int((now + self._lifetime).timestamp()),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        return f"{payload}.{self._sign(payload)}"

    def read(self, token: str, now: datetime) -> SignedSession:
        """Verifies a token and returns who it names.

        Raises `InvalidSession` for anything that is not a token this
        signer issued -- including every malformed shape, which all come
        back as the same refusal rather than as a parse error that would
        tell a prober which step it reached.
        """
        payload, separator, signature = token.partition(".")
        if not separator or not payload or not signature:
            raise InvalidSession("not a session token")
        # Verified before the payload is parsed, let alone trusted. Parsing
        # first would run a JSON decoder over unauthenticated bytes for no
        # reason.
        if not hmac.compare_digest(self._sign(payload), signature):
            raise InvalidSession("signature does not match")

        try:
            claims = json.loads(_b64decode(payload))
            discord_user_id = int(claims["sub"])
            expires_at = datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
        except (ValueError, KeyError, TypeError) as exc:
            # Reachable only for a payload this signer signed, so this is a
            # bug in issuing rather than an attack -- but it is still not
            # something to raise a decoder's own exception out of.
            raise InvalidSession("session payload is malformed") from exc

        if now >= expires_at:
            raise ExpiredSession("session has expired")
        return SignedSession(discord_user_id=discord_user_id)

    def _sign(self, payload: str) -> str:
        return _b64encode(hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest())
