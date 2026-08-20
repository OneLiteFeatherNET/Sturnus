# End-to-end verification checklist

Covers **Plan 3 Task 11** (`docs/superpowers/plans/2026-08-19-sturnus-03-worker-outline.md`)
and **Plan 4 Task 10** (`docs/superpowers/plans/2026-08-19-sturnus-04-linking-deployment.md`)
in one pass: a full session against a real, deployed Sturnus (bot, worker,
and — if you've reached Plan 4 — the link service), watched by a human from
start to finish. No amount of unit or integration testing substitutes for
this; it's the only place chronology, latency, and the legal gates get
checked against what actually happened in a room, rather than against a
mock.

Work through every box. Where a step belongs to only one of the two plans,
it's marked **[Plan 3]** or **[Plan 4]**; unmarked steps apply either way.
If you're running this before the link service exists, skip the **[Plan 4]**
boxes and note that explicitly in your write-up — this document still
verifies Plan 3 Task 11 on its own.

**If any box in "Legal gates" fails, stop. Do not deploy further, do not
proceed to the next section, and do not treat it as a note-for-later.**
Every other failure in this document is a bug to record and fix in the
normal course of things; these are not.

## 0. Setup — before you start

- [ ] **This is a test guild, not a production server.** A production
  server's members did not sign up to be the first real load-bearing test
  of consent enforcement, audio retention, and erasure commands. If
  there's any doubt whether "the team's usual server" counts as
  production for this purpose, treat it as production and use a
  dedicated test guild instead.
- [ ] **Both participants are informed**, beyond whatever `/consent`'s
  embed says: they know this is a test run of a recording bot, they know
  roughly what will happen to the recording, and they've agreed to take
  part before the session starts. `/consent` being clicked is not the
  same thing as informed participation in a test — the whole point of
  this run is to watch a human reaction to the actual system, not to
  rubber-stamp a UI flow.
- [ ] Every environment variable in `docs/operations.md` §1 is set for
  the bot process (Discord token, database URL, S3 credentials, master
  key) — and for the worker and link-service processes, whatever their
  equivalents turn out to be once those entrypoints exist.
- [ ] The database has current migrations applied.
- [ ] **[Plan 4]** The worker's Whisper model has finished downloading and
  is cached on its volume — confirm before assuming anything else is
  broken; this is the slowest startup step and the most likely to look
  like a failure while it isn't.
- [ ] **[Plan 4]** The link service is reachable at its public URL and
  `/healthz` returns healthy.
- [ ] `/setup` has been run in the test guild: recording voice channel,
  privacy/consent policy URL, policy version, consent role (created or
  reused).
- [ ] A **second, ordinary voice channel** that Sturnus never joins exists
  in the guild alongside the recording channel (`docs/operations.md`
  §3.3) — without one, consenting to the recording channel is the price
  of talking to anyone by voice at all, which is not legally meaningful
  consent. Confirm it's actually usable, not just present.
- [ ] `/config show` reports **"All required keys are set."** — in
  particular `document_target` (the real Outline collection this test
  will write to — not the scratch collection `scripts/verify_outline_api.py`
  uses) and `admin_role_id` are set explicitly; `/setup` does not set
  either.
- [ ] The recording channel's `Speak` permission is denied for
  `@everyone` and allowed for the consent role — confirm in Discord's
  channel settings directly, don't just trust that `/setup` applied it.
- [ ] Both participants have run `/consent` and hold the consent role —
  confirm with `/consent status` for each, not by assumption.
- [ ] **[Plan 4]** Both participants have run `/link` and completed the
  Outline OAuth flow — confirm with `/consent status` (or wherever link
  state is surfaced) before starting, not by reading the document
  afterward and hoping.
- [ ] You have a way to note wall-clock times as the session happens
  (a second device, a shared doc) — you'll need the moment the session
  ends and the moment the link is posted, and those are easy to lose
  track of once you're also one of the participants.

## 1. The run

- [ ] Both participants join the recording channel. Confirm the bot joins
  and posts its announcement.
- [ ] Hold a short conversation (a few minutes is enough) with:
  - [ ] At least one **clear pause** — several seconds of silence from
    both participants, not just a gap between sentences.
  - [ ] At least one **interruption** — one participant starts speaking
    while the other is still mid-sentence, and both continue talking
    briefly before one yields.
  - [ ] Enough back-and-forth that the transcript will contain multiple
    blocks from each speaker, not just one long turn each.
- [ ] Note the **wall-clock time both participants have left** the
  channel (or the empty-grace period has expired and the bot has closed
  the session) — this is your latency measurement's start point.
- [ ] Confirm the session transitions out of `RECORDING` and the worker
  begins transcribing (check `transcription_job` rows, or worker logs, or
  whatever status surface exists).
- [ ] Note the **wall-clock time the link is posted** back to the
  recording channel's text chat — this is your latency measurement's end
  point.

## 2. What to measure

- [ ] **Latency**: time from session end (both participants gone, grace
  period expired) to the link actually appearing in the channel.
- [ ] **Ratio to speaking time**: latency ÷ total time either participant
  was actually speaking (not total session wall-clock — two people
  talking for 4 minutes with long pauses is not the same load as 4
  minutes of continuous speech). Estimate speaking time from the
  transcript's own timestamps once you have it, or from your own notes
  during the run.

Record both numbers in §5 below.

## 3. What to check in the document

Open the posted link and read the whole document before checking
anything off — do not spot-check a paragraph and assume the rest matches.

- [ ] **Chronological order across speakers.** The order blocks appear in
  matches the order things were actually said, including where speaker A
  and speaker B's turns interleave — not just correct within each
  speaker's own blocks considered separately.
- [ ] **Order around the interruption specifically.** This is the case
  most likely to be subtly wrong and least likely to look wrong at a
  glance — check it against your own recollection of who said what
  first, not against the document's internal consistency alone.
- [ ] **A pause reads as a pause.** The gap you left is visible as a gap
  (either in timestamps or in the blocks not being silently merged
  across it) rather than two separate turns sliding together into one
  continuous-looking block.
- [ ] **Timestamps are plausible** against what you actually observed —
  spot-check at least the interruption and the pause against your noted
  times.
- [ ] **[Plan 4]** Both linked participants render as real Outline
  mentions (`@[Name](mention://user/...)`), not plain names.
- [ ] **[Plan 3, if run without linking]** Both participants render as
  plain names with a working Discord profile link
  (`[name](https://discord.com/users/...)`), since neither is linked yet.
- [ ] **[Plan 4]** The mention/notification behaviour matches what
  `scripts/verify_outline_mentions.md` predicted — if that exercise found
  Outline notifies per mention, confirm the template was actually updated
  (a linked participant should be mentioned once, in Participants, and
  shown as plain text in every transcript block) rather than still
  showing the old per-block mention behaviour.
- [ ] The participants list at the top of the document matches who
  actually spoke (or was present, per whatever the participants list is
  scoped to) — no one missing, no one who was never in the channel.

## 4. Legal gates — blocking

**A failure in any box in this section stops the deployment. Fix it before
this runs anywhere near real users, including a second test run — do not
note it and continue.**

- [ ] **An administrator without the consent role who speaks contributes
  nothing to the document.** Have an admin account without the consent
  role join the recording channel and speak. Confirm afterward: no block
  attributed to them anywhere in the document, and ideally no audio of
  theirs reached S3 at all (`VoiceReceiveAdapter` should have dropped the
  packets before they were ever written).
- [ ] **`/audio delete` removes what it claims.** Run `/audio delete` as
  one of the session's participants. Confirm the reply's count matches
  what actually existed, and confirm the underlying S3 object(s) are
  actually gone — not just a database flag — and that
  `audio_deleted_at` is stamped on the corresponding `transcription_job`
  row(s).
- [ ] **[Plan 4]** `/consent revoke` removes the role, and a session
  started after revocation excludes that person entirely (their audio is
  never captured, not merely excluded after the fact).
- [ ] **[Plan 4]** `/audio purge <user>`, run by an admin against a named
  user, behaves the same as `/audio delete` for that user's own
  recordings.
- [ ] **No transcript content, audio, token, or key appears in any log
  line from any pod** — bot, worker, and (Plan 4) link service. Grep the
  actual logs from this run for anything that looks like transcript text,
  a base64 audio blob, the Discord token, the Outline API token or OAuth
  client secret, or the master key. A log line naming a user id, a job
  id, or a status code is fine; a log line containing what someone said,
  or the bytes of what they said, or a credential, is not.

## 5. What to record afterward

Every one of these is an **estimate in the spec today** — this is the
first time real numbers exist for any of them. Record what you actually
measured, not a plausibility check against the estimate.

- [ ] Measured latency (§2) and its ratio to speaking time: ____________
- [ ] Transcription quality on real German speech — a subjective read is
  fine (e.g. "correct except for two proper nouns"), but be specific
  about what was wrong, not just "mostly good": ____________
- [ ] CPU and memory for the bot pod under load: ____________
- [ ] CPU and memory for the worker pod under load (this is the one most
  likely to differ sharply from the spec's estimate, since transcription
  is the heaviest step): ____________
- [ ] **[Plan 4]** CPU and memory for the link-service pod under load:
  ____________
- [ ] Actual recording size per speaker-hour (extrapolate from this
  session's recording size and its speaking-time measurement from §2):
  ____________
- [ ] Anything else that surprised you, whether or not it fits one of the
  categories above.

This is what tells Plan 4 (or, if you're already there, whoever sizes the
next deployment) what to actually size resource requests for — the
numbers in the spec were reasoned guesses, and this write-up is what
replaces them with reality.
