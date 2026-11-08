# Design

## Context

`SturnusClient.on_voice_state_update` (`infrastructure/discord/client.py`) already
sees every join and leave, and `_sync_participants` already counts members with
the consent role. Neither tells the person who joined anything. The consent
prompt (`ConsentView` and `CONSENT_STRINGS` in `infrastructure/discord/views.py`)
exists only behind `/consent grant`, and the link flow (`/link start` in
`infrastructure/discord/link_cog.py`) issues a personal OAuth URL with a one-time
state of 15 minutes (`db/link_state.py`). Both flows already have use cases in
`application/`, so the prompt reuses them and adds presentation only.

The consent constraints are the ones the project names. Consent is granted by a
role and recorded append-only (`record_grant`, table `consent`); it is active
until revoked or until the policy version changes (`is_consent_active`). The
telemetry constraint applies to everything this change logs and counts.

## Decisions

**1. Trigger and recipients.** The prompt is posted when a non-bot member's
voice state changes from no channel, or another channel, into a channel the bot
records. A person is pinged only if they lack active consent, or if they have it
but no linked account. Someone with active consent and a link is never pinged.
Consent is never asked again until it is revoked or the policy version changes;
a join weeks later by a member who still has active consent produces no message.
When the policy version changes, the next join of each member is where the prompt
asks them again, because `is_consent_active` returns false for every older
version.

**2. Where it is posted.** The text chat of the voice channel itself. Discord
voice channels carry a text chat that the bot's send permission covers, and
the message stays next to the recording it concerns. Direct messages were
rejected (see Alternatives).

**3. One ping per person and channel, while the channel is occupied.** The record
is a set of (channel id, person id) pairs held in process memory. A person who
leaves and rejoins while the channel still has a human in it is not pinged
again. When the last human leaves the channel, all pairs for that channel are
dropped, so the next recording starts with a fresh prompt. This matches the
team's reasoning: a recording starts only with a consenting person, so "once per
recording" would never remind anyone in an empty channel. Restarting the bot
loses the set. The consequence is one repeated ping per person after a restart,
which is acceptable for a single bot pod. The set is not persisted, because a
database row for a reminder would add a table and a retention question for no
benefit to consent.

**4. Buttons act for the presser.** Each button's `custom_id` names only the
action, not a person. Consent and link act on `interaction.user`. Every member of
the channel can press the same message for themselves, and the mentioned person
is no more special than anyone else. Because the custom ids are static, the
buttons are registered as `ui.DynamicItem` subclasses matched by a regex
(`sturnus:consent-prompt:consent`, `...:link`), so they keep working after a
restart without a database row.

**5. Link account is ephemeral and personal.** Pressing "Link account" sends an
ephemeral message to the presser that contains a link button to a freshly issued
personal OAuth URL, produced by the same code path as `/link start`. The URL is
never placed in the channel message. A URL in the public message would be
clicked by whoever sees it first, and the callback would then bind that
person's Outline account to the Discord identity of the mentioned member.

**6. Consent opens a modal with a checkbox.** Pressing "Consent" calls
`send_modal` as the first response, within Discord's 3 seconds. The modal holds
a Text Display with the explanation, the transcription host and the policy
version, and a checkbox in a Label titled "I agree to my voice being recorded and
transcribed". Submitting with the checkbox ticked runs the same use case and
error handling as `/consent grant`: the role is assigned, then `record_grant` is
written, and the same messages are shown on failure. Submitting without the tick
writes nothing and answers ephemerally that no consent was recorded. If the
presser already has active consent, the button answers ephemerally that consent
is already recorded and no modal is opened.

**7. What the message shows after each step.** The message keeps only the
steps still missing as buttons. Once the mentioned person has consented, the
message is edited to "@person consented". The edit uses the message reference
kept in memory with the set from decision 3. If the reference is gone after a
restart, the edit is skipped and the stale message keeps its buttons, which then
answer "consent already recorded" to anyone who presses them; nothing is lost.
Linking alone changes only the buttons: a person who has linked but not
consented sees the consent button only, and a person who has consented but not
linked still sees the link button under the "consented" line. Linking does not
block consent and consent does not require linking (decision 8).

**8. Consent and link are independent.** Neither use case reads the other's
state. Consent does not require an Outline account. A person may record without
attribution, and the link step remains optional in the prompt.

**9. Permissions and failure.** `setup_plan.py` adds View Channel and Send
Messages for the bot's own role in every recording channel. If posting the prompt
fails with a Discord permission error, the failure is logged as an event with the
guild and channel ids and the outcome label `no_permission`. The voice flow does
not wait for the post and does not raise, so recording continues for consenting
members exactly as before.

## Privacy and consent effects

**Public content.** The channel message names only the person mentioned, and
says only that they have not yet consented or linked. It contains no audio, no
transcript and no recording state beyond what the existing consent prompt
already shows. Everyone in the channel can see that a person has not consented;
the team accepted that trade-off because the alternative leaves the person
unaware of the recording.

**Logging.** Log lines carry guild, channel and person ids and an outcome label,
in lazy `%s` style (Ruff G/LOG). No consent text, link URL, state token or
display name is logged, and no f-string reaches the logger.

**Telemetry.** One counter, `consent_prompt_total`, labelled by outcome
(`posted`, `skipped_consented`, `skipped_already_pinged`, `no_permission`,
`consented_via_prompt`, `link_started`). Person and channel ids are not metric
labels, both for cardinality and so that a metrics backend never receives
Discord identities.

**Consent model.** The change adds no write path to `consent`. Every grant still
goes through `record_grant`, which stays append-only, and the policy version
written with it is the one shown in the modal. Revocation and policy-version
changes keep their existing meaning; the prompt is a second route to the same
grant, not a new rule.

## Alternatives considered

- **Direct message instead of a channel ping.** Rejected. A DM can be missed or
  blocked, it is invisible to the others in the channel, and it does not show the
  person which recording they are being asked about. It also requires the person
  to share a server with the bot and to allow DMs from it.
- **Putting the personal OAuth URL in the channel message.** Rejected, for the
  binding reason in decision 5.
- **A modal with only a "Confirm" button and no checkbox.** Rejected. A button
  press is not a separate acknowledgement of the recording and the transcription
  host. The checkbox makes the agreement an explicit act inside the dialog, and
  the existing consent record is written only after it.
- **Persisting the ping set in the database.** Rejected. It would add a table and
  a retention question for a reminder, and the only cost of losing it is one
  repeated ping after a restart.
- **Pinging once per recording.** Rejected. A recording starts only with a
  consenting person, so in a channel where nobody has consented yet a
  per-recording rule would never ask anyone.
- **Discord Linked Roles or Role Connections instead of a link prompt.** Rejected
  for this change. They need an extra Discord OAuth flow and a role-connection
  schema; they belong in a later change.
- **Time-based re-consent.** Rejected and not planned. A consent lasts until it is
  revoked or the policy version changes, as the team decided.

## Risks

- **Repeated ping after restart.** One extra ping per person and channel. The
  owner accepts it; it is noted in the pull request.
- **Stale buttons after restart.** Old messages keep working buttons that report
  "already recorded" when pressed. Harmless, and tested.
- **Discord rate limits.** A prompt is posted only on a join, so the rate is
  bounded by joins. Posting is not retried in a loop; a failed post is logged and
  not repeated until the next join.
- **Modal timing.** The consent button must open the modal within 3 seconds, so
  the modal is sent before any await that touches the database.
- **Component V2 limits.** The message has no `content`; mentions are only
  delivered through the Text Display and `allowed_mentions` set to the one user.
  The test asserts both.

## Migration

No database change and no data migration. Deployment adds the two permissions to
recording channels through `/setup`; a deployment that does not re-run `/setup`
keeps the old behaviour with a logged `no_permission` outcome until it does. The
commands keep working throughout, so rollback is a redeploy of the previous image.

## Open Questions

None. The team decided that the message stays in the channel and is marked as
done ("@person consented") rather than deleted, so the others see who is now
recorded.
