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

    Knows nothing of collections, spaces, or file paths -- those concepts
    live in the configuration of whichever adapter implements this port.
    """

    async def create(self, title: str, body: str) -> CreatedDocument: ...


def render_transcript(transcript: Transcript, template_source: str, tz: tzinfo) -> str:
    """Renders `transcript` through `template_source`, localised to `tz`.

    The timezone is a parameter rather than a constant: a protocol read by
    people in one place should carry local times, and hardcoding UTC would
    make every timestamp subtly wrong for its readers.
    """
    blocks: list[_BlockContext] = [
        {
            "time": block.start.astimezone(tz).strftime("%H:%M:%S"),
            "speaker": block.speaker,
            "text": block.text,
        }
        for block in transcript.blocks
    ]
    template = _build_environment().from_string(template_source)
    return template.render(participants=transcript.participants, blocks=blocks)


def document_title(transcript: Transcript, tz: tzinfo) -> str:
    """Builds the Outline document title from the session's start (Spec 8.3)."""
    local_start = transcript.session_started_at.astimezone(tz)
    return f"Voice session {local_start.strftime('%Y-%m-%d %H:%M')}"
