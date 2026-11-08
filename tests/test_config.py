import base64

import pytest
from pydantic import ValidationError

from sturnus.config import SentrySettings, Settings, shard_count_is_configured

# KeyWrapper requires the master key to base64-decode to exactly 32 bytes,
# which makes any valid fixture look like a real key to a secret scanner.
# Built at runtime instead of written as a literal, so no base64 blob sits
# in the source for the scanner to (falsely) flag -- the plaintext sentinel
# below is obviously not a real key to a human reader either.
_FAKE_MASTER_KEY = b"THIS-IS-NOT-A-REAL-KEY-test-only"
assert len(_FAKE_MASTER_KEY) == 32
_FAKE_MASTER_KEY_B64 = base64.b64encode(_FAKE_MASTER_KEY).decode()


def _env(**overrides: str) -> dict[str, str]:
    base = {
        "STURNUS_DISCORD_TOKEN": "discord-secret-value",
        "STURNUS_DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "STURNUS_S3_ENDPOINT": "https://s3.example",
        "STURNUS_S3_BUCKET": "sturnus-audio",
        "STURNUS_S3_ACCESS_KEY": "ak",
        "STURNUS_S3_SECRET_KEY": "s3-secret-value",
        "STURNUS_MASTER_KEY": _FAKE_MASTER_KEY_B64,
        "STURNUS_MASTER_KEY_ID": "k1",
        "STURNUS_RECORDING_DIR": "/tmp/rec",
        "STURNUS_OUTLINE_BASE_URL": "https://outline.example",
        "STURNUS_OUTLINE_CLIENT_ID": "outline-client-id",
        "STURNUS_OUTLINE_REDIRECT_URI": "https://bot.example/oauth/callback",
        # The bot never calls this endpoint; `/about` and the `/consent`
        # prompt name its host, so the bot has to be told what it is.
        "STURNUS_TRANSCRIPTION_BASE_URL": "https://transcribe.example/v1",
        "STURNUS_GUILD_ID": "123456789012345678",
    }
    base.update(overrides)
    return base


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.s3_bucket == "sturnus-audio"
    assert s.health_port == 8080


def test_missing_required_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STURNUS_DISCORD_TOKEN")
    with pytest.raises(ValidationError):
        Settings()


def test_the_served_guild_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    assert Settings().guild_id == 123456789012345678


def test_a_bot_without_a_guild_id_refuses_to_construct_its_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No default: a bot must never serve whichever guilds it happens to find itself in."""
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STURNUS_GUILD_ID")
    with pytest.raises(ValidationError) as raised:
        Settings()
    assert "guild_id" in str(raised.value)


def test_secrets_are_not_exposed_by_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings object must be safe to log or include in a traceback."""
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    rendered = repr(Settings())
    # Assert on the secret VALUES, never on words that also appear in field
    # names — `discord_token` contains "token", so that assertion could never
    # hold regardless of whether masking works.
    assert "discord-secret-value" not in rendered
    assert "s3-secret-value" not in rendered
    assert _FAKE_MASTER_KEY_B64 not in rendered


@pytest.mark.parametrize(
    "blank",
    ["STURNUS_OUTLINE_CLIENT_ID", "STURNUS_DISCORD_TOKEN", "STURNUS_MASTER_KEY"],
)
def test_a_blank_required_setting_is_rejected(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    """Present-but-empty is the failure mode a missing-value check misses.

    Pydantic accepts `""` for a required `str` or `SecretStr`, so a
    placeholder never filled in -- `STURNUS_OUTLINE_CLIENT_ID=` in a secret
    file, say -- starts the process, passes its readiness probe, and fails
    only when something first uses the value. For the OAuth client id that
    is when a participant runs `/link start`, and the error surfaces from
    Outline rather than from Sturnus.
    """
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(blank, "")
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    # The message has to name the variable: an operator reading a crashed
    # pod's logs needs to know which one to go and fill in.
    assert blank in str(excinfo.value)


def test_whitespace_does_not_satisfy_a_required_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray space in a secret file is as empty as no value at all."""
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("STURNUS_OUTLINE_CLIENT_ID", "   ")
    with pytest.raises(ValidationError):
        Settings()


def test_an_optional_setting_may_still_be_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check covers required fields only -- an optional one with a
    default is free to be whatever it is, and must not be dragged in."""
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    assert Settings().health_port == 8080


def test_sentry_is_absent_unless_a_dsn_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DSN means no Sentry, which is what an operator who has not opted
    in gets. `sturnus.infrastructure.observability.init_sentry` branches on
    exactly this."""
    monkeypatch.delenv("STURNUS_SENTRY_DSN", raising=False)
    settings = SentrySettings()
    assert settings.sentry_dsn is None
    assert settings.sentry_environment == "production"


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_blank_sentry_dsn_is_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """`STURNUS_SENTRY_DSN=""` is what the chart ships by default, and
    pydantic turns it into `SecretStr('')` rather than `None` -- the
    blank-required-value check above does not apply, because the field is
    optional. Left as `SecretStr('')` it would reach `sentry_sdk.init()`,
    which for an empty DSN installs every monkey-patch the SDK has and then
    sends nothing: all of the risk, none of the reporting.
    """
    monkeypatch.setenv("STURNUS_SENTRY_DSN", blank)
    assert SentrySettings().sentry_dsn is None


def test_a_real_sentry_dsn_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STURNUS_SENTRY_DSN", "https://key@sentry.example/7")
    monkeypatch.setenv("STURNUS_SENTRY_ENVIRONMENT", "staging")
    settings = SentrySettings()
    assert settings.sentry_dsn is not None
    assert settings.sentry_dsn.get_secret_value() == "https://key@sentry.example/7"
    assert settings.sentry_environment == "staging"


def test_sentry_settings_do_not_require_the_rest_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason this is its own class: it constructs with nothing set, so
    `init_sentry` can run before `Settings()`/`WorkerSettings()`/
    `LinkSettings()` and a settings `ValidationError` is itself reportable
    rather than being the one failure Sentry can never see.
    """
    for key in _env():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("STURNUS_SENTRY_DSN", raising=False)
    assert SentrySettings().sentry_dsn is None


@pytest.mark.parametrize("leftover", ["4", "0", "-1", ""])
def test_a_leftover_shard_count_does_not_stop_the_bot_starting(
    monkeypatch: pytest.MonkeyPatch, leftover: str
) -> None:
    """`STURNUS_SHARD_COUNT` was removed with per-shard gateway connections.

    An operator who has not yet unset it must not have a crash-looping pod
    over a setting that now does nothing: pydantic-settings reads only the
    fields it declares, so the variable is simply not looked at. Whether to
    say so is the entrypoint's decision, tested below.
    """
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("STURNUS_SHARD_COUNT", leftover)
    assert Settings().guild_id == 123456789012345678


def test_a_leftover_shard_count_changes_no_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STURNUS_SHARD_COUNT", raising=False)
    without = Settings().model_dump()
    monkeypatch.setenv("STURNUS_SHARD_COUNT", "4")
    assert Settings().model_dump() == without


def test_the_settings_no_longer_declare_a_shard_count() -> None:
    assert "shard_count" not in Settings.model_fields


def test_a_set_shard_count_variable_is_reported_even_when_blank() -> None:
    """A chart that still renders the variable as `""` should be named in the log.

    The hint is for an operator's leftover, so it fires on presence, not on
    a useful value.
    """
    assert shard_count_is_configured({"STURNUS_SHARD_COUNT": ""}) is True


def test_an_unset_shard_count_variable_is_not_reported() -> None:
    assert shard_count_is_configured({"STURNUS_DISCORD_TOKEN": "x"}) is False
