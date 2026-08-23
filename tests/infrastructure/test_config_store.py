from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sturnus.domain import settings
from sturnus.infrastructure.db.config_store import ConfigStore
from sturnus.infrastructure.db.models import Base

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
GUILD = 4711


@pytest.fixture
async def store(clean_database: str) -> ConfigStore:
    engine: AsyncEngine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return ConfigStore(async_sessionmaker(engine, expire_on_commit=False))


async def test_unset_key_without_default_is_none(store: ConfigStore) -> None:
    assert await store.get(GUILD, settings.VOICE_CHANNEL_ID) is None


async def test_unset_key_falls_back_to_default(store: ConfigStore) -> None:
    assert await store.get(GUILD, settings.IDLE_TIMEOUT_MINUTES) == "15"


async def test_stored_value_wins_over_default(store: ConfigStore) -> None:
    await store.set(GUILD, settings.IDLE_TIMEOUT_MINUTES, "45", T0)
    assert await store.get(GUILD, settings.IDLE_TIMEOUT_MINUTES) == "45"


async def test_set_is_idempotent_and_updates_in_place(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "8", T0)
    assert await store.get(GUILD, settings.MAX_SESSION_HOURS) == "8"


async def test_clearing_a_value_restores_the_default(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    await store.set(GUILD, settings.MAX_SESSION_HOURS, None, T0)
    assert await store.get(GUILD, settings.MAX_SESSION_HOURS) == "4"


async def test_guilds_are_isolated(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    assert await store.get(9999, settings.MAX_SESSION_HOURS) == "4"


async def test_timeouts_are_assembled_from_config(store: ConfigStore) -> None:
    await store.set(GUILD, settings.EMPTY_GRACE_SECONDS, "90", T0)
    timeouts = await store.timeouts(GUILD)
    assert timeouts.empty_grace_seconds == 90
    assert timeouts.idle_timeout_minutes == 15  # default value
    assert timeouts.max_session_hours == 4


async def test_set_rejects_a_non_numeric_value_for_an_integer_key(store: ConfigStore) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        await store.set(GUILD, settings.MAX_SESSION_HOURS, "not-a-number", T0)


async def test_set_rejects_a_non_positive_value_for_an_integer_key(store: ConfigStore) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        await store.set(GUILD, settings.MAX_SESSION_HOURS, "0", T0)
    with pytest.raises(ValueError, match="must be positive"):
        await store.set(GUILD, settings.IDLE_TIMEOUT_MINUTES, "-5", T0)


async def test_rejected_set_does_not_change_the_stored_value(store: ConfigStore) -> None:
    await store.set(GUILD, settings.MAX_SESSION_HOURS, "6", T0)
    with pytest.raises(ValueError):
        await store.set(GUILD, settings.MAX_SESSION_HOURS, "bogus", T0)
    assert await store.get(GUILD, settings.MAX_SESSION_HOURS) == "6"


async def test_snapshot_merges_stored_values_over_the_defaults(store: ConfigStore) -> None:
    """One query for the whole guild -- what the per-tick reconcile reads."""
    await store.set(GUILD, settings.IDLE_TIMEOUT_MINUTES, "45", T0)
    await store.set(GUILD, settings.VOICE_CHANNEL_ID, "12345", T0)

    snapshot = await store.snapshot(GUILD)

    assert snapshot[settings.IDLE_TIMEOUT_MINUTES] == "45"
    assert snapshot[settings.VOICE_CHANNEL_ID] == "12345"
    assert snapshot[settings.MAX_SESSION_HOURS] == settings.DEFAULTS[settings.MAX_SESSION_HOURS]


async def test_snapshot_omits_keys_with_neither_a_value_nor_a_default(store: ConfigStore) -> None:
    """Absence is the signal that a guild cannot record, so it must survive.

    `voice_channel_id` and `consent_role_id` have no defaults; a snapshot
    that invented one for them would make an unconfigured guild look
    configured to the reconcile pass.
    """
    snapshot = await store.snapshot(GUILD)
    assert settings.VOICE_CHANNEL_ID not in snapshot
    assert settings.CONSENT_ROLE_ID not in snapshot


async def test_snapshot_agrees_with_get_for_every_known_key(store: ConfigStore) -> None:
    """The reconcile path must not resolve values differently from `/config show`."""
    await store.set(GUILD, settings.EMPTY_GRACE_SECONDS, "90", T0)
    snapshot = await store.snapshot(GUILD)
    for key in frozenset(settings.DEFAULTS) | settings.REQUIRED_KEYS:
        assert snapshot.get(key) == await store.get(GUILD, key)


async def test_snapshot_is_per_guild(store: ConfigStore) -> None:
    await store.set(GUILD, settings.VOICE_CHANNEL_ID, "12345", T0)
    assert settings.VOICE_CHANNEL_ID not in await store.snapshot(GUILD + 1)


async def test_the_transcription_language_is_a_key_an_administrator_can_set(
    store: ConfigStore,
) -> None:
    """`set` rejects every key it does not know, so a setting the worker
    reads but `DEFAULTS` never names is one nobody can change: `/config
    set` answers "unknown configuration key" while the worker goes on
    using the built-in value forever.
    """
    await store.set(GUILD, settings.TRANSCRIPTION_LANGUAGE, "en", T0)
    assert await store.get(GUILD, settings.TRANSCRIPTION_LANGUAGE) == "en"


async def test_clearing_the_transcription_language_restores_german(store: ConfigStore) -> None:
    """Clearing restores the default rather than removing the value, which
    is why `auto` exists as a spelling (`settings.DETECT_LANGUAGE`) -- there
    is no state in which no language is configured.
    """
    await store.set(GUILD, settings.TRANSCRIPTION_LANGUAGE, "en", T0)
    await store.set(GUILD, settings.TRANSCRIPTION_LANGUAGE, None, T0)
    assert await store.get(GUILD, settings.TRANSCRIPTION_LANGUAGE) == "de"


async def test_a_guild_that_configured_nothing_still_gets_the_projects_vocabulary(
    store: ConfigStore,
) -> None:
    """The prompt is only worth having if it carries the names Whisper
    actually gets wrong, and a guild that never runs `/config set` is the
    normal case -- so the default has to be the real vocabulary, not an
    empty string waiting for someone to fill it in.
    """
    prompt = await store.get(GUILD, settings.TRANSCRIPTION_PROMPT)
    assert prompt is not None
    for name in ("Ducula", "Guira", "Minestom", "Outline"):
        assert name in prompt


async def test_the_transcription_prompt_is_a_key_an_administrator_can_set(
    store: ConfigStore,
) -> None:
    await store.set(GUILD, settings.TRANSCRIPTION_PROMPT, "Nur eigene Wörter.", T0)
    assert await store.get(GUILD, settings.TRANSCRIPTION_PROMPT) == "Nur eigene Wörter."


# ---------------------------------------------------------------------------
# Boolean keys
# ---------------------------------------------------------------------------


async def test_a_boolean_key_accepts_the_two_spellings_it_names(store: ConfigStore) -> None:
    await store.set(GUILD, settings.VIDEO_CONSENT_OFFERED, settings.TRUE, T0)
    assert await store.get(GUILD, settings.VIDEO_CONSENT_OFFERED) == settings.TRUE

    await store.set(GUILD, settings.VIDEO_CONSENT_OFFERED, settings.FALSE, T0)
    assert await store.get(GUILD, settings.VIDEO_CONSENT_OFFERED) == settings.FALSE


@pytest.mark.parametrize("value", ["yes", "1", "True", "off", ""])
async def test_anything_else_is_refused_at_write_time(store: ConfigStore, value: str) -> None:
    """Stronger than the integer case, not weaker.

    A bad integer fails loudly on the read path. `settings.is_true` reads
    anything it does not recognise as false, so an unvalidated `"yes"`
    would fail nowhere at all -- it would quietly mean the opposite of
    what the person who typed it meant, about whether a guild may offer
    video consent.
    """
    with pytest.raises(ValueError):
        await store.set(GUILD, settings.VIDEO_CONSENT_OFFERED, value, T0)


async def test_a_guild_that_was_never_asked_does_not_offer_video_consent(
    store: ConfigStore,
) -> None:
    """The default says the policy document has not been checked, which for
    every guild nobody has asked is true. `true` as a default would assert
    something about somebody else's wording."""
    assert settings.is_true(await store.get(GUILD, settings.VIDEO_CONSENT_OFFERED)) is False


async def test_clearing_it_puts_the_guild_back_to_not_offering_it(store: ConfigStore) -> None:
    await store.set(GUILD, settings.VIDEO_CONSENT_OFFERED, settings.TRUE, T0)
    await store.set(GUILD, settings.VIDEO_CONSENT_OFFERED, None, T0)

    assert settings.is_true(await store.get(GUILD, settings.VIDEO_CONSENT_OFFERED)) is False


async def test_a_guild_that_was_never_asked_does_not_offer_administrator_downloads(
    store: ConfigStore,
) -> None:
    """The second key built the same way, and for a heavier reason.

    Turning it on grants access rather than withholding it: it says an
    administrator may obtain a copy of a recording of a meeting they were
    not in. Software cannot read the document at `policy_url` to check
    that participants were told so, and the honest form of "cannot check"
    is a switch that starts off.
    """
    assert settings.is_true(await store.get(GUILD, settings.ADMIN_AUDIO_DOWNLOAD_OFFERED)) is False


@pytest.mark.parametrize("value", ["yes", "1", "True", "off", ""])
async def test_the_download_switch_refuses_anything_but_its_two_spellings(
    store: ConfigStore, value: str
) -> None:
    with pytest.raises(ValueError):
        await store.set(GUILD, settings.ADMIN_AUDIO_DOWNLOAD_OFFERED, value, T0)
