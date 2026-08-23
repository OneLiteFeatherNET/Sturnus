"""What may be written in the URL that chooses which OAuth client signs you in.

A slug is not a label. It is a public path segment that **selects a
credential**: `/api/auth/login?guild=acme` decides which client id and
which client secret complete the round trip. So the shape is decided
here, in a pure function a test can reach without a database, and
everything outside it is refused rather than normalised -- a slug that
was quietly rewritten before being stored is a slug the administrator
does not recognise in the link they were told to distribute.

Two properties the shape exists for:

- **It cannot be confused with a route.** The console serves the link at
  `/g/{slug}/sign-in`, and a deployment that later serves anything else
  under a name a guild has already claimed has a collision it cannot
  resolve.
- **It cannot be confused with a guild id.** A snowflake is digits, so a
  slug must not be.
"""

from __future__ import annotations

import pytest

from sturnus.domain.oauth_clients import (
    MAX_SLUG_LENGTH,
    MIN_SLUG_LENGTH,
    RESERVED_SLUGS,
    is_provider_url,
    is_valid_slug,
)


@pytest.mark.parametrize(
    "slug",
    [
        "acme",
        "acme-industries",
        "onelitefeather",
        "team-42",
        "a" * MAX_SLUG_LENGTH,
        "a" * MIN_SLUG_LENGTH,
    ],
)
def test_an_ordinary_name_is_a_slug(slug: str) -> None:
    assert is_valid_slug(slug) is True


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "a" * (MIN_SLUG_LENGTH - 1),
        "a" * (MAX_SLUG_LENGTH + 1),
    ],
)
def test_a_slug_is_long_enough_to_name_something_and_short_enough_for_a_url(slug: str) -> None:
    assert is_valid_slug(slug) is False


@pytest.mark.parametrize(
    "slug",
    [
        "Acme",
        "ACME",
        "acme industries",
        "acme_industries",
        "acme.industries",
        "acme/sign-in",
        "acme%2f",
        "acme?guild=other",
        "acme#fragment",
        "acme\n",
        "acmé",
        "../acme",
    ],
)
def test_anything_that_would_have_to_be_escaped_is_not_a_slug(slug: str) -> None:
    """A slug goes into a URL unencoded, so it may only be URL-safe text.

    Refused rather than percent-encoded: a slug the administrator has to
    escape before pasting it into a chat message is a link nobody can
    read back to the person who typed it.
    """
    assert is_valid_slug(slug) is False


@pytest.mark.parametrize("slug", ["-acme", "acme-", "acme--industries", "--"])
def test_a_hyphen_separates_words_and_does_not_start_or_end_one(slug: str) -> None:
    assert is_valid_slug(slug) is False


@pytest.mark.parametrize("slug", ["1289374650912837465", "42", "007"])
def test_a_slug_cannot_be_mistaken_for_a_guild_id(slug: str) -> None:
    """A snowflake is digits, and a link is read by people.

    `/g/1289374650912837465/sign-in` and a guild id in a URL are the same
    string to a reader, and the two select different things. Requiring a
    letter first makes the two unconfusable by construction.
    """
    assert is_valid_slug(slug) is False


@pytest.mark.parametrize("slug", sorted(RESERVED_SLUGS))
def test_a_slug_may_not_be_a_name_this_deployment_already_serves(slug: str) -> None:
    """`/g/api/sign-in` is a link, and `/api/...` is the whole API.

    Reserving them costs a guild one candidate name and buys the
    certainty that no route ever added to this deployment can be shadowed
    by a name a guild already claimed.
    """
    assert is_valid_slug(slug) is False


def test_every_reserved_name_would_otherwise_have_been_a_slug() -> None:
    """The reservation list is only doing work while its entries are legal.

    A reserved name that the shape rules already refuse is a line nobody
    would notice going stale -- so the list is held to naming things that
    are refused *because they are reserved*, not by accident.
    """
    for reserved in RESERVED_SLUGS:
        assert _matches_the_shape(reserved), reserved


def _matches_the_shape(slug: str) -> bool:
    """The shape rules alone, with the reservation lifted."""
    from sturnus.domain.oauth_clients import has_slug_shape

    return has_slug_shape(slug)


# ---------------------------------------------------------------------------
# The two URLs a guild registers, which other people's browsers follow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://outline.example",
        "https://outline.example/",
        "https://wiki.example/outline",
        "https://outline.example:8443/outline",
    ],
)
def test_an_ordinary_provider_address_is_accepted(url: str) -> None:
    assert is_provider_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://outline.example",
        "ftp://outline.example",
        "javascript:alert(1)",
        "data:text/html,hi",
        "//outline.example",
        "outline.example",
        "",
    ],
)
def test_only_https_carries_an_authorization_code(url: str) -> None:
    """The code and the consent step both travel over this address.

    A guild that could register `http://` would be a guild whose members
    hand their authorization codes to whoever is on the path.
    """
    assert is_provider_url(url) is False


def test_a_url_that_reads_as_one_host_and_resolves_to_another_is_refused() -> None:
    """The one form where parsing correctly is a security property.

    `https://outline.example@evil.example/` names `evil.example`, and an
    administrator reviewing the value in a form reads the first host.
    """
    assert is_provider_url("https://outline.example@evil.example/") is False


@pytest.mark.parametrize(
    "url",
    [
        "https://outline.example/?client_id=stolen",
        "https://outline.example/#fragment",
        "https://outline.example/ ",
        " https://outline.example/",
        "https://outline\n.example/",
    ],
)
def test_a_base_url_carries_no_query_no_fragment_and_no_whitespace(url: str) -> None:
    """`authorize_url` builds the query; a second one would collide with it."""
    assert is_provider_url(url) is False
