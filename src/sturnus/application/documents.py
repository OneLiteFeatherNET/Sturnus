"""Rendering the target-neutral transcript model into a document body.

The template is the readable specification of the output (Spec 8.2/8.3):
which parts of a speaker's identity show up, and in what form, is a decision
the template makes -- this module only feeds it the data.

Dependency-rule note: `documents.py` lives in `application`, which must not
import from `infrastructure` (tests/test_architecture.py) -- not even the
sandboxed Jinja2 environment that ships in `sturnus.infrastructure.templates`
for the other adapters. This module therefore builds its own sandboxed
environment directly from Jinja2 (a third-party dependency, not a project
layer -- importing it here does not violate the rule). `render_transcript`
still takes the template source as a plain string, exactly as specified, so
callers outside `application` are free to load it from anywhere, including
the packaged template shipped in `sturnus.infrastructure.documents`.

`escape_markdown` below is the single definition of which characters are
dangerous at this boundary. It lives here, in `application`, rather than in
`infrastructure` or being duplicated per adapter, because the layering
allows `infrastructure` to import from `application` but not the reverse
(the same resolution `sturnus.application.recording.audio_key` uses, which
`sturnus.infrastructure.objectstore` imports). `sturnus.infrastructure.
templates.markdown` re-exports it for the other Markdown-producing adapters,
so there is exactly one character set for this rule anywhere in the
codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo
from typing import Protocol, TypedDict

from jinja2.sandbox import SandboxedEnvironment

from sturnus.domain.transcript import SpeakerIdentity, Transcript

_MARKDOWN_SPECIAL = "\\`*_{}[]()#+-.!|>~"


def escape_markdown(value: str) -> str:
    """Neutralises Markdown metacharacters in attacker-controlled text.

    Discord display names and transcript text are not trustworthy: someone
    calling themselves `[click here](https://...)` would otherwise place a
    link into every protocol they appear in. This is the single definition
    of the escaped character set; `sturnus.infrastructure.templates.markdown`
    imports it rather than keeping its own copy.
    """
    return "".join("\\" + ch if ch in _MARKDOWN_SPECIAL else ch for ch in value)


def _build_environment() -> SandboxedEnvironment:
    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    # autoescape is off deliberately: the output is Markdown, not HTML, and
    # HTML escaping would corrupt it. Escaping is explicit through the `md`
    # filter instead, applied to every value drawn from the transcript.
    env.filters["md"] = escape_markdown
    return env


def _build_html_environment() -> SandboxedEnvironment:
    """The same engine with the opposite escaping, for an HTML destination.

    Two things here are the whole point of having a second environment
    rather than a second template on the first one.

    `autoescape=True`: every value drawn from the transcript is escaped as
    HTML on its way into the output, so a display name of
    `<script>alert(1)</script>` reaches the page as text.

    **No `md` filter.** It is not merely unused here -- it is absent, so a
    template that reaches for it fails to render rather than quietly
    emitting `escape_markdown`'s backslashes into a document where nothing
    ever removes them again. The registry
    (`sturnus.application.export_formats`) is what pairs each template with
    the environment that is right for it; this is the half of that pairing
    that cannot be got wrong silently.
    """
    return SandboxedEnvironment(autoescape=True, trim_blocks=True, lstrip_blocks=True)


class _BlockContext(TypedDict):
    time: str
    speaker: SpeakerIdentity
    text: str


@dataclass(frozen=True)
class CreatedDocument:
    id: str
    url: str


class DocumentSink(Protocol):
    """A destination a rendered transcript can be written to (Spec 8.1).

    `target` names where within the destination the document goes -- an
    Outline collection id, a future Confluence adapter's space key, a file
    storage path; its interpretation is up to the adapter (Spec 11's
    `document_target`). It is a parameter to `create`, not configuration
    fixed on the adapter at construction: `document_target` is per-guild,
    while one process (the worker) serves every guild, so which target
    applies is only known once a specific session -- and therefore its
    guild -- is in hand, at document-creation time.
    """

    async def create(self, title: str, body: str, target: str) -> CreatedDocument: ...


@dataclass(frozen=True)
class ChannelRef:
    """Where a meeting was held, as the protocol names it.

    Carried separately from `Transcript` because it is about the meeting
    rather than about what was said, and because the name is a snapshot: the
    bot records it when the session opens, so a channel renamed afterwards
    does not rewrite the protocols of meetings held under the old name.
    """

    guild_id: int
    channel_id: int
    name: str | None = None

    @property
    def url(self) -> str:
        """The Discord deep link that opens this channel."""
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}"

    @property
    def label(self) -> str:
        """What to show: the name when known, the id when it is not."""
        return self.name if self.name else str(self.channel_id)


def transcript_context(
    transcript: Transcript,
    tz: tzinfo,
    channel: ChannelRef | None = None,
) -> dict[str, object]:
    """Everything a document template may read, localised to `tz`.

    Extracted from `render_transcript` when a second output *shape* -- HTML
    -- arrived. The two differ in exactly one thing, the escaping, and
    nothing else about a protocol changes with its destination: the same
    participants, the same blocks, the same local times. Building the
    context once is what keeps that true, rather than leaving a second copy
    to drift into rendering a different meeting.
    """
    blocks: list[_BlockContext] = [
        {
            "time": block.start.astimezone(tz).strftime("%H:%M:%S"),
            "speaker": block.speaker,
            "text": block.text,
        }
        for block in transcript.blocks
    ]
    local_start = transcript.session_started_at.astimezone(tz)
    duration = transcript.session_ended_at - transcript.session_started_at
    return {
        "participants": transcript.participants,
        "blocks": blocks,
        "channel": channel,
        # Outline renders `mention://date/<YYYY-MM-DD>` as a date chip
        # (MentionType.Date). Date only: the version deployed here parses no
        # time component, so an ISO datetime would degrade to a plain link.
        "date_iso": local_start.strftime("%Y-%m-%d"),
        "date_label": local_start.strftime("%d.%m.%Y"),
        "started": local_start.strftime("%H:%M"),
        "duration_minutes": max(1, round(duration.total_seconds() / 60)),
        # Only a standalone document needs its own title inside its body;
        # the Outline template does not read this, because Outline stores
        # the title as a field of its own. It is in the context rather than
        # a parameter of the HTML renderer alone so there is one definition
        # of what a protocol is called (`document_title`) and one context
        # every template is rendered against.
        "title": document_title(transcript, tz),
    }


def render_transcript(
    transcript: Transcript,
    template_source: str,
    tz: tzinfo,
    channel: ChannelRef | None = None,
) -> str:
    """Renders `transcript` through `template_source` as Markdown.

    The timezone is a parameter rather than a constant: a protocol read by
    people in one place should carry local times, and hardcoding UTC would
    make every timestamp subtly wrong for its readers -- wrong in a way that
    does not look wrong, since any hour reads as a plausible meeting time.

    `channel` is optional so a caller that cannot resolve it still gets a
    protocol; the template simply omits the heading.

    **Markdown, and only Markdown.** The environment escapes through the
    `md` filter and autoescapes nothing, so handing this function an HTML
    template would produce a page that renders a hostile display name as
    markup. `render_html` is the other half of that pair; which one a
    destination gets is `sturnus.application.export_formats`' decision.
    """
    template = _build_environment().from_string(template_source)
    return template.render(**transcript_context(transcript, tz, channel))


def render_html(
    transcript: Transcript,
    template_source: str,
    tz: tzinfo,
    channel: ChannelRef | None = None,
) -> str:
    """The same protocol, rendered as HTML with HTML escaping.

    Deliberately not `render_transcript` with a different template.
    `escape_markdown` answers `<script>` with a backslash in front of
    nothing -- every character of the tag survives -- so a Markdown-escaped
    HTML document is an XSS sink that looks escaped. The escaping is a
    property of the *environment*, and that is what differs here; see
    `_build_html_environment`.
    """
    template = _build_html_environment().from_string(template_source)
    return template.render(**transcript_context(transcript, tz, channel))


def document_title(transcript: Transcript, tz: tzinfo) -> str:
    """Builds the Outline document title from the session's start (Spec 8.3)."""
    local_start = transcript.session_started_at.astimezone(tz)
    return f"Voice session {local_start.strftime('%Y-%m-%d %H:%M')}"
