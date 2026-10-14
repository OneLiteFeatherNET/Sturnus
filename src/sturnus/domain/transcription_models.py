"""The transcription models this deployment will run, and which it defaults to.

Until this module existed, a model name was a free string that travelled
from a re-queue into `transcription_job.requested_model` and out the other
side into the decoder without anything ever looking at it. One typo, four
attempts, one lost recording, and nothing anywhere could have caught it
because nothing anywhere knew what a model name is.

**Still closed. No longer closed for the reason it was.**

The original argument was that a free string is not "a model name", it is
*an instruction to this worker to fetch and execute arbitrary weights from
the internet*, issued through a re-queue button. That argument died with
`sturnus.infrastructure.whisper`. The worker fetches nothing and executes
nothing: a model name is now a string handed to somebody else's server,
which serves what it serves and refuses the rest, and the worst a wrong
name achieves is a 400 and a failed job.

Two weaker reasons remain, and together they are still enough. The
console's re-queue dropdown (`console/routes_queue.py`) needs a finite set
of things to render. And `transcription_job.model` records what ran, so an
unknown name must still be *refused* rather than quietly substituted --
a silent fallback would make that column a lie, which is the one property
of this module that survives the change untouched.

**Why `whisper-1` alone.** On OpenAI it is the only model that supports
`verbose_json` and `timestamp_granularities`; `gpt-4o-transcribe` and
`gpt-4o-mini-transcribe` support neither. Sturnus cannot use them at all --
the protocol interleaves several speakers chronologically, so a transcript
without segment times is not a partial result, it is no result. "Use the
newer model" is the obvious wrong move, and the API error it produces will
not explain itself; `docs/operations.md` says so beside the variable.

**Why the list is still hardcoded.** A deployment whose endpoint serves a
model under another name genuinely needs this to be configuration, and the
shape for that is settled -- a frozen registry value object parsed once per
process and injected into the worker, the console and the bot. It is
deliberately not built here, because it is the only part that reaches
outside the worker; see the change's `design.md`, Decision 6. Until then, a
self-hoster with a differently named model needs a fork, and that cost is
recorded rather than hidden.

Pure data and three functions, in `domain`, so the endpoint that offers the
choice, the write that stores it and the adapter that sends it cannot
disagree about which names exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class TranscriptionModel:
    """One model this deployment will run.

    **A name and nothing else, deliberately.** This type used to carry
    `approximate_size` and `summary`, and both were honest while the worker
    downloaded the weights and held them: an administrator picking from a
    dropdown was choosing a download, a memory footprint and an accuracy,
    and none of that is legible from a name.

    For a model served by somebody else's endpoint, nobody in this
    repository can write either field truthfully. The size is not ours and
    is not downloaded; the trade a remote model makes depends on what the
    operator pointed at. A plausible-looking wrong answer in a dropdown is
    worse than a bare name, because a bare name does not invite a decision
    it cannot support.

    A one-field dataclass rather than a bare string, because the thing the
    console renders is a registry entry, and a later change that gives an
    entry something honest to say -- what the endpoint itself reports about
    it, say -- should not have to rewrite every consumer to do it.
    """

    #: The name sent as `model` to `/audio/transcriptions`, and the value
    #: stored in `transcription_job.requested_model`.
    name: str


#: What may be asked for. One entry today; the tuple shape stays because
#: the dropdown renders it as it stands and because an endpoint serving
#: two models is the ordinary case this deployment has not reached yet.
KNOWN_MODELS: Final[tuple[TranscriptionModel, ...]] = (TranscriptionModel(name="whisper-1"),)

#: The model a re-queue runs when nobody chose one. This agrees with
#: `WorkerSettings.transcription_model` and a test keeps it agreeing.
FALLBACK: Final = "whisper-1"

#: Derived rather than restated, so a name can only be added in one place.
KNOWN_NAMES: Final[frozenset[str]] = frozenset(model.name for model in KNOWN_MODELS)

_BY_NAME: Final[dict[str, TranscriptionModel]] = {model.name: model for model in KNOWN_MODELS}


class UnknownTranscriptionModel(ValueError):
    """A name this deployment will not run.

    Carries both halves of the answer, because a caller who mistyped a
    model name needs to see the list to fix it and there is no second
    request that would show them. The message is built from this
    repository's own literals only -- the offending name is the caller's,
    which is why this exception is turned into an HTTP body rather than a
    log line (see `sturnus.observability.fields`).
    """

    def __init__(self, name: str) -> None:
        known = ", ".join(sorted(KNOWN_NAMES))
        super().__init__(f"unknown transcription model {name!r}; this deployment runs {known}")
        self.name = name


def is_known(name: str) -> bool:
    """Whether this deployment will run a model by this name."""
    return name in KNOWN_NAMES


def describe(name: str) -> TranscriptionModel:
    """The registry entry for one name, or the refusal that names it."""
    model = _BY_NAME.get(name)
    if model is None:
        raise UnknownTranscriptionModel(name)
    return model


def resolve(requested: str | None) -> str:
    """The concrete model a request means, refusing a name nobody has.

    `None` means "nobody chose" and becomes `FALLBACK` here, at the
    boundary, so that nothing downstream has to know what an absent
    choice means. That matters more than it looks: `None` used to travel
    all the way into the column, where it meant "whichever model the
    worker that happens to claim this job was configured with" -- which is
    not a record of anything, and differs between two workers of the same
    fleet. A concrete name makes `transcription_job.requested_model` a
    fact.

    **An unknown name is refused, never replaced.** Substituting the
    fallback would run one model, record the request for another, and
    report success -- and `transcription_job.model` exists precisely so
    that "what actually ran" is knowable. This is the rule the move to a
    remote endpoint did not weaken: the endpoint refusing a name it does
    not serve is a 400 on a job that has already been claimed, while this
    refuses it where a caller can still be told to fix the request.
    """
    if requested is None:
        return FALLBACK
    if requested not in KNOWN_NAMES:
        raise UnknownTranscriptionModel(requested)
    return requested
