"""Tests for the `/about` notice text (AGPL-3.0 section 13).

`about_text` is exercised directly, without a Discord gateway connection:
it is the decision logic behind `/about`, and the one thing this command
must never do is silently lose the license name or the source-code link
that section 13's network-use offer depends on.
"""

from sturnus.infrastructure.discord.about_cog import LICENSE_NAME, REPOSITORY_URL, about_text


def test_notice_names_the_license() -> None:
    assert LICENSE_NAME in about_text()


def test_notice_links_to_the_source_repository() -> None:
    assert REPOSITORY_URL in about_text()


def test_notice_reports_the_given_version() -> None:
    assert "v1.2.3" in about_text(version="1.2.3")


def test_notice_reports_a_custom_repository_url() -> None:
    custom_url = "https://example.invalid/fork"
    assert custom_url in about_text(repository_url=custom_url)


def test_default_repository_url_points_at_onelitefeather() -> None:
    """A fork changing this constant is a deliberate choice, not an accident."""
    assert REPOSITORY_URL == "https://github.com/OneLiteFeatherNET/Sturnus"
