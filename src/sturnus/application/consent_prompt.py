"""Decisions behind the consent prompt posted in a recording channel.

Nothing here touches Discord. `needs_prompt` says which steps a joining
member is still missing, and `PromptRecord` remembers who was already asked
in which channel, so one occupied channel never pings the same person twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sturnus.domain.consent import ConsentRecord, is_consent_active


@dataclass(frozen=True)
class PromptNeeds:
    """Which steps of the prompt a member still has to take."""

    consent: bool
    link: bool

    @property
    def any(self) -> bool:
        return self.consent or self.link


def needs_prompt(
    record: ConsentRecord | None,
    *,
    linked: bool,
    policy_version: str,
    now: datetime,
) -> PromptNeeds:
    """Consent is missing unless active; linking is missing unless linked.

    Consent and linking are independent (spec: neither use case reads the
    other's state). `now` is threaded into `is_consent_active` and never read
    here: there is no expiry by elapsed time, only revocation and a changed
    policy version.
    """
    return PromptNeeds(
        consent=not is_consent_active(record, policy_version, now),
        link=not linked,
    )


#: What the record stores against a (channel, person) pair. Opaque to this
#: module: the Discord layer decides what it is (a message id, typically).
MessageRef = object


@dataclass
class PromptRecord:
    """In-memory set of (channel id, person id) pairs already prompted.

    Process-local on purpose: a restart loses it, and the cost is one repeated
    ping per person. The optional message reference lets the later
    "consented" edit find the prompt it changes.
    """

    _entries: dict[tuple[int, int], MessageRef | None] = field(default_factory=dict)

    def should_prompt(self, channel_id: int, person_id: int) -> bool:
        return (channel_id, person_id) not in self._entries

    def mark(self, channel_id: int, person_id: int, message_ref: MessageRef | None = None) -> None:
        self._entries[(channel_id, person_id)] = message_ref

    def message_ref(self, channel_id: int, person_id: int) -> MessageRef | None:
        return self._entries.get((channel_id, person_id))

    def clear_channel(self, channel_id: int) -> None:
        """Drops every pair for a channel, called when its last human leaves."""
        for key in [key for key in self._entries if key[0] == channel_id]:
            del self._entries[key]
