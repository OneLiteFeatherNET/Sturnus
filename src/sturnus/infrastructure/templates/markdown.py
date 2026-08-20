"""Escaping for values interpolated into Markdown.

Discord display names and transcript text are not trustworthy. Someone
calling themselves `[click here](https://…)` would otherwise place a link
into every protocol they appear in, and a name containing `](` can close
the surrounding construct and start something else.

The escaping rule itself is defined once, in `sturnus.application.documents`,
and re-exported here rather than duplicated: the dependency rule lets
`infrastructure` import from `application` (never the reverse), which is the
same resolution `sturnus.infrastructure.objectstore` uses for
`sturnus.application.recording.audio_key`. Keeping one definition means a
character added to the dangerous set here and in the `application`-layer
renderer can never silently drift apart.
"""

from __future__ import annotations

from sturnus.application.documents import escape_markdown

__all__ = ["escape_markdown"]
