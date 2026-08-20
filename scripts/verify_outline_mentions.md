# Verifying Outline's mention-notification behaviour

Spec 8.3's open question, restated: does Outline send **one notification per
`@[...](mention://user/...)` occurrence**, or **one notification per document
and mentioned user**, no matter how many times that user is mentioned inside
it?

This matters because `src/sturnus/infrastructure/documents/outline_template.md.j2`
currently mentions a linked speaker in **every transcript block** they appear
in — including the participants list at the top. Spec 8.3 already warns that
in a two-hour protocol, one person may show up in hundreds of blocks. If
Outline notifies per mention, the bot becomes a spam source the first time it
runs against a real conversation. If it notifies per document and user,
today's template is already safe and needs no change.

This cannot be settled from documentation — it depends on the running
instance's notification implementation, not on anything in Outline's public
API reference. It requires a live instance, a scratch collection, and **two
separate Outline user accounts**: one to create documents (the "author"),
and a different one to be mentioned (the "target"). Outline — like most
mention systems — does not notify you for mentioning yourself, so running
this with one account produces a false negative, not an answer.

## Step 0: make sure notifications can be observed at all

Log in as the **target** account and open **Settings → Notifications**.
Confirm the mention/document-mentioned-you notification is enabled, for
whichever channels the instance offers (in-app bell, email). If it's off,
turn it on for the duration of this test. Note which channel(s) you're
watching — you'll check the same one(s) after each document below.

If the target account has an email address you can actually check, prefer
watching email: the in-app bell can coalesce or lazily update in ways that
make counting harder to trust than a stack of separate emails.

## Step 1: create Document A — one mention, baseline

As the **author** account, create a document in the scratch collection
(same collection id you'd pass to `scripts/verify_outline_api.py`) with a
body containing **exactly one** mention of the target user, using the exact
syntax the template produces:

```markdown
**10:00:00** · @[Target Name](mention://user/<target-user-id>) ([discordname](https://discord.com/users/123456789012345678))

Some transcript text here.
```

Publish it. Record the wall-clock time you published it.

## Step 2: observe Document A

Wait two or three minutes, then check the target account's notification
channel(s) from Step 0. Record how many distinct notifications arrived that
reference Document A. This should be **1** — if it's 0, notifications
aren't reaching you (go back to Step 0) rather than proof of anything about
per-mention vs per-document behaviour.

## Step 3: create Document B — the same mention, repeated

As the **author** account, create a second document in the same scratch
collection. This time, mention the **same target user five separate times**,
each in its own block, reproducing the exact shape
`outline_template.md.j2` produces for a speaker who talks in five
non-consecutive transcript blocks:

```markdown
## Participants

@[Target Name](mention://user/<target-user-id>) ([discordname](https://discord.com/users/123456789012345678))

## Transcript

**10:00:00** · @[Target Name](mention://user/<target-user-id>) ([discordname](https://discord.com/users/123456789012345678))

First thing they said.

**10:01:40** · @[Target Name](mention://user/<target-user-id>) ([discordname](https://discord.com/users/123456789012345678))

Second thing they said, after someone else spoke in between.

**10:04:12** · @[Target Name](mention://user/<target-user-id>) ([discordname](https://discord.com/users/123456789012345678))

Third thing.

**10:07:55** · @[Target Name](mention://user/<target-user-id>) ([discordname](https://discord.com/users/123456789012345678))

Fourth thing.

**10:09:03** · @[Target Name](mention://user/<target-user-id>) ([discordname](https://discord.com/users/123456789012345678))

Fifth thing.
```

That's six mentions total (one in Participants, five in Transcript) — six is
fine; the exact count doesn't matter, only that it's clearly more than
Document A's one, and that it mirrors the template's actual output shape
rather than an artificial test string. Publish it. Record the time.

## Step 4: observe Document B

Wait the same two or three minutes, then check the same notification
channel(s) again. Record how many distinct notifications arrived that
reference Document B specifically (not Document A — make sure you can tell
them apart, e.g. by document title).

## Step 5: tell the two behaviours apart

Compare the two counts:

| Document A (1 mention) | Document B (6 mentions) | Conclusion |
|---|---|---|
| 1 notification | 1 notification | **Per document and user.** Outline collapses every mention of the same user in one document into a single notification. Current template behaviour is already safe. **No template change needed.** |
| 1 notification | more than 1 (ideally 6, but "more than 1" is enough to conclude) | **Per mention.** Each occurrence sends its own notification. **Template change required** — see below. |
| 1 notification | 1 notification, but arrived noticeably later or batched with others | Outline may be debouncing/digesting notifications rather than deduplicating by document. Worth a note in the write-up, but for this bot's purposes (a handful of sessions a day, not a burst) this still behaves like "per document" from the recipient's point of view — treat as the first row. |

If the result is ambiguous (e.g. 2 notifications for Document B, not 1 and
not 6), don't force it into either row — that shape suggests something
document-specific (maybe a retry, maybe a digest window boundary) rather
than either hypothesis cleanly, and is worth re-running once with a longer
wait and a cleaner room (no other test documents mentioning the target
around the same time) before concluding anything.

## What to change in `outline_template.md.j2` if it turns out to be per mention

The file is
`src/sturnus/infrastructure/documents/outline_template.md.j2`. It currently
has two places that render a speaker, each with the same conditional shape:

```jinja
{% if speaker.external_user_id %}@[{{ speaker.external_display_name | md }}](mention://user/{{ speaker.external_user_id | md }}) ([{{ speaker.discord_display_name | md }}](https://discord.com/users/{{ speaker.discord_user_id }})){% else %}[{{ speaker.discord_display_name | md }}](https://discord.com/users/{{ speaker.discord_user_id }}){% endif %}
```

— once in the `## Participants` loop (over `participants`), once in the
`## Transcript` loop (over `blocks`, as `block.speaker`).

**The participants loop does not change.** Each participant appears exactly
once there by construction — one row per person in `transcript.participants`
— so it already produces at most one mention per person, regardless of how
many times they speak. This is naturally the "first mention" a reader (and
Outline's notifier) encounters, since Participants renders before Transcript.

**The transcript loop's `{% if %}` branch is deleted.** Every block renders
the plain-text form unconditionally — the same markup already used today
only for unlinked speakers — regardless of whether `block.speaker.external_user_id`
is set:

```jinja
{% for block in blocks %}
**{{ block.time }}** · [{{ block.speaker.discord_display_name | md }}](https://discord.com/users/{{ block.speaker.discord_user_id }})

{{ block.text | md }}

{% endfor %}
```

The effect: a linked participant is mentioned exactly **once** per document
— in the Participants list — and every transcript block, theirs or anyone
else's, always shows the plain Discord-linked name. This matches Spec 8.4's
own documented fallback ("render only a speaker's first mention as an actual
mention, and all subsequent ones as plain text with a Discord link") using
the Participants entry as that first mention, and keeps the change to
exactly the one conditional in the transcript loop — the participants loop,
`document_title`, `render_transcript`, and everything in
`sturnus.application.documents` are untouched.

After making that change, update `tests/application/test_document_rendering.py`
(`test_a_linked_speaker_is_rendered_as_a_mention` currently asserts a block
with a linked speaker contains `@[Max Example](mention://user/9c8b)` — that
assertion would need to move to a participants-list check instead, since the
transcript block for that same speaker would no longer contain it) to match,
and update Spec
8.3's example block to match. Also update this file's own "no template
change needed" framing above to record which branch of Step 5 you actually
hit, and on which Outline version, so the next person doesn't have to redo
this exercise.
