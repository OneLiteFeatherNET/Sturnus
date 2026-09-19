"""Tests for the `/about` notice text (AGPL-3.0 section 13).

`about_text` is exercised directly, without a Discord gateway connection:
it is the decision logic behind `/about`, and the one thing this command
must never do is silently lose the license name or the source-code link
that section 13's network-use offer depends on.

It also names the transcription endpoint, which is not an AGPL obligation
but the same kind of fact: `/about` is the one command whose purpose is
telling somebody what this deployment actually is, and where their voice
is sent belongs in that answer.
"""

from sturnus.infrastructure.discord.about_cog import LICENSE_NAME, REPOSITORY_URL, about_text

HOST = "transcribe.example.invalid"


def test_notice_names_the_license() -> None:
    assert LICENSE_NAME in about_text(transcription_host=HOST)


def test_notice_links_to_the_source_repository() -> None:
    assert REPOSITORY_URL in about_text(transcription_host=HOST)


def test_notice_reports_the_given_version() -> None:
    assert "v1.2.3" in about_text(transcription_host=HOST, version="1.2.3")


def test_notice_reports_a_custom_repository_url() -> None:
    custom_url = "https://example.invalid/fork"
    assert custom_url in about_text(transcription_host=HOST, repository_url=custom_url)


def test_default_repository_url_points_at_onelitefeather() -> None:
    """A fork changing this constant is a deliberate choice, not an accident."""
    assert REPOSITORY_URL == "https://github.com/OneLiteFeatherNET/Sturnus"


def test_notice_names_where_speech_is_transcribed() -> None:
    """Where a person's voice is sent is part of what this deployment is."""
    assert HOST in about_text(transcription_host=HOST)


def test_notice_carries_no_credential() -> None:
    """The host, never the key.

    A base URL can carry a credential in its path or query, and `/about`
    is a message anybody on the server can ask for -- so the caller hands
    this function a host that `endpoint_host` has already reduced, and
    this pins that nothing else about the endpoint is printed.
    """
    notice = about_text(transcription_host=HOST)

    assert "sk-" not in notice
    assert "?" not in notice.split("Speech is transcribed by:")[1]
