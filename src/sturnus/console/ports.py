"""What the console's API needs from the world, as narrow protocols.

Each of these is satisfied by an adapter wired in by
`sturnus.entrypoints.api`, and by a fake in the tests. They are declared
here rather than imported from the concrete classes so this package
depends on shapes rather than on `sturnus.infrastructure` -- the same rule
`sturnus.application` follows, for the same reason: a console module that
imports an adapter is a console module that cannot be tested without one.

They are also narrow on purpose. `LinkDirectory` exposes one method, not
the whole of `AccountLinkRepository`, because one method is what the login
flow uses -- and a protocol that offers more than its consumer needs is an
invitation for the next handler to reach for something it should not have.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity


class OAuthClient(Protocol):
    """The identity provider the console authenticates against."""

    def authorize_url(self, state: str) -> str: ...

    async def identity_from_code(self, code: str) -> ExternalIdentity: ...


class StateStore(Protocol):
    """Single-use OAuth states, tying a callback to a login this server began."""

    async def issue(self, state: str, now: datetime) -> None: ...

    #: `False` for a state that was never issued, has already been used, or
    #: has expired -- the caller treats all three identically, because from
    #: the outside they are the same event: this is not a callback for a
    #: login we started.
    async def consume(self, state: str, now: datetime) -> bool: ...


class LinkDirectory(Protocol):
    """The bridge from an external identity to the Discord user it belongs to.

    This is the whole authorisation model: every console query is scoped by
    Discord id, because that is what `session_participant` names, and the
    only bridge to one is a link the person made themselves with `/link`.
    """

    async def discord_user_for(self, provider: str, external_user_id: str) -> int | None: ...


class AdminDirectory(Protocol):
    """Whether somebody administers any guild the bot serves."""

    async def is_admin_anywhere(self, discord_user_id: int) -> bool: ...


@dataclass(frozen=True)
class Track:
    """Where one speaker's recording is, and what unlocks it.

    Deliberately not the whole `transcription_job` row. What the audio
    endpoint needs is the object key and the wrapped key; the transcript,
    the status and the attempt count are somebody else's business, and a
    value that carried them would put a transcript one attribute access
    away from a response body.
    """

    s3_key: str
    encryption_key_id: str
    wrapped_data_key: bytes


class TrackDirectory(Protocol):
    """One speaker's recording, if the person asking is allowed to hear it.

    `requested_by` is not an afterthought and not optional: the whole
    authorisation rule for audio lives inside this one call. An
    implementation answers `None` both for "there is no such track" and for
    "you were not in that session", because from outside they must look the
    same -- the existence of a session somebody was not in is not
    information they are owed.

    Consequently there is no `track` method without a `requested_by`, and
    no way to filter afterwards in a handler. A filter that can be
    forgotten is a filter that will be.
    """

    async def track_for(
        self, session_id: int, speaker_id: int, *, requested_by: int
    ) -> Track | None: ...


class KeyUnwrapper(Protocol):
    """Unwraps a recording's data key with the process's master key.

    `sturnus.infrastructure.crypto.KeyWrapper` satisfies this. `key_id`
    is here because a recording names the master key that wrapped it, and
    a mismatch is a configuration error worth reporting as one rather than
    an authentication-tag failure three layers down.
    """

    key_id: str

    def unwrap(self, wrapped: bytes) -> bytes: ...


class EncryptedAudioSource(Protocol):
    """The object store, read by byte range.

    Three methods because the audio endpoint makes three different kinds of
    request and no more: how big the object is (to declare the track's
    length), the fixed-size file header (to get the nonce prefix), and the
    body from a chosen chunk boundary onwards.

    `stream` is an async *generator* rather than a plain iterator so the
    handler can close it: a listener who stops playing halfway through
    should stop the transfer from S3 in the same breath, and a suspended
    generator nobody closed holds the connection until the loop gets round
    to finalising it.

    A key that is not in the store raises `KeyError` -- the ordinary case
    is a recording the retention sweep already erased while its row lives
    on, which is a 404 and not an error.
    """

    async def size(self, key: str) -> int: ...

    async def read(self, key: str, start: int, length: int) -> bytes: ...

    def stream(self, key: str, start: int) -> AsyncGenerator[bytes, None]: ...
