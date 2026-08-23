# Sturnus Console — what belongs to a person, and what belongs to an administrator

**Status:** design, being implemented
**Date:** 2026-08-23
**Supersedes parts of:** [2026-08-21-sturnus-console-design.md](2026-08-21-sturnus-console-design.md) §3.3, §4.1, §4.5, §5.2

## 1. What is wrong today

The console has a User View and an Admin View, and the split is in the
wrong place. Everything a person might want to decide *about themselves*
is currently either impossible or filed under Admin:

- **Consent** is administered. A participant can grant it from Discord and
  withdraw it from Discord, and an administrator can withdraw it from the
  console — but the person it belongs to cannot see or change it in the
  console at all. The page that manages it is called `admin/user-settings`,
  which reads as "settings for users" and is in fact "a roster of other
  people's consent".
- **Theme** follows `prefers-color-scheme` and nothing else. Somebody who
  wants the console light while their operating system is dark has no way
  to say so.
- **Language** does not exist. The console speaks British English at
  everybody.
- **There is no profile.** The header carries a Sign out button and no
  indication of who is signed in. `GET /api/me` answers
  `{discord_user_id, is_admin}` — the console can render a snowflake and a
  boolean, and that is the whole of a person's identity in this interface.

And two things make the administrator's own work harder than it needs to
be:

- **Every configured Discord object is a raw snowflake.** An administrator
  sets `voice_channel_id`, `consent_role_id` and `admin_role_id` by
  pasting ids, and reads them back as ids. Nothing in the interface can
  tell them whether `1289374650912837465` is the meeting room or the
  archive.
- **`document_target` is an Outline collection UUID**, obtained by opening
  Outline, navigating to the collection, and picking the identifier out of
  the URL.

## 2. The rule this document establishes

**A person decides what is done with their own participation. An
administrator decides what the bot does, and can see — and stop — what is
being collected. Neither view is a superset of the other.**

That is not a rearrangement of menu entries. It changes which endpoints
exist, what the session carries, and what the consent table records.

| Belongs to the person | Belongs to the administrator |
|---|---|
| Their theme and their language | The bot's runtime configuration |
| Whether they may be recorded at all | Which channels the bot may record in |
| Whether that includes video | The consent roster, as an overview |
| Withdrawing their own consent | Withdrawing somebody's consent, effective from a chosen instant |
| Their own recordings, tags, protocols | The transcription queue and the usage report |

An administrator is also a person. The Admin View is something they have
*in addition*, never instead.

## 3. Identity: what the console may know about a signed-in person

`GET /api/me` grows a display name. It is already in the database:
`account_link.display_name` is the Outline display name, written when the
person linked their account, and the console discards it today at
`sturnus.console.auth` immediately after using the link to find the
Discord id.

It does **not** grow an avatar. An avatar would have to come from Discord,
`api` holds no Discord token, and mirroring every linked person's avatar
into this database to decorate a menu is not a trade this system should
make. The profile control renders initials derived from the display name.

`is_admin` stays what it is: a rendering hint, never a control. Every
administrative endpoint decides for itself, as it does today.

## 4. Per-person preferences

### 4.1 Storage

`user_preference(discord_user_id, key, value, updated_at)`, primary key
`(discord_user_id, key)` — deliberately the same shape as `guild_config`,
for the same reasons: a key/value table takes a new preference without a
migration, and the validation lives in one place on the write path.

The registry is `sturnus.domain.preferences`, pure, stdlib only:

| Key | Values | Default |
|---|---|---|
| `theme` | `system`, `light`, `dark` | `system` |
| `locale` | `en`, `de` | `en` |

`system` is the theme default rather than `light` because the console
already honours `prefers-color-scheme`. A stored default would override
the operating system for somebody who never expressed a preference, which
is the one behaviour nobody asks for and everybody notices.

### 4.2 Endpoints

`GET /api/me/preferences`, `PUT /api/me/preferences/{key}`,
`DELETE /api/me/preferences/{key}` — the last one restores the default
rather than storing an empty value.

**No endpoint takes a user id in the path.** The session decides whose
preferences these are. A path parameter would be an authorisation
question, and there is no reason to have one.

### 4.3 Why the server and not `localStorage`

The console renders on the server. A preference held only in the browser
produces a first paint in the wrong theme and the wrong language on every
navigation that is not a client-side one, and it does not follow the
person to a second device. The locale is additionally mirrored into the
i18n cookie so that the very first server render — before `/api/me` has
answered — is already right.

The sidebar's collapsed state stays in `localStorage`. It is a property of
a window, not of a person.

### 4.4 Theme without a stylesheet

`console/app/assets/css/main.css` defines the role tokens twice today:
once on `:root` and once inside `@media (prefers-color-scheme: dark)`. A
chooser needs a third form — `:root[data-theme="dark"]` — and the light
tokens need to win back under `:root[data-theme="light"]` when the system
is dark.

`console/test/palette.spec.ts` parses that file and asserts WCAG contrast
for every role token in both themes. It must be extended to parse the
attribute-scoped blocks too, and it must fail if a token is defined in one
form and not the others. A theme switch that produces an unreadable
control is worse than no theme switch.

The attribute goes on `<html>` from a Nuxt plugin that runs before
hydration, so there is no flash of the other theme.

## 5. Consent: scope, and revocation from an instant

### 5.1 A scope, because "recorded" is not one thing

`consent` gains `scope`, a text column, values `audio` and `audio_video`,
default `audio`. Existing rows are `audio`, which is exactly what they
were consented for.

`sturnus.domain.consent` gains `may_record_video(...)` alongside
`may_record(...)`. The bot enforces it in the sink, where the role check
already happens, and where video packets are already counted and dropped:
today they are dropped because nothing keeps video at all; after this they
are dropped for a named reason that can be reported.

**Video is not recorded by this change and this document does not propose
that it should be.** What exists today is a probe
(`infrastructure/discord/video_probe.py`) that measures whether Discord
sends video to a bot at all. Recording it is a separate decision with its
own storage, retention and cost questions. The scope is built first
deliberately: a system must be able to record that somebody said no before
it acquires the ability to do the thing they said no to.

### 5.2 Widening a scope requires a policy that covers it

A consent record naming `audio_video` under a policy document that
describes only audio is not consent. Software cannot read the policy, so
it must not pretend to have checked it.

Therefore: a new per-guild setting `video_consent_offered`, default
`false`. While it is false the console does not offer the video option at
all — not disabled, not with a tooltip, absent. An administrator turning
it on is asserting that the document at `policy_url`, at the current
`policy_version`, names video. The interface says exactly that, in a
sentence, at the moment they turn it on.

This is the same construction §1.1 of the console design uses for audio
playback, and it is honest for the same reason: the implementation cannot
enforce it, so it is written down where the person switching it on will
read it.

### 5.3 Narrowing is always allowed; widening is a new grant

A person moving from `audio_video` to `audio` is withdrawing something.
It takes effect immediately and needs nothing.

A person moving from `audio` to `audio_video` is granting something. It
**inserts a new consent row** carrying the guild's current
`policy_version`, because `consent` is an append-only history by design
and because a widened scope under a superseded policy would be exactly the
record this table exists to prevent.

### 5.4 `revoked_at` becomes an effective instant

Today `revoked_at` is a tombstone: any non-null value means "not active",
and it is always stamped with `now()`. That makes "withdraw from the end
of the month" and "withdraw as of the meeting on Tuesday" unexpressible.

`is_consent_active` therefore takes the current time and reads:

```
granted, policy matches, and (revoked_at is null or now < revoked_at)
```

An administrator may set `revoked_at` to any instant not before
`granted_at`. A past instant is a statement about recordings that already
exist; a future one is a scheduled withdrawal.

**A past instant does not delete anything.** The console says how many
recordings with audio fall on or after the chosen instant, and offers the
existing erasure path for them; it does not quietly erase, and it does not
claim the data is gone when it is not. `/audio purge` and the retention
sweep remain the only things that delete audio.

The bot re-reads consent through a five-second cache, so a scheduled
revocation takes effect within five seconds of its instant without any new
mechanism.

### 5.5 What the person sees, and what the administrator sees

The person, at `/settings`: every guild they have a consent record in, its
state, the policy version it names, and the two controls — the scope, and
withdrawing entirely. Withdrawing from the console writes the record; it
cannot remove the Discord role, because `api` holds no Discord token, and
the interface says so rather than leaving the person to discover it.

The administrator, in the Admin View: the roster as it is today, plus the
effective instant on the withdrawal control. The overview is
**read-mostly** — its one write is the withdrawal, and this document
removes nothing else from it, because an administrator who cannot stop
collection is not administering anything.

## 6. Names, everywhere an id is shown

### 6.1 The mirrors

`api` has no Discord token and must never be given one. The established
pattern is `admin_member`: the process holding the credential writes what
the gateway-less process needs to read. This extends it with
`guild_channel`, `guild_role` and `guild_member`, written by the bot on
the sweep it already runs.

`guild_member` mirrors **only the members holding the consent role or the
admin role** — exactly the people this console ever names. Mirroring an
entire guild's member list would be copying a Discord user directory into
a database that exists to hold recordings, and nobody asked for that.

`outline_collection` is written by the worker, which is the process that
holds the Outline API token.

### 6.2 The rule for the interface

**The name is the value; the id is the subtext.** Every control that today
takes a snowflake becomes a picker over the mirror, showing the name, with
the resolved id rendered underneath in the monospace face the console
already uses for keys.

An id with no mirror row renders as the bare id and a short note that it
could not be resolved — never as a blank, and never as a silently dropped
option. A channel deleted in Discord is a configuration problem the
administrator needs to see, not one the interface should hide by omission.

The id stays the stored value and stays a string on the wire. The picker
spares a human a copy-paste; it does not change what is configured.

### 6.3 Outline collections

`document_target` keeps holding a collection id. `GET /api/outline/collections`
serves the mirror, and the settings control becomes a select. A collection
the mirror does not know is shown as its id with the same note, because a
stale mirror must not make a working configuration look broken.

## 7. Where things live in the interface

### 7.1 The profile menu

Top right of the header: a control showing the person's initials and
display name, opening a menu with

- **Settings** → `/settings`
- **Sign out**
- **Two-factor authentication** and **Multi-factor authentication**, both
  rendered as unavailable with a "coming soon" note.

The two disabled entries are deliberate and they are a promise. They are
not links, not buttons that do nothing, and not tooltips — they are
visibly inert rows that say what is coming. An interface that shows a
control which silently does nothing teaches people not to trust its
controls.

Keyboard and screen reader behaviour is not optional: the trigger is a
button with `aria-expanded` and `aria-haspopup="menu"`, the menu is
reachable and dismissible with the keyboard, and focus returns to the
trigger on close. The header already gets this right for the sidebar
toggle; match it.

### 7.2 `/settings` — the person's own page

Sections: **Appearance** (theme), **Language** (locale), **Consent** (per
guild, per §5.5), and a **Security** section holding the same two
coming-soon rows so that the menu's promise has somewhere to land.

`/admin/user-settings` is renamed to `/admin/consents`, because that is
what it is, with a permanent redirect from the old address — the console
already keeps `/settings → /admin/bot-settings` alive for exactly this
reason, and **that redirect must now be removed**, since `/settings` is a
real page again. This is the one breaking change in this document and it
is why it is stated here rather than discovered in a diff.

### 7.3 Quick settings on the dashboard

The dashboard gains a small band of controls chosen by what the reader
actually is:

- **Everyone:** their consent state for the guilds they participate in,
  and the one control that changes it. This is the thing a person is most
  likely to have come to do, and making them find it two clicks deep is
  the current design's mistake repeated in a new place.
- **Administrators, additionally:** the settings a guild changes often
  rather than once — the recording channels, the transcription language,
  the retention window — with the full configuration a click away.

Each control writes through the same endpoint the full page uses. A second
write path is a second set of validation bugs.

## 8. Motion, loading and layout

The console has one skeleton (the dashboard), no page transitions, and
text-based loading states everywhere else. The text states are good and
stay: "Reading this server's queue…" tells a reader more than a spinner.
What is missing is that a *navigation* currently shows nothing at all
between the click and the next page's own loading state.

- A route transition and a top-of-page progress indicator, both short
  enough not to slow anybody down.
- Skeletons matching each page's real layout — the dashboard's is the
  pattern — so that content arriving does not reflow the page.
- Every transition and animation respects `prefers-reduced-motion`. The
  console shows recordings of meetings; somebody with a vestibular
  disorder should not have to brace for the navigation.

Layout: every component responsive with Tailwind utilities, verified from
360 px upwards, and **no stylesheets**. The two remaining `<style scoped>`
blocks — `CalendarDayTimeline.vue` and `CalendarHeatmap.vue` — move to
Tailwind arbitrary-value utilities and inline token bindings, which is
what every other component in this codebase already does. The `@theme`
block and the role tokens in `main.css` stay; they are the design system,
not component styling.

## 9. The queue stops being asked

Covered by its own change: `GET /api/guilds/{guild_id}/queue/stream` and
`GET /api/sessions/{session_id}/queue/stream`, `text/event-stream`,
emitting only on change, heartbeating otherwise, closing when the queue
comes to rest, with the polling endpoints kept for clients that cannot
hold a stream. §5.2 of the console design document, which justifies the
browser-side poll, is superseded.

## 10. The bot, and what "several channels" can honestly mean

A guild names **a list of allowed channels** rather than one. The bot may
record in any of them and follows the one that is meeting; when more than
one holds consenting members it picks deterministically and says which
ones are waiting.

**One bot identity holds one voice connection per guild.** That is a
Discord limit, enforced by discord.py and by this codebase's own
invariant. Recording two channels of one guild at the same moment needs a
second bot identity, which is a deployment decision nobody has taken.

What this work does is remove the *code* reasons it is impossible: runtime
state moves from being keyed by guild to being keyed by `(guild, channel)`,
so a session is a property of a room rather than of a server. After that,
concurrency is a question about tokens and pods, not a rewrite. The
session row already carries `channel_id` and `channel_name`, so the data
model was never the obstacle.

## 11. New persisted data

| Where | What | Why |
|---|---|---|
| `user_preference` | theme, locale, per person | §4 |
| `consent.scope` | `audio` / `audio_video` | §5.1 |
| `guild_channel` | id, name, kind, position | §6.1 |
| `guild_role` | id, name, position | §6.1 |
| `guild_member` | display name, consent- and admin-role holders only | §6.1 |
| `outline_collection` | id, name | §6.3 |
| `guild_config.video_consent_offered` | per guild, default false | §5.2 — no migration, it is a key |

## 12. Delivery

Each its own branch and pull request, squash merged in order:

1. `feat/console-name-mirrors` — the tables above except `consent.scope`, plus the bot and worker sweeps that fill them
2. `feat/console-i18n` — the i18n machinery and the application shell
3. `feat/console-queue-live` — §9
4. `feat/bot-allowed-channels` — §10, first half
5. `feat/api-me-and-preferences` — §3, §4
6. `feat/console-profile-menu` — §7.1, §7.2 shell, §4.4
7. `feat/consent-scope-and-effective-revocation` — §5, both sides
8. `feat/console-name-pickers` — §6.2, §6.3 in the settings page
9. `feat/console-quick-settings` — §7.3
10. `feat/console-i18n-sweep` — the pages 2 left behind
11. `feat/console-motion` — §8
12. `feat/bot-channel-keyed-sessions` — §10, second half

Steps 1 to 4 are independent of each other and were built in parallel.
7 depends on 1 for its migration number. 10 depends on everything that
adds a page, which is why it is late rather than second.

Every step is test-first, and the console keeps its convention that a
decision belongs in a pure module under `app/utils/` where it can be
tested without rendering anything.
