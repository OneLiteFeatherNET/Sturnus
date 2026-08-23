"""Process configuration from the environment.

Runtime settings that administrators change live in the database (see
`ConfigStore`); this module holds only what must exist before the bot can
reach a database at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sturnus.application.sharding import MIN_SHARD_COUNT


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


class OtelSettings(StrictSettings):
    """Whether, and where, to send traces and metrics.

    Its own class, and constructed before the process's real settings, for
    exactly the two reasons `SentrySettings` gives: nothing here is
    required, so it always builds; and observability has no business
    widening the per-process credential asymmetry Spec 13.2 establishes.

    **The blank-to-`None` normalisation is the same load-bearing trick, for
    the same verified reason.** An unconfigured cluster's chart default is
    `STURNUS_OTEL_EXPORTER_OTLP_ENDPOINT: ""`, not an absent variable, and
    `StrictSettings._reject_blank_required_values` does not apply to an
    optional field. So blank must become `None` here, and `None` must mean
    *no provider is ever constructed* -- not "a provider pointed at
    nothing", which would retry a dead endpoint forever and log an ERROR
    each time. With no provider the OpenTelemetry API degrades to
    `NonRecordingSpan` and `_ProxyCounter`, measured at 0.10 us per
    `counter.add`, so every instrumentation call in the codebase becomes a
    no-op without a single conditional at a call site.

    **One switch, deliberately.** The bare `OTEL_EXPORTER_OTLP_ENDPOINT` and
    `OTEL_SDK_DISABLED` that the SDK reads natively are *not* honoured. Two
    switches for one behaviour is how a deployment ends up silently wrong in
    a way no test catches; the endpoint below is passed explicitly to both
    exporters instead.

    `environment` deliberately reads `STURNUS_SENTRY_ENVIRONMENT` -- the
    variable the Sentry work already landed, documented and defaulted in the
    chart -- rather than introducing a second name. One environment string
    for both back ends, so `deployment.environment.name` in Tempo can never
    disagree with the environment filter in Sentry. If the two ever need to
    differ, that is a rename of one shared variable, not a second one added
    quietly alongside it.
    """

    otel_exporter_otlp_endpoint: str | None = None
    otel_traces_sample_ratio: float = 1.0
    otel_metric_export_interval_seconds: float = 60.0
    environment: str = Field(default="production", validation_alias="STURNUS_SENTRY_ENVIRONMENT")

    @field_validator("otel_exporter_otlp_endpoint", mode="after")
    @classmethod
    def _blank_endpoint_is_absent(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()

    @field_validator("otel_traces_sample_ratio", mode="after")
    @classmethod
    def _ratio_in_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                "STURNUS_OTEL_TRACES_SAMPLE_RATIO must be between 0.0 and 1.0 inclusive"
            )
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
    # How many gateway connections this one process opens. **Unset means
    # "let Discord decide"** -- `discord.AutoShardedClient` asks
    # `/gateway/bot` and uses the recommendation, which grows with the
    # guild count on its own. A number pinned in a values file does not,
    # so an explicit value is for an operator who knows why they want one
    # (matching a `max_concurrency` bucket, reproducing a routing problem)
    # and is prepared to revisit it.
    #
    # It does **not** make the bot horizontally scalable: every shard here
    # lives in this one process, and `replicas: 1` is unchanged. See
    # `sturnus.application.sharding` for the two stages this distinguishes.
    shard_count: int | None = None
    # Off by default, and meant to be turned on for one recording at a
    # time. Measures what Discord actually sends and what the Opus decoder
    # makes of it, which is the one thing a finished WAV cannot show --
    # see `sturnus.infrastructure.discord.capture_diagnostics`. Records no
    # audio: packet sizes, packet shapes and three aggregate numbers.
    capture_diagnostics: bool = False

    @field_validator("shard_count", mode="before")
    @classmethod
    def _blank_shard_count_is_absent(cls, value: object) -> object:
        """A blank `STURNUS_SHARD_COUNT` means "let Discord decide", not a failure.

        The same load-bearing normalisation `OtelSettings` performs on its
        endpoint, and for the same reason: the chart renders every optional
        value, so a cluster that has not opted in supplies `""` rather than
        omitting the variable. Pydantic would reject that as an unparseable
        `int` and the pod would crash-loop on a setting nobody had touched.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("shard_count", mode="after")
    @classmethod
    def _at_least_one_shard(cls, value: int | None) -> int | None:
        """Refuses a shard count below one while an operator is still looking.

        Left to the gateway, zero would build an empty shard range: no
        connection, `on_ready` never fires, and the process sits there
        looking alive behind a readiness probe that never turns green and
        no line saying why. Naming the variable at startup is the whole
        point of `StrictSettings`.
        """
        if value is not None and value < MIN_SHARD_COUNT:
            raise ValueError(
                f"STURNUS_SHARD_COUNT must be at least {MIN_SHARD_COUNT}. "
                f"Leave it unset to let Discord choose the shard count."
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
