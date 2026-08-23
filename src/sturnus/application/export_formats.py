"""The registry of publishable formats: a renderer paired with a sink.

**Why a registry, and why the pair.** `document_provider` was read on every
publish and selected nothing: one code path constructed `OutlineSink`
unconditionally, so a guild could name a provider and get Outline anyway.
The obvious fix -- a mapping from provider to sink -- is only half of one.
`sturnus.application.documents.render_transcript` with the packaged template
emits Outline's own `mention://` chips, and every value in it has been
through `escape_markdown`. Hand that string to an HTML sink and the browser
shows the mention scheme as literal text next to somebody's name, with
backslashes in front of every full stop; hand it to a PDF renderer and the
same thing arrives on paper. A destination therefore does not merely receive
a document, it decides what the document *is*.

So a format is a pair, and this module is where the two halves are tied
together. `FORMATS` is the whole of the dispatch: adding `pdf` is adding an
entry with a renderer and a sink family, not editing a branch in the worker,
in the console API and in the wiring. Nothing outside this module may
enumerate formats by writing their names -- `supported_formats` and
`format_named` are how that question is asked.

**Which sink, resolved one layer out.** An entry names a *sink family*
(`OUTLINE_SINK`, `OBJECT_STORE_SINK`) rather than holding a sink object,
because a sink is an adapter -- an HTTP client, an S3 client -- and this
module lives in `sturnus.application`, which must never import
`sturnus.infrastructure` (tests/test_architecture.py). The infrastructure
side (`sturnus.infrastructure.documents.sinks.DocumentSinks`) has one branch
per *family*, not one per format, which is why `pdf` can be added here with
no wiring change at all: it is an object-store artefact like the two that
already exist.

**`pdf` and `confluence` are specified and deliberately not built** (spec
§3.4). Every route to a PDF is a large native dependency in an image that
today holds Python and a Whisper model, and that is a decision about attack
surface and image size which the specification does not pre-empt. They are
absent from `FORMATS` rather than present and inert: a target configured for
a format nothing can render must be refused where an administrator can read
the refusal, not accepted and then silently skipped after every meeting.
When either is built it belongs behind this registry and needs no other
change.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import tzinfo
from typing import Final

from sturnus.application.documents import ChannelRef, render_html, render_transcript
from sturnus.domain.transcript import Transcript

# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

#: The format names. Literals from this source, which is what makes them
#: safe to log and safe as `session_document.provider`.
OUTLINE: Final = "outline"
MARKDOWN: Final = "markdown"
HTML: Final = "html"

#: The sink families. A family is "what kind of thing carries the bytes",
#: not "which format" -- three formats share two families today, and `pdf`
#: would join the second without adding a third.
OUTLINE_SINK: Final = "outline"
OBJECT_STORE_SINK: Final = "object_store"


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """Everything any renderer needs, so that they all take the same thing.

    `outline_template` is the packaged Outline template the worker loads
    from disk (`sturnus.entrypoints.worker._load_template`). It is a
    parameter rather than a constant here for the reason it always was:
    that template is a shipped resource in `sturnus.infrastructure`, which
    this module may not import. The other two formats carry their template
    below, because those templates are new, small, and have no reason to
    live anywhere a test cannot reach without a package resource.
    """

    transcript: Transcript
    tz: tzinfo
    channel: ChannelRef | None
    outline_template: str


Renderer = Callable[[RenderRequest], str]


@dataclass(frozen=True, slots=True)
class ExportFormat:
    """One format: how a protocol is rendered, and what carries it away.

    `media_type` and `file_extension` are read only by the object-store
    family, which has to name the bytes it stores and the object it stores
    them in. They are on every entry regardless, because "what kind of
    document is this" is a property of the format rather than of the sink
    that happens to take it -- `session_document.provider` records the
    format name, and the console route that serves an artefact back reads
    its media type from here rather than guessing from the key.
    """

    name: str
    render: Renderer
    sink: str
    media_type: str
    file_extension: str
    #: What this format's `target` column may say. A regex per entry rather
    #: than a check per sink family in the API, so that adding a format
    #: stays adding an entry. An Outline target is a collection id; an
    #: object-store target becomes part of an object key, and `..` in one
    #: is not a traversal in S3 but it is a key nobody meant to write.
    target_pattern: re.Pattern[str]

    def accepts_target(self, target: str) -> bool:
        """Whether `target` is something this format can address at all."""
        return bool(self.target_pattern.fullmatch(target))


# ---------------------------------------------------------------------------
# The templates that are not packaged resources
# ---------------------------------------------------------------------------

#: Plain CommonMark: no `mention://`, no Outline date chip, and a speaker
#: rendered as the name a reader would say out loud. It is a module
#: constant for the same reason `sturnus.application.publishing.
#: DEFAULT_ANNOUNCEMENT_TEMPLATE` is one -- the text is short, it is part
#: of the behaviour under test, and reaching it through
#: `importlib.resources` would put it out of reach of every test that does
#: not want a packaged file.
#:
#: Escaped through the `md` filter exactly as the Outline template is. A
#: display name is attacker-controlled text on its way into a Markdown
#: document whatever reads that document afterwards, and "plain" describes
#: the syntax, never the trust.
MARKDOWN_TEMPLATE: Final = """\
{% macro speaker(s) -%}
{{ (s.external_display_name or s.discord_display_name) | md }}
{%- endmacro %}
{% if channel %}**Channel:** [{{ channel.label | md }}]({{ channel.url }})

{% endif %}\
**Date:** {{ date_label }} · {{ started }}, {{ duration_minutes }} min · \
{{ participants | length }} participant{{ '' if participants | length == 1 else 's' }}

## Participants

{% for s in participants %}
- {{ speaker(s) }}
{% endfor %}

## Transcript

{% for block in blocks %}
**{{ block.time }}** · {{ speaker(block.speaker) }}

{{ block.text | md }}

{% endfor %}"""

#: A whole HTML document, because this one is served on its own from an
#: object store rather than embedded in a page somebody else wrote: it
#: needs its own `<!doctype>`, its own charset and its own title or a
#: browser guesses all three.
#:
#: **Nothing here is passed through `md`, and the filter does not exist in
#: the environment this renders in** (`sturnus.application.documents.
#: _build_html_environment`). Escaping is the environment's, and it is HTML
#: escaping; a `| md` in this template is a render-time error rather than a
#: page full of stray backslashes.
#:
#: Deliberately no external stylesheet, no script and no image: this
#: document is read by whoever the console lets read it, and a protocol
#: that fetches anything is a protocol that reports when it was read and to
#: where.
HTML_TEMPLATE: Final = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 48rem; \
line-height: 1.5; }
.block { margin: 1.25rem 0; }
.attribution { color: #444; font-size: 0.9rem; }
.time { font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<h1>{{ title }}</h1>
{% macro speaker(s) -%}
{{ s.external_display_name or s.discord_display_name }}
{%- endmacro %}
<p>
{% if channel %}<strong>Channel:</strong> <a href="{{ channel.url }}">{{ channel.label }}</a><br>
{% endif %}\
<strong>Date:</strong> {{ date_label }} · {{ started }}, {{ duration_minutes }} min · \
{{ participants | length }} participant{{ '' if participants | length == 1 else 's' }}
</p>
<h2>Participants</h2>
<ul>
{% for s in participants %}
<li>{{ speaker(s) }}</li>
{% endfor %}
</ul>
<h2>Transcript</h2>
{% for block in blocks %}
<div class="block">
<p class="attribution"><span class="time">{{ block.time }}</span> · \
<strong>{{ speaker(block.speaker) }}</strong></p>
<p>{{ block.text }}</p>
</div>
{% endfor %}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# The renderers
# ---------------------------------------------------------------------------


def _render_outline(request: RenderRequest) -> str:
    """Today's behaviour, unchanged and reached the same way it always was."""
    return render_transcript(
        request.transcript, request.outline_template, request.tz, request.channel
    )


def _render_markdown(request: RenderRequest) -> str:
    return render_transcript(request.transcript, MARKDOWN_TEMPLATE, request.tz, request.channel)


def _render_html(request: RenderRequest) -> str:
    return render_html(request.transcript, HTML_TEMPLATE, request.tz, request.channel)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: An Outline collection id. Broad rather than a UUID pattern: the id is
#: whatever Outline issues, and a deployment that starts issuing a
#: different shape must not need a release here to stay configurable. What
#: it excludes is whitespace and the empty string -- a target that is blank
#: or that has a space in it is a paste that went wrong, and the failure it
#: produces otherwise is an unexplained 404 from somebody else's API.
_OUTLINE_TARGET = re.compile(r"[^\s/][^\s]*")

#: An object-store prefix. Alphanumerics, dot, dash, underscore and `/` as
#: a separator; no leading slash, and `..` cannot appear because a dot may
#: not follow a dot.
_OBJECT_PREFIX = re.compile(r"[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*")

FORMATS: Final[Mapping[str, ExportFormat]] = {
    entry.name: entry
    for entry in (
        ExportFormat(
            name=OUTLINE,
            render=_render_outline,
            sink=OUTLINE_SINK,
            # Outline stores Markdown and renders it itself; nothing reads
            # this for the Outline family, and a wrong value here would be
            # invisible. It is filled in honestly all the same.
            media_type="text/markdown; charset=utf-8",
            file_extension="md",
            target_pattern=_OUTLINE_TARGET,
        ),
        ExportFormat(
            name=MARKDOWN,
            render=_render_markdown,
            sink=OBJECT_STORE_SINK,
            media_type="text/markdown; charset=utf-8",
            file_extension="md",
            target_pattern=_OBJECT_PREFIX,
        ),
        ExportFormat(
            name=HTML,
            render=_render_html,
            sink=OBJECT_STORE_SINK,
            media_type="text/html; charset=utf-8",
            file_extension="html",
            target_pattern=_OBJECT_PREFIX,
        ),
    )
}


def format_named(name: str) -> ExportFormat | None:
    """The entry for `name`, or `None` for a format this code cannot publish.

    `None` rather than a raise, deliberately. `guild_export_target.format`
    is a plain string precisely so that a row naming something this
    deployment does not implement is a row a reader *ignores* -- see
    `sturnus.domain.exports.ExportTarget`. A raise here would let one such
    row take the guild's other destinations down with it, which is the
    failure the column's type was chosen to prevent.
    """
    return FORMATS.get(name)


def supported_formats() -> tuple[str, ...]:
    """Every format this deployment can publish, in registry order.

    The one place outside this module's own entries where the set of names
    is available. The console API answers a refused `format` with it, so an
    administrator who typed `pdf` is told what they may type instead rather
    than being left to guess.
    """
    return tuple(FORMATS)
