# First deployment: what a person has to supply

Everything Sturnus needs that cannot be generated, inferred, or committed.
The rest — chart, manifests, image — is already in place.

This is the checklist for the *first* deployment. Section 1 of
[`operations.md`](operations.md) is the reference for what each variable
means; this document is the order to do things in, and the parts only you
can do.

Read the whole thing before starting. Two steps depend on values that do
not exist until an earlier step has run, which is why the order is not the
obvious one.

## 0. The one thing to decide first

**Where the audio master key lives.** Losing it destroys every recording it
wrapped — no database backup helps, no S3 backup helps, because both hold
the same unreadable bytes. Decide where the backup copy goes before you
generate it, not after.

Everything else on this list can be regenerated if lost.

## 1. Merge the cluster manifests — before you have any secrets

`OneLiteFeatherNET/Kubernetes-FLUX` PR #186 creates the namespace, the
CloudNativePG database, the Ceph bucket and its object-store user, the
ingress, and the HelmRelease.

Merge it **first, without the secret**. The pods will CrashLoopBackOff on
the missing `Secret` — that is expected and harmless, and the kustomization
says so in a comment.

The reason for this order: the S3 credentials in step 3 do not exist yet.
Rook generates them when it reconciles the `CephObjectStoreUser` this PR
adds. There is nothing to copy into a secret before that has happened.

Confirm the pieces arrived:

```bash
kubectl get cephobjectstoreuser -n rook-ceph-fr01 | grep sturnus
kubectl get cluster -n cnpg-system      # the sturnus database
kubectl get pods -n sturnus             # CrashLoopBackOff at this point
```

## 2. Create the Discord application — the only thing outside our infra

There is no Sturnus bot application yet. At
<https://discord.com/developers/applications>:

1. **New Application**, name it Sturnus.
2. **Bot** tab → **Reset Token**, copy it. This is `STURNUS_DISCORD_TOKEN`.
   It can be regenerated at any time without data loss, unlike the master
   key.
3. Same tab, **Privileged Gateway Intents** → enable **Server Members
   Intent**.

   Only that one. Voice states are not a privileged intent and have no
   toggle — `discord.py`'s `Intents.default()` already includes them. If
   you are looking for a "Voice States Intent" switch, it does not exist.
4. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`.
   Bot permissions: **View Channel**, **Connect**, **Manage Roles**.

   `Manage Roles` is what `/setup` needs to set the recording channel's
   `Speak` overwrites. `Speak` itself is not needed — the bot only listens.
5. Open the generated URL and invite the bot to the server.

**Role position matters.** Discord will not let the bot edit a role
positioned above its own. Drag the bot's role above the consent role in
**Server Settings → Roles**, or `/setup` fails with a permissions error.

## 3. Collect the seven secret values

Six you create or generate; one pair you read out of the cluster.

| Value | Where it comes from |
|---|---|
| `STURNUS_DISCORD_TOKEN` | Step 2 above. |
| `STURNUS_DATABASE_URL` | You choose the password; see below. |
| `STURNUS_S3_ACCESS_KEY` | Read from Rook — **do not invent**. |
| `STURNUS_S3_SECRET_KEY` | Read from Rook — **do not invent**. |
| `STURNUS_MASTER_KEY` | `openssl rand -base64 32` |
| `STURNUS_OUTLINE_SERVICE_KEY` | Outline API key, step 4. |
| `STURNUS_OUTLINE_CLIENT_SECRET` | Outline OAuth app, step 4. |

**The S3 pair.** Rook generated these in step 1. Read them:

```bash
kubectl -n rook-ceph-fr01 get secret rook-ceph-object-user-feather-s3-sturnus \
  -o jsonpath='{.data.AccessKey}' | base64 -d; echo
kubectl -n rook-ceph-fr01 get secret rook-ceph-object-user-feather-s3-sturnus \
  -o jsonpath='{.data.SecretKey}' | base64 -d; echo
```

**The database URL.** Generate a password, then use it in *two* places —
the connection string here and `roles/sturnus.sops.env`, which is what
CloudNativePG sets the role's password to. They must match, or the
connection fails with an authentication error that says nothing about the
mismatch:

```
postgresql+asyncpg://sturnus:<password>@feather-core-cluster-pg-pooler-rw.cnpg-system.svc.cluster.local:5432/sturnus
```

**The master key** must base64-decode to exactly 32 bytes. `openssl rand
-base64 32` produces that; a passphrase does not.

## 4. Create the two Outline credentials

Both in <https://outline.onelitefeather.dev>.

**An API key** (Settings → API & Apps → New API key) — this is
`STURNUS_OUTLINE_SERVICE_KEY`. The worker uses it to create the protocol
document, so it needs write access to the target collection and nothing
more.

**An OAuth application** (Settings → Applications), redirect URI exactly:

```
https://sturnus.onelitefeather.dev/oauth/callback
```

That path is fixed by the link server's own route table, and the host must
match `ingress.yaml` in the FLUX directory. A mismatch here fails **late**
— after a participant has already logged in and consented — and the error
appears in Outline, not in Sturnus. Walk the flow once after deploying
rather than assuming it.

It gives you a client id and a client secret. The **secret** goes in the
SOPS secret; the **id** is public and goes in the HelmRelease's
`commonEnv`, where `STURNUS_OUTLINE_CLIENT_ID` is currently an empty
string waiting for it.

Also decide which **collection** the protocols go into and note its id —
that is `document_target`, set later with `/config`, not an environment
variable.

## 5. Encrypt and wire up the secrets

In the FLUX repository, from the corrected templates on `feat/sturnus`:

```bash
cd apps/clusters/feathre-core/base-apps/sturnus/
cp sturnus-secrets.sops.env.TEMPLATE sturnus-secrets.sops.env
# fill in the seven values, then:
sops -e -i sturnus-secrets.sops.env
```

Same for `infrastructure/clusters/feather-core/configs/postgresql/roles/sturnus.sops.env`
with the database password from step 3.

Then uncomment the `secretGenerator` stanza in each directory's
`kustomization.yaml` — both are written out in the comments, ready to
paste. Commit and merge.

**Never commit either file unencrypted.** `.sops.yaml` at the repo root
matches `*.sops.env` and your age key is already a recipient.

## 6. Verify the pods actually start

```bash
kubectl -n sturnus get pods
kubectl -n sturnus logs deploy/sturnus-worker | head -30
```

What the failures mean:

- **`CreateContainerConfigError`** — a key is missing from the Secret.
  `kubectl describe pod` names which one. This is deliberate: the chart
  asks for each key individually rather than mounting the whole Secret, so
  a missing one stops the pod instead of injecting an empty string.
- **`ValidationError: ... Field required`** — a variable the Secret does
  not carry and `commonEnv` does not set. Section 1 of `operations.md` has
  the per-component table.
- **`... is set but empty`** — a value present but blank, most likely
  `STURNUS_OUTLINE_CLIENT_ID` still at its placeholder. Sturnus refuses to
  start on these rather than failing later at first use.

The worker runs the database migrations at startup, and the bot and link
wait for the tables. So `sturnus-worker` coming up healthy is the gate —
if it does not, the other two never will.

## 7. Configure the guild

In Discord, as an administrator:

1. `/setup` — creates or adopts the recording channel and sets its
   permissions: `Speak` denied to `@everyone`, allowed for the consent
   role. This is the primary consent protection, not a convenience.
2. `/config` — the recording channel, the timeouts, the retention window,
   the target collection id, and `policy_url`, which must point at a real
   privacy policy naming the retention period. Participants consent to
   what that document says.
3. Have one person run `/consent grant` and `/link start` to confirm both
   flows end where they should.

## 8. The one live test

`docs/verification/end-to-end-checklist.md` scripts a two-person session
and is the only exercise several things have ever had: the SIGTERM and
SIGKILL drills, the timeline anchoring check, and four blocking legal
gates. Nothing in the automated suite covers them.

Run it before the bot meets a real meeting. Whatever is not on that
checklist does not get checked, and the crash-recovery path has never run
against a real bot.

## What you do not have to do

For completeness, since these look like open questions and are not:

- The chart, the manifests and the image are done and released (v0.3.1).
- The image is in Harbor; the HelmRelease already points at it.
- `renovate.json` and the `sturnus-maintainers` team are set up, so
  dependency PRs get a reviewer.
- The variable names in the FLUX secret template were verified against the
  code rather than copied from the design document — an earlier revision
  had three wrong and two missing.
