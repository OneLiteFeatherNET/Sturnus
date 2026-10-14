"""What the consent prompt says before somebody presses the button.

The prompt is the last thing a person reads before agreeing to be
recorded, so what it does and does not contain is a property worth pinning
rather than a matter of wording. Two things are pinned here: that it names
where audio is sent, and that it never carries the credential that endpoint
is reached with.

The template is exercised directly. `ConsentCog.grant` needs a gateway, an
interaction and a configured guild to reach it, and none of those would
make this assertion stronger.
"""

from __future__ import annotations

from sturnus.infrastructure.discord.views import CONSENT_STRINGS

_HOST = "transcribe.example.invalid"
_POLICY = "https://example.invalid/policy"


def _prompt() -> str:
    return CONSENT_STRINGS["description"].format(policy_url=_POLICY, transcription_host=_HOST)


def test_the_prompt_names_where_audio_is_sent() -> None:
    """In front of the button, not in a document nobody opens.

    Consent covers being recorded by Sturnus; it does not by itself cover
    the recording being sent to a third party. Since transcription happens
    at an endpoint the operator chose, a person deciding whether to speak
    is entitled to see that host while they are deciding.
    """
    assert _HOST in _prompt()


def test_the_prompt_still_links_the_full_policy() -> None:
    """The host is an addition to the policy link, never a replacement."""
    assert _POLICY in _prompt()


def test_the_prompt_says_that_audio_leaves_this_deployment() -> None:
    """Naming a host is not the same as saying something is sent to it.

    A reader who does not already know what a transcription endpoint is
    learns nothing from a hostname on its own, so the sentence has to
    carry the act as well as the address.
    """
    prompt = _prompt()

    assert "sent there" in prompt


def test_the_template_asks_for_both_values() -> None:
    """A caller that forgets either one fails loudly.

    `str.format` raises `KeyError` on a missing placeholder rather than
    rendering an empty space, which is what makes this template safe to
    extend: a future call site that supplies only the policy URL cannot
    quietly ship a prompt with the endpoint silently missing.
    """
    assert "{transcription_host}" in CONSENT_STRINGS["description"]
    assert "{policy_url}" in CONSENT_STRINGS["description"]


def test_no_consent_string_carries_a_credential_placeholder() -> None:
    """The host, never the key.

    The endpoint's API key exists in `WorkerSettings` as a `SecretStr` and
    reaches no user-facing text, no log field, no span attribute and no
    metric label. This is the user-facing half of that guarantee, asserted
    on every string in the table rather than only on the one that gained a
    placeholder.
    """
    for text in CONSENT_STRINGS.values():
        assert "api_key" not in text
        assert "{transcription_api_key}" not in text
        assert "sk-" not in text
