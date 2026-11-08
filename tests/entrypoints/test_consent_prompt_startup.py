"""The consent prompt's buttons are registered at startup (task 5.2).

A prompt posted before a restart keeps its buttons in Discord, and a press
reaches the bot only if a `DynamicItem` matching its custom id is registered.
The buttons must therefore be registered when the bot starts, not when the
first prompt is posted, or the first press after a restart fails.

The client is the real `SturnusClient` built by `test_client`, with stores
stubbed. Nothing connects to Discord; registration is checked on the client's
own view store.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord

from sturnus.entrypoints.bot import attach_consent_prompt
from sturnus.infrastructure.discord.consent_prompt import (
    CONSENT_BUTTON_ID,
    LINK_BUTTON_ID,
)
from tests.infrastructure.discord.test_client import FakeClock, _client
from tests.infrastructure.discord.test_consent_prompt_message import make_services

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def _registered_custom_ids(client: discord.Client) -> list[str]:
    """The custom ids a press could match right now, from the client's view store."""
    store = client._connection._view_store
    patterns = list(store._dynamic_items)
    return [
        custom_id
        for custom_id in (CONSENT_BUTTON_ID, LINK_BUTTON_ID)
        if any(pattern.match(custom_id) for pattern in patterns)
    ]


def test_the_consent_button_is_registered_once_the_bot_has_started() -> None:
    client = _client(FakeClock(T0))

    attach_consent_prompt(client, make_services())

    assert CONSENT_BUTTON_ID in _registered_custom_ids(client)


def test_the_link_button_is_registered_once_the_bot_has_started() -> None:
    client = _client(FakeClock(T0))

    attach_consent_prompt(client, make_services())

    assert LINK_BUTTON_ID in _registered_custom_ids(client)


def test_the_client_holds_the_services_its_buttons_read_after_startup() -> None:
    client = _client(FakeClock(T0))
    services = make_services()

    attach_consent_prompt(client, services)

    assert client.consent_prompt_services is services


def test_a_client_without_attached_services_has_the_join_prompt_switched_off() -> None:
    client = _client(FakeClock(T0))

    assert client.consent_prompt_services is None
