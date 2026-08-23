"""What this deployment will accept as the name of a transcription model.

The registry's whole purpose is that a name is checked *before* it becomes
a job, so the tests here are about the two answers it gives -- "yes, and
here is what it costs you" and "no, and here is what there is" -- and
about the one invariant that makes the second answer safe to act on: the
fallback is itself a member.
"""

from __future__ import annotations

import pytest

from sturnus.domain import transcription_models as registry


def test_every_registered_model_describes_the_trade_it_makes() -> None:
    """A dropdown that lists seven names and explains none of them is a guess.

    An administrator choosing between `tiny` and `large-v3` is choosing
    between minutes and hours of worker time and between a usable
    transcript and a rewrite, so each entry has to carry enough to make
    that choice without leaving the page.
    """
    assert registry.KNOWN_MODELS
    for model in registry.KNOWN_MODELS:
        assert model.name
        assert model.approximate_size
        assert model.summary
        assert model.summary.endswith("."), "each summary is a sentence, not a label"


def test_the_fallback_is_one_of_the_registered_models() -> None:
    """Otherwise every refusal offers a replacement that is itself refused."""
    assert registry.FALLBACK in registry.KNOWN_NAMES


def test_the_registered_names_are_exactly_the_registry() -> None:
    """`KNOWN_NAMES` is derived, so it cannot fall out of step with the tuple."""
    assert frozenset(model.name for model in registry.KNOWN_MODELS) == registry.KNOWN_NAMES


def test_no_model_is_registered_twice() -> None:
    """Two rows with one name would render two dropdown entries that agree."""
    names = [model.name for model in registry.KNOWN_MODELS]
    assert len(names) == len(set(names))


def test_a_name_nobody_has_is_not_known() -> None:
    assert registry.is_known(registry.FALLBACK) is True
    assert registry.is_known("large-v4") is False
    assert registry.is_known("") is False


def test_describing_a_registered_model_returns_its_entry() -> None:
    assert registry.describe(registry.FALLBACK).name == registry.FALLBACK


def test_describing_a_name_nobody_has_says_what_was_asked_for() -> None:
    with pytest.raises(registry.UnknownTranscriptionModel) as raised:
        registry.describe("large-v4")
    assert "large-v4" in str(raised.value)


def test_asking_for_nothing_resolves_to_the_fallback() -> None:
    """`None` means "nobody chose", and it must not survive that far.

    Every layer below the boundary deals in a concrete registered name, so
    that `transcription_job.requested_model` records what was asked for
    rather than the absence of a question.
    """
    assert registry.resolve(None) == registry.FALLBACK


def test_asking_for_a_registered_model_resolves_to_itself() -> None:
    for model in registry.KNOWN_MODELS:
        assert registry.resolve(model.name) == model.name


def test_asking_for_a_name_nobody_has_is_refused_rather_than_replaced() -> None:
    """A typo must not quietly become the fallback.

    Substituting here would run a different model than the caller named
    and record the substitute, which makes `transcription_job.model` agree
    with a request nobody made. The refusal names both halves so the
    caller can fix it in one read.
    """
    with pytest.raises(registry.UnknownTranscriptionModel) as raised:
        registry.resolve("large-v4")
    message = str(raised.value)
    assert "large-v4" in message
    assert registry.FALLBACK in message


def test_the_registry_is_ordered_from_cheapest_to_most_accurate() -> None:
    """The order is the answer to "which way is the trade", so it is pinned.

    A dropdown renders this tuple in this order and adds nothing of its
    own; a registry that got shuffled would silently reverse what the list
    is telling somebody.
    """
    names = [model.name for model in registry.KNOWN_MODELS]
    assert names.index("tiny") < names.index("small") < names.index("large-v3")
