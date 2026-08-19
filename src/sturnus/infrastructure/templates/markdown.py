"""Escaping for values interpolated into Markdown.

Discord display names and transcript text are not trustworthy. Someone
calling themselves `[click here](https://…)` would otherwise place a link
into every protocol they appear in, and a name containing `](` can close
the surrounding construct and start something else.
"""

from __future__ import annotations

_SPECIAL = "\\`*_{}[]()#+-.!|>~"


def escape_markdown(value: str) -> str:
    return "".join("\\" + ch if ch in _SPECIAL else ch for ch in value)
