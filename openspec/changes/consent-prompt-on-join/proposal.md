# Proposal

## Why

Today a person who joins a recording channel without consent sees nothing. The
bot is in the channel, the channel is recorded for everyone who has granted
consent, and the only signal for the others is an absence: their audio is
dropped by the sink (`infrastructure/discord/sink.py`), they are not counted as
participants, and they cannot speak, because the recording channels deny Speak
to @everyone (`application/setup_plan.py`). Nothing in the channel says why.

To learn what is happening and what to do about it, a person has to already know
that `/consent grant` exists, what the consent text says, and that `/link start`
exists if they want the transcript attributed to their Outline account. Nobody
on the team is told this by the channel itself. A member who simply stays quiet
is not making an informed choice; they are unaware. The failure this prevents is
a team member who wants to take part, joins a recording, cannot speak, and leaves
without ever learning that a two-button prompt would have taken them there.

The channel asks instead. When someone joins a recording channel, the bot posts a
message in that channel's text chat, mentions only that person, and offers the
steps they are missing: consent, and account linking. Each button acts for the
person who presses it, so every member can decide for themselves from the same
message.

Consent is not asked again once it is given. Sturnus serves only the owner's own
team, and a consent stays in force until the person revokes it. It does not
expire with time. A member with active consent is never pinged on a later join,
however long after the last recording, until they revoke it. Consent ends only
through revocation or through a policy version that an administrator has
deliberately changed (`domain/consent.py`, `is_consent_active`). When that
happens, the next join of a member is where they are asked again.

## What Changes

- When a person without active consent, or without a linked account, joins a
  channel the bot records, the bot posts a Components V2 message in that
  channel's text chat that mentions that person and only that person. A person
  with active consent is not pinged; a person with active consent but no link
  is pinged only for the link step.
- The message names what is recorded and where the audio goes, using the same
  transcription host wording as the existing consent prompt, and offers only the
  missing steps as buttons.
- **Link account** replies ephemerally, to the person who pressed it, with a link
  button to that person's own OAuth URL. The URL never appears in the channel
  message, because it would bind the account of whoever clicks to the Discord
  identity of the person mentioned.
- **Consent** opens a modal explaining the consent, with the host and the policy
  version, and a checkbox. Submitting it with the box ticked performs the same
  grant as `/consent grant`. Submitting it without the box ticked records nothing.
- A person is pinged at most once per channel while the channel is occupied. When
  the channel becomes empty, the record resets.
- Once the mentioned person has consented, the message is edited to read
  "@person consented" and stays in the channel without a new ping. The buttons
  shown afterwards are only the steps still missing.
- The bot's `/setup` plan grants it View Channel and Send Messages in recording
  channels. Missing permissions produce a logged event and never an error in the
  voice flow.
- The commands stay: `/consent grant`, `/consent revoke`, `/consent status`,
  `/link start`, `/link status`, `/link remove`.

**Out of scope.** Localisation of the bot's texts (they are English and hard
coded today, and this change follows that). Discord Linked Roles and Role
Connections, which need an additional Discord OAuth flow and belong in a later
change. Video. Direct messages as a channel for the prompt.

## Capabilities

### New Capabilities

- `consent-prompt`: the message a person receives in a recording channel when
  they join it without consent or without a linked account, the buttons it
  offers, and how often it is repeated.

### Modified Capabilities

None. The consent rules themselves do not change: active consent still ends only
through revocation or a changed policy version, as `domain/consent.py`
(`is_consent_active`) already states. This change adds a way to reach those
rules from the channel. The existing `transcription` capability already requires
that a person is told the destination before consenting; the prompt carries that
text forward and does not weaken it.

## Impact

**Code:** `infrastructure/discord/client.py` (`on_voice_state_update`), a new
Components V2 view and modal in `infrastructure/discord/`, the consent and link
use cases in `application/` reused unchanged, `application/setup_plan.py` for the
two additional permissions, and `entrypoints/bot.py` for registering the
persistent buttons at startup.

**Discord permissions:** the bot needs View Channel and Send Messages in each
recording channel. A deployment without them keeps working; the missing prompt is
logged.

**Database:** none. The prompt keeps its short-lived state in process memory; see
design.md.

**Dependencies:** none.

**Telemetry:** one counter of prompts by outcome, without person or channel
identifiers as labels.

**Commit type.** Ships as `feat(bot)`: a new user-visible behaviour in the bot.
No breaking change; every existing command keeps working.
