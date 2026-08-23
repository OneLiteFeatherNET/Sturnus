"""The transcription models this deployment will run, and which it defaults to.

Until this module existed, a model name was a free string that travelled
from a re-queue into `transcription_job.requested_model` and out the other
side into `WhisperModel(...)` without anything ever looking at it. That is
not a validation gap in the ordinary sense, because there is no fallback
underneath it: `faster_whisper` resolves an unknown name against
HuggingFace, fails, and the job fails with it. `attempts` climbs, the
worker retries, and at `max_attempts` the speaker is `dead` with no
transcript and an error message about a repository that does not exist.
One typo, four attempts, one lost recording. Nothing anywhere could have
caught it, because nothing anywhere knew what a model name is.

**Closed, and hardcoded, and both on purpose.**

Closed first. `faster_whisper` accepting any HuggingFace repository id is
exactly why the list must be closed rather than why it need not be: a free
string is not "a model name", it is *an instruction to this worker to
fetch and execute arbitrary weights from the internet*, issued through a
re-queue button. The registry is therefore a decision about what this
deployment is prepared to run, in the same spirit as
`sturnus.domain.settings` and `sturnus.domain.preferences` -- both of
which close a value set for the weaker reason that an unknown value
selects no code path. Here an unknown value selects a download.

Hardcoded second, which is the more arguable half. Making the list an
environment variable would move a decision about what this deployment runs
out of review and into a Kubernetes `ConfigMap`, and it would still need
every rule below to be worth anything -- a configurable registry that
accepts whatever it is given is the free string again with more steps. It
would also have to be parsed identically by the console API, the worker
and the bot, three processes that share this module today and would then
share a format instead. The names below are seven lines; adding one is a
commit, a review and a deploy, which is the correct amount of ceremony for
"this cluster will now download and run a new model". If a deployment ever
genuinely needs a per-cluster list, the shape to reach for is this tuple
as the default and configuration that may only *narrow* it.

**Why these seven.** All multilingual. The English-only builds -- the
`.en` variants and `distil-large-v3` -- are deliberately absent: every
guild this serves meets in German (see
`WorkerSettings.whisper_default_language`), so offering them would be
offering a choice that is wrong for this deployment's own material in a
way the chooser could not see from the name.

**Why `large-v3` is the fallback.** It is what
`WorkerSettings.whisper_model` declares as the worker's default, and
`tests/entrypoints/test_worker_entrypoint.py` pins the two together so
they cannot drift. The reasoning for the value itself lives on that field
and is not restated here.

Pure data and three functions, in `domain`, so the endpoint that offers
the choice, the write that stores it and the engine that loads it cannot
disagree about which names exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class TranscriptionModel:
    """One model this deployment will run, and what choosing it costs.

    `approximate_size` and `summary` are not decoration. An administrator
    picking from a dropdown is choosing how long a re-queue takes, how
    much memory the worker needs and how good the transcript will be, and
    none of that is legible from the name -- `large-v3` and
    `large-v3-turbo` differ by roughly half the download and by an amount
    of non-English accuracy that only shows up in the transcript.

    The size is deliberately approximate and deliberately a string. These
    are CTranslate2 conversions whose on-disk size depends on the compute
    type the worker was built with (`int8_float32` here), so a number
    presented as exact would be exact about the wrong thing. It is an
    order of magnitude for a human, not a quota for a scheduler.
    """

    #: The name `faster_whisper` resolves, and the value stored in
    #: `transcription_job.requested_model`.
    name: str
    #: Roughly what the worker downloads and holds for it.
    approximate_size: str
    #: The trade this model makes, in one sentence.
    summary: str


#: What may be asked for, ordered cheapest first. The order is part of the
#: answer -- a dropdown renders this tuple as it stands and adds nothing --
#: so reading down the list is reading the trade from "fast and rough" to
#: "slow and right".
KNOWN_MODELS: Final[tuple[TranscriptionModel, ...]] = (
    TranscriptionModel(
        name="tiny",
        approximate_size="75 MB",
        summary=(
            "The fastest and the least accurate; useful for checking that a recording "
            "contains speech at all, not for a transcript anybody will read."
        ),
    ),
    TranscriptionModel(
        name="base",
        approximate_size="145 MB",
        summary=(
            "Still very fast, still visibly wrong on names and technical terms; a step "
            "up from tiny and no more than that."
        ),
    ),
    TranscriptionModel(
        name="small",
        approximate_size="480 MB",
        summary=(
            "The smallest model that produces German a reader can follow without the "
            "audio beside it, at a few times the speed of large-v3."
        ),
    ),
    TranscriptionModel(
        name="medium",
        approximate_size="1.5 GB",
        summary=(
            "Close to large-v3 on clear speech and noticeably behind it on crosstalk "
            "and accents, for roughly half the time and half the memory."
        ),
    ),
    TranscriptionModel(
        name="large-v2",
        approximate_size="3.1 GB",
        summary=(
            "The previous generation of the large model; worth a second run only when "
            "large-v3 hallucinated on a particular recording, which it sometimes does."
        ),
    ),
    TranscriptionModel(
        name="large-v3-turbo",
        approximate_size="1.6 GB",
        summary=(
            "A distillation of large-v3 with four decoder layers instead of thirty-two: "
            "much faster, and what it gives up is concentrated outside English."
        ),
    ),
    TranscriptionModel(
        name="large-v3",
        approximate_size="3.1 GB",
        summary=(
            "The most accurate model here and this deployment's default; the slowest, "
            "and the one to re-queue against when a transcript is wrong."
        ),
    ),
)

#: The model a re-queue runs when nobody chose one. See the module
#: docstring: this agrees with `WorkerSettings.whisper_model` and a test
#: keeps it agreeing.
FALLBACK: Final = "large-v3"

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
    that "what actually ran" is knowable.
    """
    if requested is None:
        return FALLBACK
    if requested not in KNOWN_NAMES:
        raise UnknownTranscriptionModel(requested)
    return requested
