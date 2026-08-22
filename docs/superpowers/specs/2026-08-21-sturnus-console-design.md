# Sturnus Console — design

**Status:** design, being implemented
**Date:** 2026-08-21

## 1. What this is

A web console at `https://sturnus.onelitefeather.dev` where a person who
took part in recorded meetings can see what Sturnus holds about them, play
back the audio of the sessions they were in, and — if they administer the
bot — change its runtime settings without `kubectl` or a Discord command.

It exists because everything Sturnus produces is currently only reachable
three ways: a Discord slash command, an Outline document, or a shell. None
of those lets a participant answer "what was I in, how much did I say, and
what does it sound like".

### 1.1 The consent question, stated plainly

**Audio playback is a wider use of a recording than a transcript is.** The
people in a session consented to being recorded so that a protocol could
be written. Playing their voice back to another participant is not the
same act, even though that participant heard them live in the meeting.

Three things make it defensible, and all three are load-bearing:

- **Only participants of the same session may play its audio.** Not
  administrators-in-general, not anyone with a link. The check is on
  `session_participant`, evaluated per request, per session.
- **Everyone in a session already heard everyone else in it.** The console
  gives back what was in the room, to the people who were in it, and to
  nobody else.
- **It is stated in the policy.** This capability requires a
  `policy_version` bump and a matching change to the document at
  `policy_url` before it is switched on in production, because consent
  under the old wording did not cover it (Section 6 of
  `docs/operations.md` already establishes that bumping `policy_version`
  invalidates consents that name the old one — that is the mechanism, and
  it is deliberate that people re-consent).

An operator switching this on without doing the third of those has a
working console and no lawful basis for its second section. The
implementation cannot enforce that; this document is where it is written
down.

## 2. Architecture

Two new processes, both behind the existing hostname:

```
                    sturnus.onelitefeather.dev  (Cloudflare Tunnel)
                                  │
              ┌───────────────────┼────────────────────┐
              │                   │                    │
         /oauth/callback        /api/*                 /
              │                   │                    │
         sturnus-link       sturnus-api          sturnus-console
      (exists; Discord      (new; read models,   (new; Nuxt SSR,
       account linking)      audio streaming,     Tailwind)
                             settings)
```

`sturnus-api` is a fourth entrypoint in the existing hexagon
(`src/sturnus/entrypoints/api.py`), reusing `application` and `domain`
unchanged. `sturnus-console` is a Nuxt application under `console/` in the
same repository, built into its own image.

### 2.1 What each process may hold

The credential separation the system already has (Spec 13.2) extends
rather than bends:

| Process | Discord token | S3 + master key | OAuth secret | Database |
|---|---|---|---|---|
| `bot` | yes | yes | no | yes |
| `worker` | no | yes | no | yes |
| `link` | no | no | yes | yes |
| `api` | **no** | **yes** | **yes** | yes |
| `console` | no | no | no | **no** |

`api` needs S3 and the master key because it decrypts audio on the way to
the browser. It must never hold the Discord token: it has no gateway, and
a process that can read every recording is not one to also give the
ability to act as the bot.

`console` holds nothing. It renders and calls `api`; every credential and
every authorisation decision lives on the other side of that boundary.

### 2.2 Why a separate console process rather than serving Nuxt from `api`

Because they fail differently and scale differently. A rendering process
that has crashed is an outage of the page; an API process that has crashed
is an outage of the data. Keeping them apart also keeps the Python image
free of a Node runtime, which is the larger of the two by a wide margin.

## 3. Identity and authorisation

### 3.1 Signing in

OAuth against Outline, the same provider `link` already uses and the same
`OutlineOAuth` client. The flow:

1. Browser hits a protected page, has no session, is redirected to
   `GET /api/auth/login`.
2. `api` stores a state row (reusing `oauth_state`, which already has a
   TTL and a purge sweep) and redirects to Outline.
3. Outline redirects back to `GET /api/auth/callback`.
4. `api` exchanges the code for an identity, **looks that identity up in
   `account_link`**, and issues a session cookie naming the Discord user
   id it found.
5. No `account_link` row means no session. The console tells the person to
   run `/link` in Discord first.

That last step is the whole authorisation model in one sentence: **the
console knows a person by their Discord id, and the only bridge from an
Outline identity to a Discord id is a link the person made themselves.**

### 3.2 The session cookie

A signed, `HttpOnly`, `Secure`, `SameSite=Lax` cookie carrying the Discord
user id and an expiry, signed with `STURNUS_SESSION_SECRET` (HMAC-SHA256).
No server-side session store: the cookie *is* the session, so `api`
remains stateless and a restart does not sign everybody out.

`SameSite=Lax` rather than `Strict` because the OAuth callback is a
cross-site navigation and `Strict` would drop the cookie on the hop that
sets it.

Lifetime 12 hours. Long enough for a working day, short enough that a
forgotten open tab is not a standing grant.

### 3.3 Who may see what

| Resource | Rule |
|---|---|
| Own statistics | The signed-in user, always |
| A session's metadata | Only if the user is in its `session_participant` |
| A session's audio | Same, evaluated per request |
| A transcript link | Same |
| Settings | Administrators only |

There is no "list every session" endpoint. Every query is scoped by the
signed-in Discord id at the repository layer, not filtered afterwards in a
handler — a filter that can be forgotten is a filter that will be.

### 3.4 Administrators

`admin_role_id` is a Discord role, and `api` has no gateway to ask about
role membership. A new table `admin_member(guild_id, discord_user_id,
updated_at)` carries it across, written by `bot` — which does have the
`members` intent — on the sweep it already runs, and read by `api`.

The alternative, giving `api` a Discord token, would undo Section 2.1 for
a convenience.

## 4. What the console shows

### 4.1 Layout

- **Top bar:** the Sturnus mark and name, and a burger control that
  collapses the sidebar from labelled entries to icons only. The collapsed
  state persists per browser.
- **Left sidebar:** Dashboard, Recordings, Calendar, Settings (only for
  administrators).
- Dark and light, following the viewer's system preference, in the brand
  palette from `docs/brand/README.md`.

### 4.2 Dashboard

What the signed-in person has actually accumulated:

- Total speaking time across every session (from `job.speech_seconds`).
- Number of sessions attended, and how many produced a protocol.
- Number of distinct people spoken with.
- Longest session, most recent session, first session.
- Words transcribed, as a proxy for how much was actually said.

### 4.3 Recordings

Every session the person was in, newest first. Each row: date, channel,
duration, who else was there, a link to the Outline protocol if one
exists, and a player.

A session has one track per speaker. The player exposes them as separate
channels the listener can solo or mute, because that is what the recording
actually is — mixing them down would destroy the one property that makes
this format worth keeping.

### 4.4 Calendar

A year heatmap: one cell per day, coloured by how much was recorded that
day, with the number of participants in the tooltip. Clicking a day opens
a timeline for it — each session as a bar positioned by clock time, sized
by duration, labelled by channel.

### 4.5 Settings

The runtime configuration from `sturnus.domain.settings`, per guild, with
the same validation the `ConfigStore` write path already applies —
`INTEGER_KEYS` are checked before storage, `REQUIRED_KEYS` cannot be
cleared.

Changing `policy_version` warns, in the interface, that it invalidates
every consent naming the old one. That is documented behaviour and not a
surprise worth discovering in production.

## 5. Audio delivery

`GET /api/sessions/{id}/tracks/{discord_user_id}/audio`

- Authorises against `session_participant` for the signed-in user.
- Streams from S3, decrypting chunk by chunk, converting the raw 48 kHz
  stereo PCM into a WAV stream with a header written up front.
- Supports `Range`, because a browser audio element will ask for one and
  because a listener who wants minute 30 should not have to download
  minutes 0 to 29. The chunked AES-GCM format allows starting at a chunk
  boundary; the handler seeks to the chunk containing the requested byte
  and discards the remainder of it.
- Never writes plaintext audio to disk. The decrypted bytes exist only in
  the chunk buffer on their way to the socket.

### 5.1 What is deliberately not built

**No transcoding.** Opus would be a tenth of the bytes, but it needs
`ffmpeg` in the image and a CPU budget in a process that has neither. WAV
over a compressed HTTP response is enough for playback, and the
alternative is a decode pipeline in the request path.

## 6. New persisted data

Three columns and one table. Everything else the console needs already
exists.

| Where | What | Why |
|---|---|---|
| `transcription_job.audio_seconds` | Track length | The console shows durations; deriving them means downloading from S3 |
| `transcription_job.speech_seconds` | What the gate handed the decoder | The dashboard's central number |
| `transcription_job.segment_count` | Segments the decoder produced | Distinguishes "said little" from "was not transcribed" |
| `admin_member` | Discord admins, mirrored | Section 3.4 |

The worker already computes the first three — they are the metrics
`sturnus_transcription_audio_duration_seconds`,
`sturnus_transcription_total_seconds` and the segment loop's own count.
Persisting them is a write it does not currently make, not a measurement
it does not currently take.

## 7. Delivery

Eleven changes, each its own worktree, branch and pull request, squash
merged in order, released as one version:

1. Persisted durations, `admin_member`, migration, worker and bot writes
2. `api` skeleton: process, health, OAuth login, session cookie
3. Read endpoints: dashboard, sessions, calendar
4. Audio streaming with `Range`
5. Settings endpoints
6. Console skeleton: Nuxt, Tailwind, layout, auth guard
7. Dashboard page
8. Recordings page and multi-track player
9. Calendar heatmap and timeline
10. Settings page
11. Chart components, HTTPRoute, FLUX, rollout

Every step is test-first. The Python side keeps the existing standard —
behaviour-named tests, no self-referential assertions. The console is
tested with Vitest at the component level and the API contract is pinned
from the Python side, which is where it is decided.
