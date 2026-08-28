"""Where a guild publishes its protocols, and what it published there.

`DocumentSink` was already the right port -- `create(title, body, target)`,
with `target` a parameter because it is per-guild. What was missing is the
per-guild *configuration* that says which sink, with which credential,
against which space. That configuration is not a `guild_config` key for
two reasons: it has structure, and the settings API renders every
`guild_config` value straight back to whichever administrator asks for it.
A Confluence token must not be renderable.

The read models here are the shape a caller outside the database sees, and
the notable thing about `ExportTarget` is what it does not carry. The
secret is not an optional field a careful caller leaves alone; it is
absent, and `has_secret` is all that is left of it. A read model that
could hold the token is a read model that will eventually be serialised
into a response by somebody who did not know that.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ExportTarget:
    """One destination one guild publishes to.

    `format` names the renderer/sink pair -- `outline`, `markdown`,
    `html`, `pdf`, `confluence` -- because a format is a *pair* and not
    just a sink: a PDF sink handed Outline-flavoured Markdown gets
    `mention://` chips as literal text. A plain string rather than an
    enum, for the reason `guild_channel.kind` is one: a format this
    deployment's code does not recognise must be a row a reader ignores,
    not a failed read that takes the guild's other destinations with it.

    `target` is what the sink addresses -- an Outline collection id, a
    Confluence space key, an object-store prefix -- and `config` is
    whatever else that format needs, which is why it is a mapping rather
    than five columns four formats would leave null.

    `enabled` is separate from existence because switching a destination
    off is not the same as forgetting how it was configured.
    """

    id: int
    guild_id: int
    format: str
    name: str
    target: str
    config: Mapping[str, Any]
    #: Whether a credential is stored, never the credential. See the
    #: module docstring: the absence is the design.
    has_secret: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SessionDocument:
    """One document one session produced, at one destination.

    A session has documents, plural, once a guild enables more than one
    destination: publishing writes to each and records each outcome, and
    one failing destination must not lose the others.
    `session.document_url` stays the primary -- it is what the
    announcement posts and what everything already reading a session
    reads -- and this is what the second, third and fourth get.

    `target_id` is `None` for a document whose destination has since been
    removed. Removing a destination is an administrator saying "stop
    publishing here", not "forget what was published": the document still
    exists in the other system, and the link is what somebody follows
    when they go looking for last quarter's minutes.

    `guild_id` is the session's guild, joined rather than stored on the
    row: `session_document` has no such column and does not need one,
    because a document's guild is its session's and cannot be anything
    else. It is on the read model because an object-store artefact is
    sealed under a key bound to that guild, and the reader has to supply
    that binding from its own context rather than from the object -- an
    envelope that carried the guild it was filed under would authenticate
    just as happily after being moved.
    """

    session_id: int
    guild_id: int
    target_id: int | None
    provider: str
    document_id: str
    url: str
    created_at: datetime
