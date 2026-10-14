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

import pytest
from pydantic import ValidationError

from sturnus.application.worker import _FALLBACK_TEMPLATE
from sturnus.domain import transcription_models
from sturnus.entrypoints.worker import WorkerSettings, _load_template


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
        "transcription_base_url": "https://transcribe.invalid/v1",
    }
    return WorkerSettings(**{**required, **overrides})  # type: ignore[arg-type]


def test_a_worker_without_an_endpoint_does_not_start() -> None:
    """**A consent control, not a missing default.**

    Audio leaves the pod now. A default here -- any default -- would mean
    a configuration under which a recording is sent somewhere because
    nobody chose otherwise. The operator either names a host or has no
    working deployment, and this is the line that makes that true.
    """
    required = {
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

    with pytest.raises(ValidationError) as raised:
        WorkerSettings(**required)  # type: ignore[arg-type]

    assert "transcription_base_url" in str(raised.value)


def test_a_worker_needs_no_api_key() -> None:
    """A vLLM or Speaches container on the cluster's own network has none.

    Required-with-no-default for the URL and optional for the key is the
    whole configuration story: the first says where, and the second is a
    property of what is there.
    """
    assert _settings().transcription_api_key.get_secret_value() == ""


def test_the_default_model_is_the_one_a_requeue_falls_back_to() -> None:
    """Two declarations of "the model nobody chose", pinned to one value.

    `WorkerSettings.transcription_model` is what a first pass runs;
    `sturnus.domain.transcription_models.FALLBACK` is what a re-queue
    writes into `transcription_job.requested_model` when the caller named
    nothing. Nothing in the running system forces them to agree -- the
    worker never reads the registry for its own default, and the registry
    cannot import the settings -- so a change to one and not the other
    would make "re-queue it unchanged" silently change the model.

    The pin is on the *declared* default, not on a deployed
    `STURNUS_TRANSCRIPTION_MODEL`. A cluster that overrides it is telling its workers
    what to run on a first pass and is not telling this repository what
    "unchanged" means; if such a deployment wants its re-queues to agree,
    the registry is where that is said.
    """
    assert _settings().transcription_model == transcription_models.FALLBACK


def test_the_default_language_is_the_one_these_meetings_are_held_in() -> None:
    """This is the value that decides what an unconfigured guild gets when
    the endpoint's own detection comes up empty, and `en` on a
    German-speaking server was simply wrong. Per-guild configuration
    (`transcription_language`, Spec 11) overrides it; this is the floor
    under it.
    """
    assert _settings().transcription_default_language == "de"


def test_the_window_cap_stays_inside_the_endpoint_s_request_limit() -> None:
    """Twelve minutes at 32 kB/s is about 23 MB, under OpenAI's 25 MB.

    The cap is in seconds because that is the unit the cuts are planned
    in, so the size it implies has to be checked rather than read off.
    16 kHz mono 16-bit PCM is the format `SpeakerWriter` writes, and the
    multipart envelope needs the remaining headroom.
    """
    bytes_per_second = 16_000 * 2
    window_bytes = _settings().transcription_max_chunk_seconds * bytes_per_second

    assert window_bytes < 25 * 1000 * 1000
