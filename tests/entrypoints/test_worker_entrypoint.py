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

import ctranslate2  # type: ignore[import-untyped]

from sturnus.application.worker import _FALLBACK_TEMPLATE
from sturnus.domain import transcription_models
from sturnus.entrypoints.worker import (
    _WHISPER_COMPUTE_TYPE,
    WorkerSettings,
    _load_template,
)


def test_loaded_template_is_not_the_fallback() -> None:
    assert _load_template() != _FALLBACK_TEMPLATE


def test_loaded_template_renders_outline_mentions() -> None:
    assert "mention://" in _load_template()


def _settings(**overrides: object) -> WorkerSettings:
    """`WorkerSettings` with every required field supplied as a literal.

    Passed as arguments rather than through `monkeypatch.setenv` so the
    defaults under test are read from the class, not from whatever the
    machine running the suite happens to export.
    """
    required: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@db/sturnus",
        "s3_endpoint": "http://s3.invalid",
        "s3_bucket": "sturnus-audio",
        "s3_access_key": "access",
        "s3_secret_key": "secret",
        "master_key": "a" * 44,
        "master_key_id": "k1",
        "outline_base_url": "https://outline.invalid",
        "outline_service_key": "token",
    }
    return WorkerSettings(**{**required, **overrides})  # type: ignore[arg-type]


def test_the_default_model_is_the_undistilled_large_one() -> None:
    """`large-v3-turbo` is a distilled decoder -- four layers where
    `large-v3` has thirty-two -- and the accuracy it gives up is not evenly
    spread: it shows up outside English, which is the only place this
    deployment operates. Nothing here is latency-sensitive (transcription
    happens offline, per speaker, after the meeting), so the distillation
    buys time nobody is waiting for.
    """
    assert _settings().whisper_model == "large-v3"


def test_the_default_model_is_the_one_a_requeue_falls_back_to() -> None:
    """Two declarations of "the model nobody chose", pinned to one value.

    `WorkerSettings.whisper_model` is what a first pass runs;
    `sturnus.domain.transcription_models.FALLBACK` is what a re-queue
    writes into `transcription_job.requested_model` when the caller named
    nothing. Nothing in the running system forces them to agree -- the
    worker never reads the registry for its own default, and the registry
    cannot import the settings -- so a change to one and not the other
    would make "re-queue it unchanged" silently change the model.

    The pin is on the *declared* default, not on a deployed
    `WHISPER_MODEL`. A cluster that overrides it is telling its workers
    what to run on a first pass and is not telling this repository what
    "unchanged" means; if such a deployment wants its re-queues to agree,
    the registry is where that is said.
    """
    assert _settings().whisper_model == transcription_models.FALLBACK


def test_the_default_language_is_the_one_these_meetings_are_held_in() -> None:
    """This is the value that decides what an unconfigured guild gets when
    the engine's own detection comes up empty, and `en` on a
    German-speaking server was simply wrong. Per-guild configuration
    (`transcription_language`, Spec 11) overrides it; this is the floor
    under it.
    """
    assert _settings().whisper_default_language == "de"


def test_the_configured_quantisation_is_one_this_cpu_can_actually_run() -> None:
    """CTranslate2 does not refuse a compute type it cannot provide -- it
    quietly falls back to one it can, so a wrong value here costs accuracy
    or speed with nothing in the logs to say so. `int8_float32` names the
    activation type outright instead of leaving it to the `int8` alias,
    whose float type CTranslate2 chooses per device.
    """
    assert _WHISPER_COMPUTE_TYPE in ctranslate2.get_supported_compute_types("cpu")
