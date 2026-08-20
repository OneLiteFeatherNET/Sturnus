# Outline mention notifications: once per document, or once per occurrence?

Status: settled by source reading against the exact version the OneLiteFeather
instance runs. Not settled empirically — see "What is still unverified".

Date of investigation: 2026-08-20.

## The question

The Sturnus protocol document attributes every speaking block to its speaker
using an Outline mention, `@[Display Name](mention://user/<uuid>)`. In a
two-hour session one person can appear in several hundred blocks. Spec 8.3
asks whether Outline sends one notification per mention occurrence or one
notification per document and user, because the answer decides the shape of
`src/sturnus/infrastructure/documents/outline_template.md.j2`: if Outline
notified per occurrence, only a speaker's first block could be rendered as a
mention and all later blocks would have to degrade to a plain Discord link.

## The answer

**One notification per mentioned user per publish, regardless of how many times
that user is mentioned in the document.** Three hundred blocks by one speaker
produce exactly one `documents.mentioned` notification for that speaker.

Consequence for the template: **the Spec 8.3 fallback is not required.** Every
block may render as a full mention. The template as it stands today already
does this and needs no change.

## Evidence

Everything in this section is quoted source code, fetched from
`raw.githubusercontent.com` at the tag `v1.9.1`, which is the version the
OneLiteFeather instance runs. Nothing here is paraphrase.

### 1. Which code path runs

Sturnus writes the protocol with a single `POST /api/documents.create` carrying
`publish: true`. That is one place in the code: `_CREATE_DOCUMENT_PATH =
"/api/documents.create"` in
`src/sturnus/infrastructure/documents/outline.py`, called once from
`src/sturnus/application/worker.py`. A published document emits a
`documents.publish` event, which `NotificationsProcessor` routes to
`DocumentPublishedNotificationsTask`. The revision path
(`RevisionCreatedNotificationsTask`) is only reached if the document is later
updated, which Sturnus does not do.

### 2. The mention list is *not* deduplicated by user

`ProsemirrorHelper.parseMentions` (v1.9.1,
`server/models/helpers/ProsemirrorHelper.tsx:164-189`) collects mention nodes
and skips duplicates via `seenIds`, but `seenIds` is keyed on the mention
*node* id:

```ts
static parseMentions(doc: Node, options?: Partial<MentionAttrs>) {
  const mentions: MentionAttrs[] = [];
  const seenIds = new Set<string>();

  doc.descendants((node: Node) => {
    if (node.type.name === "mention") {
      if (
        !(options?.type && options.type !== node.attrs.type) &&
        !(options?.modelId && options.modelId !== node.attrs.modelId) &&
        !seenIds.has(node.attrs.id)
      ) {
        seenIds.add(node.attrs.id);
        mentions.push(node.attrs as MentionAttrs);
      }
```

Node ids, not user ids. And every occurrence gets a distinct node id, because
the 2-segment mention URL that Sturnus emits carries no id of its own and one
is generated per parse — `shared/editor/rules/mention.ts` at v1.9.1:

```ts
return { id: id ?? uuidv4(), type: mentionType, modelId };
```

So this function returns 300 entries for 300 blocks by one speaker. It is *not*
where the deduplication happens. This matters: someone reading only this
function could wrongly conclude the collapsing happens here, and would then be
unable to explain the 2023 bug report described below.

### 3. Where the deduplication actually happens — the decisive quote

`server/queues/tasks/DocumentPublishedNotificationsTask.ts` at v1.9.1:

```ts
const mentions = DocumentHelper.parseMentions(document, {
  type: MentionType.User,
});
const userIdsProcessed = new Set<string>();
const userIdsMentioned: string[] = [];
const usersToSubscribe: User[] = [];

for (const mention of mentions) {
  if (userIdsProcessed.has(mention.modelId)) {
    continue;
  }
  userIdsProcessed.add(mention.modelId);

  const recipient = await User.findByPk(mention.modelId);
  ...
  if (
    recipient.subscribedToEventType(
      NotificationEventType.MentionedInDocument
    )
  ) {
    await Notification.create({
      event: NotificationEventType.MentionedInDocument,
      userId: recipient.id,
      actorId: mention.actorId,
      teamId: document.teamId,
      documentId: document.id,
    });
    userIdsMentioned.push(recipient.id);
  }
}
```

`userIdsProcessed` is keyed on `mention.modelId`, which is the **mentioned
user's id**. `Notification.create` therefore runs at most once per distinct
user per task run, no matter how many mention nodes carry that user. That
single line, `if (userIdsProcessed.has(mention.modelId)) { continue; }`, is the
whole answer to the question.

### 4. The 2-segment mention syntax is understood by this version

This was the one thing that could have invalidated the analysis, so it was
checked directly. `shared/utils/parseMentionUrl.ts` at v1.9.1:

```ts
const match3 = url.match(/^mention:\/\/([a-z0-9-]+)\/([a-z_]+)\/([a-z0-9-]+)$/);
...
const match2 = url.match(/^mention:\/\/([a-z_]+)\/([a-z0-9-]+)$/);
```

Both the 3-segment `mention://<id>/<type>/<modelId>` and the 2-segment
`mention://user/<uuid>` form that the Sturnus template emits are parsed. (In
older releases — v0.87.4 and earlier — only the 3-segment form was matched. On
such a build the template's mentions would silently degrade to plain links: no
mention chip, and no notification at all. That is not the case here.)

### 5. The update path behaves the same way

`RevisionCreatedNotificationsTask` at v1.9.1 diffs mentions and then applies the
identical guard:

```ts
const mentions = differenceBy(newMentions, oldMentions, "id");
const userIdsProcessed = new Set<string>();
...
for (const mention of mentions) {
  if (userIdsProcessed.has(mention.modelId)) {
    continue;
  }
```

So even an edited document produces at most one mention notification per user
*per revision*.

### 6. Corroboration from the issue tracker

This is weaker evidence than the source, and is cited only because it explains
the history and confirms the reading is not a misinterpretation. Outline issue
#5584 (2023-07-21) reported exactly the Sturnus scenario: "if a user is
mentioned e.g. 30 times in a page while writing the page, the user will get 30
emails in 5 mins." The maintainer (tommoor) fixed it the same day in PR #5585,
"fix: Duplicate mentions results in duplicate notifications", merge commit
`dbd85d62`, first shipped in release v0.71.0 (2023-08-18). That PR introduced
the per-user guard, in its original `Array.includes` form, into all four
notification tasks. So per-occurrence notification was a real bug, it was
reported, and it was fixed three years before this investigation — and the fix
is still present at v1.9.1.

### 7. What the product documentation says

Nothing. `docs.getoutline.com` and the changelog entries for "Document
mentions" and "In app notifications" state only that `@` mentions exist and
notify. Neither states per-document versus per-occurrence semantics. There is
therefore no documented contract to rely on; this is an implementation
property.

## Which instance this applies to

A host correction that belongs in the record: **`https://wiki.onelitefeather.net`
is not the Outline instance.** It serves GitBook (response header
`x-gitbook-route-site: wiki.onelitefeather.net/`, served from
`6ab8c2a8d0-hosting.gitbook.io`). The real Outline instance is
**`https://outline.onelitefeather.dev`**, found from the absolute `url` field
returned by an authenticated `list_documents` call. `POST /api/auth.config`
there returns team name "OneLiteFeather" with an `azure` provider. Whoever
configures `STURNUS_OUTLINE_BASE_URL` must use the `.dev` host.

Two further facts read from the instance itself, both from the inline
`window.env` on the served page: `ENVIRONMENT: production`, and
`EMAIL_ENABLED: false` — this deployment has no SMTP configured, so mention
notifications currently reach the in-app inbox only and cannot fan out as
email. That is a current config value, not a property of Outline; if SMTP is
enabled later, each notification row becomes one email (still one, not N).

## Version: how it was determined, and how firm that is

The instance version is **1.9.1**, read from the running deployment:

```console
$ kubectl get deploy -n outline \
    -o jsonpath='{.items[*].spec.template.spec.containers[*].image}'
docker.getoutline.com/outlinewiki/outline:1.9.1   # outline-web
docker.getoutline.com/outlinewiki/outline:1.9.1   # outline-collaboration
```

That is the image the cluster pulls and runs, so it is an observation rather
than an inference. `POST /api/installation.info` reports the same field but
needs an admin token, which this investigation did not use.

Two independent fingerprints, gathered before the image tag was read, agree
with it. They are recorded here because they are what a reader without cluster
access can reproduce:

- **API surface bracketing.** Unauthenticated `POST /api/<name>` returns 401
  `authentication_required` when a route exists and 404 `not_found` when it
  does not; this was calibrated with control probes against invented names,
  which all returned 404. `templates.list` -> 401 implies >= v1.6.0;
  `accessRequests.create` -> 401 implies >= v1.8.0; `/api/batch` -> 401 implies
  >= v1.9.0.
- **Frontend bundle string markers.** The served
  `EditorStyleHelper.C6vT3iyM.js` still contains `"comment-marker-hovered"`.
  That constant (`EditorStyleHelper.commentHovered`) exists at v1.9.1 and was
  replaced by `commentGutter = "comment-gutter"` in v1.9.2. So the build is
  >= v1.9.1 and < v1.9.2.

A custom or forked image could still carry the upstream tag while diverging
from it. Nothing observed suggests that here.

**The conclusion is in any case robust to the version.** The per-user dedup
guard has been present continuously from v0.71.0 (August 2023) through current
`main`, verified at v0.78.0, v0.82.0, v0.87.4, v1.9.1 and `main`. The only
version-sensitive detail is the 2-segment URL support, which affects whether
mentions render at all, not whether they over-notify.

## What is evidence and what is inference

Evidence, quoted above and reproducible by fetching the same files:

- The `userIdsProcessed` / `mention.modelId` guard in
  `DocumentPublishedNotificationsTask` and `RevisionCreatedNotificationsTask`
  at v1.9.1.
- `parseMentionUrl` at v1.9.1 accepting the 2-segment form.
- `parseMentions` keying its own dedup on the node id, not the user id.
- Issue #5584 and PR #5585 with its merge commit and first release.
- The instance being `outline.onelitefeather.dev`, running `production`, with
  `EMAIL_ENABLED: false`.
- The deployed image tag, `docker.getoutline.com/outlinewiki/outline:1.9.1`,
  read from the cluster.

Inference, i.e. reasoned but not directly observed:

- That Sturnus's write therefore produces exactly one notification per speaker.
  This follows from the source plus the version, but was never observed
  happening.
- That no database constraint is involved: `server/models/Notification.ts`
  declares no unique index and the three notification migrations add only
  non-unique indexes, so uniqueness rests entirely on the in-process `Set`.

## What is still unverified

1. **No empirical test was run.** The read-only constraint was honoured: no
   document, collection or comment was created, updated or deleted on the
   production wiki, and no real user was mentioned anywhere. A genuine test
   would notify a real person, and that test is not ours to run. Every
   write-capable endpoint was touched unauthenticated only, where the auth
   middleware rejects before any handler executes.
2. **The instance was not asked to report its own version.** The tag was
   read from the deployment's container image instead, which is what it
   runs but not what it says about itself. See above.
3. **Bot-authored mentions carry no actor.** `Mention.parseMarkdown()` sets only
   `id`, `type`, `modelId` and `label`; `actorId` stays undefined. The
   `Notification.actorId` column allows null so the row is created, but the
   mention email template reads `notification.actor.name`. This is inert today
   because `EMAIL_ENABLED` is false, and it does not affect the in-app
   notification. Worth watching if SMTP is ever enabled.
4. Because API-created mentions carry no actor, the self-mention suppression
   `recipient.id === mention.actorId` does not fire. A bot mentioning the token
   owner would notify them.

## The real risk is write frequency, not occurrence count

This is the finding all three investigation angles converged on independently,
and it is the one worth carrying into the design:

The deduplication is **per notification-generating event**, not per document
lifetime. One `documents.create` with `publish: true` gives one notification per
speaker. But if Sturnus were ever changed to append the protocol incrementally
via repeated `documents.update`, each resulting revision would fire
`RevisionCreatedNotificationsTask` — and because the 2-segment mention URL gets a
fresh node uuid on every parse, `differenceBy(newMentions, oldMentions, "id")`
would treat *every* mention as new on *every* rewrite. That is one notification
per speaker per revision, and it is not rate-limited: the six-hour
`shouldNotify()` window applies only to generic `UpdateDocument` notifications.
The v1.9.1 source says so explicitly in a comment: mention notifications "must
be processed regardless of the change threshold as even a small edit can add a
mention."

Two mitigations exist in Outline and are worth knowing about. `DebounceProcessor`
delays `documents.update` by five minutes in production and aborts if the
document changed again, collapsing a burst into one revision — but
`RevisionsProcessor` bypasses that debounce and creates a revision immediately
when the update carries `done: true`.

There is also a second-order effect: `DocumentPublishedNotificationsTask` calls
`subscribeUsersToDocument` for every mentioned user, and `UpdateDocument`
defaults to enabled. Mentioning someone silently subscribes them to the protocol
document, so later edits notify them again.

**Design rule that follows:** keep the single write. Buffer the protocol and do
exactly one `documents.create` with `publish: true` at the end of the meeting.
If incremental writing is ever introduced, do not pass `done: true`, and
re-evaluate this document.

## Decision recorded

`src/sturnus/infrastructure/documents/outline_template.md.j2` renders every
speaking block as a full mention. This is correct and requires no change. The
Spec 8.3 fallback — first block as mention, later blocks as plain text with a
Discord link — is **not** activated, because the notification-spam concern that
motivated it does not exist on Outline >= 0.71.0, and the instance runs v1.9.1.

If the human confirmations listed in the follow-up section come back
differently — in particular, if the instance turns out to run something older
than v0.71.0, or if a controlled test with a throwaway second account shows more
than one notification — revert to the fallback. The asymmetry justifies that:
losing mention rendering on later blocks costs a little fidelity in the
protocol, while notifying a person several hundred times per meeting is not
recoverable.