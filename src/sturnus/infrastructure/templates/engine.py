"""The Jinja2 environment used for every rendered artefact.

Sandboxed from the outset. In this phase all templates ship inside the
image, so nothing untrusted is executed yet — but the moment templates
become settable through a command, an ordinary environment would be
arbitrary code execution in the bot's pod. Adding the sandbox now costs
nothing and means that later change does not land on a foundation that
cannot carry it.
"""

from __future__ import annotations

from jinja2.sandbox import SandboxedEnvironment

from sturnus.infrastructure.templates.markdown import escape_markdown


def build_environment() -> SandboxedEnvironment:
    env = SandboxedEnvironment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
    # autoescape is off deliberately: the output is Markdown, not HTML, and
    # HTML escaping would corrupt it. Escaping is explicit through this filter
    # instead, applied to every value that comes from outside.
    env.filters["md"] = escape_markdown
    return env


def render(template_source: str, **context: object) -> str:
    return build_environment().from_string(template_source).render(**context)
