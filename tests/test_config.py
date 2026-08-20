import base64

import pytest
from pydantic import ValidationError

from sturnus.config import Settings

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
