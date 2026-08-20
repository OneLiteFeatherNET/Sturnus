"""Regression test for Defect D: production must load the real template.

`sturnus.application.worker.process_one` defaults to a minimal
`_FALLBACK_TEMPLATE` so its own test suite can run without reaching into
`sturnus.infrastructure` -- that default exists purely for tests. Every
production document must be rendered from the packaged
`outline_template.md.j2` instead, loaded here through
`sturnus.entrypoints.worker._load_template`. This test does not run the
worker loop; it only pins the one fact that broke: the loaded template is
the real one, containing `mention://`, the syntax `outline_template.md.j2`
uses to render an Outline mention -- not the participant-less,
mention-less fallback.
"""

from sturnus.application.worker import _FALLBACK_TEMPLATE
from sturnus.entrypoints.worker import _load_template


def test_loaded_template_is_not_the_fallback() -> None:
    assert _load_template() != _FALLBACK_TEMPLATE


def test_loaded_template_renders_outline_mentions() -> None:
    assert "mention://" in _load_template()
