"""Everything the console can decide about a configuration key without asking anything.

Which keys exist, what metadata each carries, whether one may be cleared,
and what has to happen before the running system is actually using a new
value — all of it is a function of `sturnus.domain.settings` and
`sturnus.application.reconfigure`, so all of it lives here rather than
inside a request handler. `tests/console/test_settings_view.py` therefore
needs no database, no server and no Discord gateway, and the handlers in
`routes_settings` are left with parse, authorise, delegate, serialise.

Three things this module is careful *not* to be:

**Not a second validator.** `integer` below is a rendering hint so the
console can put a number field on the page. What actually refuses `"soon"`
for `idle_timeout_minutes` is `ConfigStore.set`, and the API turns its
`ValueError` into a 400. Two copies of a validation rule is how the two
drift, and the copy nobody exercises is always the one that goes stale.

**Not a second key list.** `KNOWN_KEYS` is the union `ConfigStore.set`
validates against, computed from the same registry rather than typed out
again. A console offering a key the store refuses renders a field that can
never be saved; one hiding a key the store accepts makes a setting
reachable only from Discord.

**Not optimistic about what a write achieves.** The console's API process
holds no Discord token and cannot reconcile anything (Spec 13.2), while
`/config set` writes *and* reconciles before it replies. So every view
carries `takes_effect`, derived from the three ways the bot reads a key:

* `IMMEDIATELY` — read per use, by a permission check, the consent cache
  or the worker. Nothing caches it, so nothing has to notice.
* `NEXT_RECONCILE` — held in the bot's memory and refreshed by
  `SturnusClient._tick_guild`, which runs per guild every ten seconds.
* `PROCESS_RESTART` — read once at start (`RESTART_REQUIRED_KEYS`). No
  amount of waiting will land it.

and `deferred_while_recording` for the identity keys, which a reconcile
holds back for as long as a session lasts rather than dropping a live
voice connection. A recording is never discarded to make a setting apply
sooner; the console cannot see whether one is in progress, so it says the
wait is possible rather than pretending it is not.

**Not a second opinion about the deprecated key, either.** `voice_channel_id`
and the `voice_channel_ids` that replaced it are one setting with two
names. Both are rendered — a guild cannot be moved off a key the console
will not show it — and both are described through
`settings.canonical_key`, so the console cannot end up calling one of them
immediate and the other deferred. Which of the two the bot actually reads
is `settings.recording_channel_ids`' answer, and this module does not
duplicate it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sturnus.application.reconfigure import (
    IDENTITY_KEYS,
    RESTART_REQUIRED_KEYS,
    TUNABLE_KEYS,
)
from sturnus.domain import settings

#: Every key `ConfigStore.set` accepts — the registry itself, not a second
#: union assembled here. `REQUIRED_KEYS` and `DEFAULTS` are disjoint by
#: construction (a required key is required precisely because it has no
#: default), which is what makes "may this be cleared?" a question with a
#: total answer; `LEGACY_KEYS` joins them as a third disjoint class, keys
#: that are read and writable but required of nobody.
KNOWN_KEYS: frozenset[str] = settings.KNOWN_KEYS

#: The three answers to "when does this write reach the running system?".
#: Strings rather than an enum because they cross a JSON boundary and the
#: front end has to match on them; naming them here keeps the spelling in
#: one place on this side of it.
IMMEDIATELY = "immediately"
NEXT_RECONCILE = "next_reconcile"
PROCESS_RESTART = "process_restart"

#: The one key whose change invalidates existing consent. Bumping it makes
#: every stored consent record naming the old version inactive —
#: `sturnus.domain.consent.may_record` compares the record's version
#: against the guild's current one — so role-holders stop being recorded
#: mid-session within the consent cache's TTL (`docs/operations.md`
#: section 6). That is documented, intended behaviour and the operator is
#: allowed to do it. What they must not do is find out afterwards.
_CONSENT_INVALIDATING_KEYS: frozenset[str] = frozenset({settings.POLICY_VERSION})

#: Keys the bot holds in memory and refreshes on its reconcile pass.
#:
#: Membership is asked through `settings.canonical_key`, because
#: `voice_channel_id` and `voice_channel_ids` are one setting with two
#: names: the deprecated spelling is read on exactly the same pass and
#: deferred behind exactly the same recording, and rendering it as
#: "immediate" would be the console's own version of the lie `/config set`
#: exists to avoid.
_RECONCILED_KEYS: frozenset[str] = frozenset(IDENTITY_KEYS) | frozenset(TUNABLE_KEYS)


@dataclass(frozen=True)
class KeyView:
    """One configuration key as the console renders it.

    `value` is the *effective* value — what the bot would read — so it is
    the stored value if there is one, the default if there is not, and
    `None` for a required key that has never been set. That last state is
    real and has to be rendered: a required key has no default to fall
    back on, which is exactly what makes it required.
    """

    key: str
    value: str | None
    default: str | None
    required: bool
    may_clear: bool
    integer: bool
    invalidates_consent: bool
    takes_effect: str
    deferred_while_recording: bool

    def as_json(self) -> dict[str, object]:
        """The wire shape. Values stay strings, as they are in the column.

        No coercion of `"45"` to `45` for an integer key: the column is
        `Text`, the bot parses it, and a JSON number here would mean the
        console and the store disagreed about what was stored.

        `may_clear` travels beside `required` rather than being left for
        the reader to infer from it. They agreed while there were two
        classes of key and stopped agreeing the moment there were three:
        `voice_channel_id` is required of nobody and clearable by nobody.
        A front end deriving the button from `required` would offer a
        Clear that `clear_setting` answers 409 to, on the same page that
        has just said the field is optional -- so the rule the endpoint
        enforces is the one that is sent.
        """
        return {
            "key": self.key,
            "value": self.value,
            "default": self.default,
            "required": self.required,
            "may_clear": self.may_clear,
            "integer": self.integer,
            "invalidates_consent": self.invalidates_consent,
            "takes_effect": self.takes_effect,
            "deferred_while_recording": self.deferred_while_recording,
        }


def is_known(key: str) -> bool:
    """Whether this is a key anything in the system reads.

    The gate on every write. Without it `guild_config` is a table that
    anybody holding a session and one guild can put arbitrary rows into —
    `ConfigStore.set` refuses them too, but a 404 is the honest answer to
    "configure the key that does not exist", and letting the store answer
    it would report a bad request for a wrong URL.
    """
    return key in KNOWN_KEYS


def may_clear(key: str) -> bool:
    """Whether clearing this key restores something rather than removing it.

    Asked of `DEFAULTS` rather than "not required", because there are now
    three classes of key rather than two. A required key has no default, so
    clearing it does not fall back — it takes the guild out of service
    until somebody sets it again. A legacy key (`voice_channel_id`) has no
    default either, and clearing it from a page would take a guild that has
    not moved to `voice_channel_ids` yet out of service in exactly the same
    way, while looking like tidying up. Both are refused here.

    `/config clear` on this branch will happily do either, and that
    difference is deliberate rather than an oversight. A slash command is
    typed by an administrator who is looking at the reply; a web form is a
    button next to every field.
    """
    return key in settings.DEFAULTS


def takes_effect(key: str) -> str:
    """How long before the running system uses a new value for this key."""
    canonical = settings.canonical_key(key)
    if canonical in RESTART_REQUIRED_KEYS:
        return PROCESS_RESTART
    if canonical in _RECONCILED_KEYS:
        return NEXT_RECONCILE
    return IMMEDIATELY


def describe(key: str, stored: Mapping[str, str]) -> KeyView:
    """One key, against a guild's effective configuration.

    `stored` is `ConfigStore.snapshot`'s output: the defaults with the
    guild's own rows layered over them, exactly as `ConfigStore.get`
    resolves a single key. Reading it from a mapping rather than a store
    keeps this function pure and keeps the listing endpoint down to one
    query instead of one per key.
    """
    return KeyView(
        key=key,
        value=stored.get(key),
        default=settings.DEFAULTS.get(key),
        required=key in settings.REQUIRED_KEYS,
        # The same function `clear_setting` asks, not a restatement of it:
        # a view whose verdict can differ from the endpoint's is a page
        # offering a button that answers 409.
        may_clear=may_clear(key),
        integer=key in settings.INTEGER_KEYS,
        invalidates_consent=key in _CONSENT_INVALIDATING_KEYS,
        takes_effect=takes_effect(key),
        deferred_while_recording=settings.canonical_key(key) in IDENTITY_KEYS,
    )


def describe_all(stored: Mapping[str, str]) -> tuple[KeyView, ...]:
    """Every key the system reads, in a stable order.

    Sorted because `KNOWN_KEYS` is a frozenset and offers no ordering of
    its own: unordered, the same configuration renders differently on
    every page load, which makes the page unreadable and a screenshot in
    a bug report worthless.

    Driven by the registry rather than by `stored`, so a row that reached
    `guild_config` by a direct `UPDATE` under a name nothing reads is
    simply not shown — rather than appearing as a field the API would then
    refuse to save.
    """
    return tuple(describe(key, stored) for key in sorted(KNOWN_KEYS))
