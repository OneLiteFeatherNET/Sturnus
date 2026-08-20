"""Process configuration from the environment.

Runtime settings that administrators change live in the database (see
`ConfigStore`); this module holds only what must exist before the bot can
reach a database at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class StrictSettings(BaseSettings):
    """Settings that refuse to start on a blank required value.

    Shared by all three processes' settings classes. Pydantic accepts the
    empty string for a required `str` or `SecretStr`, so a variable present
    but unset -- a placeholder never filled in, a secret key holding no
    value -- produces a process that starts, passes its readiness probe, and
    fails only when something first uses the value. `link` with a blank
    OAuth client id is healthy right up until a participant runs
    `/link start` and Outline rejects them.

    Failing at startup instead names the variable while an operator is still
    looking at the deployment.
    """

    model_config = SettingsConfigDict(env_prefix="STURNUS_", frozen=True)

    @model_validator(mode="after")
    def _reject_blank_required_values(self) -> StrictSettings:
        for name, field in type(self).model_fields.items():
            if not field.is_required():
                continue
            value = getattr(self, name)
            text = value.get_secret_value() if isinstance(value, SecretStr) else value
            if isinstance(text, str) and not text.strip():
                prefix = type(self).model_config.get("env_prefix", "")
                raise ValueError(
                    f"{prefix}{name.upper()} is set but empty. It is required, "
                    f"so leaving it blank would start this process in a state "
                    f"that only fails once the value is first used."
                )
        return self


class SentrySettings(StrictSettings):
    """Whether, and where, to report errors -- read before anything else.

    Its own class rather than three fields on `Settings`/`WorkerSettings`/
    `LinkSettings` for two reasons.

    *Ordering.* Nothing here is required, so this class always constructs.
    That lets `sturnus.infrastructure.observability.init_sentry` run as the
    first thing each `main()` does, before the process's real settings are
    read -- so a configuration failure (a missing `STURNUS_MASTER_KEY`, an
    unparseable `STURNUS_HEALTH_PORT`) is itself reported. Hanging the DSN
    off `WorkerSettings` would make the one failure that most needs
    reporting the one failure that could never be reported.

    *Blast radius.* The per-process asymmetry of the other three classes is a
    security decision (Spec 13.2); observability is orthogonal to it and has
    no business widening any of them.

    The blank-to-`None` normalisation below is load-bearing, not padding.
    Two verified facts meet here:

    1. `STURNUS_SENTRY_DSN=""` yields `SecretStr('')`, not `None`, and
       `StrictSettings._reject_blank_required_values` does not apply because
       the field is not required. A chart default of `""` -- which is what
       every cluster that has not opted in will have -- lands exactly there.
    2. `sentry_sdk.init(dsn="")` does not mean "off". It builds a live client
       that reports `is_active() is True` with no transport: fully
       monkey-patched (`logging.Logger.callHandlers`, `sys.excepthook`,
       `threading.Thread.run`, `atexit`), sending nothing.

    So blank must become `None`, and `None` must mean *do not call `init()`
    at all*. `sentry_sdk.is_initialized()` / `is_active()` cannot stand in
    for that test.
    """

    sentry_dsn: SecretStr | None = None
    sentry_environment: str = "production"

    @field_validator("sentry_dsn", mode="after")
    @classmethod
    def _blank_dsn_is_absent(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return value


class Settings(StrictSettings):
    discord_token: SecretStr
    database_url: str
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    # Base64-encoded 32 bytes. See the encryption task for how it is used.
    master_key: SecretStr
    # Names which master key encrypted a given recording, so the key can be
    # rotated without re-encrypting existing material.
    master_key_id: str
    recording_dir: Path
    # Public OAuth client info for `/link` (Spec 8.4): enough to build the
    # authorization URL the user's browser is sent to. Deliberately no
    # `outline_client_secret` here -- that stays confined to
    # `sturnus.entrypoints.link.LinkSettings`, the one process allowed to
    # hold it (Spec 13.2's blast-radius separation). The bot never
    # exchanges a code for a token, so it never needs the secret.
    outline_base_url: str
    outline_client_id: str
    outline_redirect_uri: str
    health_port: int = 8080


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
