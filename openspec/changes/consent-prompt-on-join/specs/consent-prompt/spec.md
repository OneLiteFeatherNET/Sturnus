# Spec Delta

## Purpose

What a person sees in a recording channel when they join it without consent or
without a linked account: who is asked, what the message says, which buttons it
offers, how each button acts for the person who presses it, and how often the
prompt is repeated.

## ADDED Requirements

### Requirement: A joining member without consent is asked in the channel

When a non-bot member joins a channel the bot records, and that member has no
active consent, the bot SHALL post a message in that channel's text chat that
mentions that member. The message MUST mention only that member, and MUST name
what is recorded and the transcription host.

#### Scenario: Join without consent

- **WHEN** a member without active consent joins a channel the bot records
- **THEN** the bot posts a message in that channel's text chat mentioning that
  member
- **AND** the message names the transcription host

#### Scenario: Only the joining member is mentioned

- **WHEN** a member without active consent joins while other members are present
- **THEN** only the joining member is mentioned
- **AND** no other member is pinged

#### Scenario: Bot accounts are not prompted

- **WHEN** a bot account joins a recording channel
- **THEN** no prompt is posted

### Requirement: Consent persists until it is revoked or the policy changes

A member with active consent SHALL NOT be pinged for consent again. Consent MUST
remain in force until the member revokes it or an administrator changes the
policy version, and MUST NOT end because of elapsed time.

#### Scenario: Consent persists until revoked

- **WHEN** a member with active consent joins a recording channel again weeks
  after their last recording
- **THEN** no consent prompt is posted for that member

#### Scenario: Revoked consent is asked again

- **WHEN** a member who revoked consent joins a recording channel
- **THEN** the member is prompted again

#### Scenario: Changed policy version is asked again

- **WHEN** an administrator changes the policy version and a member whose consent
  was given under the previous version joins a recording channel
- **THEN** the member is prompted to consent under the new version

### Requirement: The prompt offers only the missing steps

The prompt SHALL offer a consent button to a member without active consent and a
link button to a member without a linked account. A member with active consent
and a linked account MUST NOT be pinged at all. A member with active consent but
no linked account MUST be pinged, and MUST be offered only the link button.

#### Scenario: Member with neither consent nor link

- **WHEN** a member without active consent and without a linked account is prompted
- **THEN** the message offers both the consent and the link buttons

#### Scenario: Member with consent but no link

- **WHEN** a member with active consent and without a linked account joins a
  recording channel
- **THEN** the member is pinged
- **AND** the message offers only the link button

#### Scenario: Member with consent and link

- **WHEN** a member with active consent and a linked account joins a recording
  channel
- **THEN** no message is posted

### Requirement: Link account is answered only to the presser

Pressing the link button SHALL answer ephemerally, visible only to the member who
pressed it, with a link to that member's own account-linking page. The link MUST
NOT be included in the channel message.

#### Scenario: Presser receives their own link

- **WHEN** a member presses the link button
- **THEN** only that member sees a reply containing a link button
- **AND** the link is issued for that member's own account

#### Scenario: Link is not in the channel

- **WHEN** the prompt is posted
- **THEN** no account-linking URL appears in the channel message

### Requirement: Consent is given through a modal with an explicit checkbox

Pressing the consent button SHALL open a modal that explains the recording, names
the transcription host and the policy version, and contains a checkbox labelled
"I agree to my voice being recorded and transcribed". Submitting the modal with
the checkbox set MUST perform the same grant as `/consent grant`.

#### Scenario: Checkbox set grants consent

- **WHEN** a member submits the consent modal with the checkbox set
- **THEN** the consent role is assigned and consent is recorded as `/consent grant`
  records it
- **AND** the member is told that consent is recorded

#### Scenario: Checkbox not set grants nothing

- **WHEN** a member submits the consent modal without the checkbox set
- **THEN** no consent is recorded and no role is assigned
- **AND** the member is told, ephemerally, that no consent was recorded

#### Scenario: Already consenting member

- **WHEN** a member with active consent presses the consent button
- **THEN** no modal is opened
- **AND** the member is told, ephemerally, that consent is already recorded

### Requirement: Buttons act for the member who presses them

Each button SHALL act on behalf of the member who pressed it, never on behalf of
the member mentioned in the message. The buttons MUST keep working after a restart
of the bot.

#### Scenario: Another member presses consent

- **WHEN** a member other than the mentioned member presses the consent button
  and submits the modal with the checkbox set
- **THEN** consent is recorded for the member who pressed the button
- **AND** no consent is recorded for the mentioned member

#### Scenario: Buttons after a restart

- **WHEN** the bot restarts and a member presses a button on a prompt posted
  before the restart
- **THEN** the button responds as it did before the restart

### Requirement: A prompt is repeated at most once per person and occupied channel

The bot SHALL post at most one prompt per member and recording channel for as
long as the channel has a human member. When the last human member leaves the
channel, the record for that channel MUST be cleared, so that the next recording
starts a fresh prompt.

#### Scenario: Rejoin while the channel is occupied

- **WHEN** a member who was already prompted in a channel leaves and rejoins while
  another human member remains in it
- **THEN** no second prompt is posted for that member

#### Scenario: Channel becomes empty

- **WHEN** the last human member leaves a recording channel and a member later
  joins it without consent
- **THEN** the member is prompted again

### Requirement: A prompt for consented members is marked done

When the mentioned member gives consent through the prompt, the bot SHALL edit the
prompt to state that the member consented. The edit MUST NOT ping the member
again. Consent given through the prompt MUST NOT be required for linking, and
linking MUST NOT be required for consent.

#### Scenario: Consent through the prompt

- **WHEN** the mentioned member gives consent through the prompt
- **THEN** the prompt is edited to show that the member consented
- **AND** the member is not pinged again

#### Scenario: Consent without linking

- **WHEN** a member gives consent without a linked account
- **THEN** consent is recorded
- **AND** the prompt remains available for the link step only

### Requirement: Missing send permission is logged, not raised

If the bot lacks the permission to post in a recording channel's text chat, the
bot MUST log the event with the guild and channel identifiers and MUST NOT raise
an error into the voice flow. Recording of members with consent MUST continue.

#### Scenario: Bot cannot post

- **WHEN** a member joins a recording channel in which the bot lacks Send Messages
- **THEN** the event is logged with the guild and channel identifiers
- **AND** recording for consenting members continues

#### Scenario: Setup grants the permissions

- **WHEN** `/setup` configures a recording channel
- **THEN** the bot's role is granted View Channel and Send Messages in that channel

### Requirement: The prompt does not record personal content in logs or metrics

The prompt SHALL log only identifiers and outcome labels. Consent text, account
link URLs, state tokens and display names MUST NOT appear in log lines, and metric
labels MUST NOT contain person or channel identifiers.

#### Scenario: Log lines carry identifiers only

- **WHEN** the prompt is posted, a button is pressed or a permission failure occurs
- **THEN** each log line contains guild and channel identifiers and an outcome label
- **AND** no link URL or state token appears in any log line

#### Scenario: Metric labels are outcomes only

- **WHEN** the prompt counter is incremented
- **THEN** its labels contain an outcome and no person or channel identifier
