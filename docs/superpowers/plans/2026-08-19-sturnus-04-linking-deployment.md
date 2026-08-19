# Sturnus Plan 4: Account Linking and Deployment

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Participants can link their Outline account so they appear in protocols as themselves, and the whole system runs in the cluster.

**Architecture:** A third process serves the OAuth callback — the only one reachable from outside — and stores nothing but the resulting identity mapping. The three processes share one image with three entry points. Delivery follows the organisation's existing patterns: Release Please, the reusable Docker workflow, a Helm chart in this repository, and Flux manifests in the cluster repository.

**Tech Stack:** Python 3.12, `aiohttp` (already present via discord.py), `httpx`, Helm, Flux, CloudNativePG, Rook/Ceph S3, SOPS.

**Spec:** `docs/superpowers/specs/2026-08-19-sturnus-design.md`

**Predecessors:** Plans 1–3. After Plan 3 the MVP works end to end, but every speaker appears under their Discord name because nothing is linked yet.

## Global Constraints

- **Python `>=3.12`**, dependency management exclusively through `uv`.
- **The dependency rule** and **one data access path** as in the previous plans.
- **All code, comments, docstrings and assertion messages in English.**
- **Conventional Commits**; no Claude attribution.
- **`mypy` `strict = true`**; `ruff check` and `ruff format --check` clean.
- **No access token is ever persisted** (Spec 8.4). The OAuth exchange establishes an identity once; the token is used for that single call and discarded.
- **Secrets never appear in logs** — not the client secret, not a code, not a token, not a state.

## Two things this plan spans two repositories

Tasks 1–8 are in this repository. **Task 9 changes `OneLiteFeatherNET/Kubernetes-FLUX`** — a different repository with its own review. Do not mix the two in one branch, and do not push cluster manifests here.

---

### Task 1: The OAuth state

The state is what ties a callback back to a Discord user, and the only thing standing between an attacker and linking their own Outline account to someone else's Discord identity. It is therefore a security boundary, not bookkeeping.

**Files:**
- Create: `src/sturnus/application/linking.py`
- Test: `tests/application/test_linking.py`

**Interfaces:**
- Produces:
  - `new_state() -> str`
  - `LinkStateStore(session_factory)` with `issue(discord_user_id, provider, now, ttl) -> str`, `consume(state, now) -> PendingLink | None`, `purge_expired(now) -> int`
  - `PendingLink(discord_user_id: int, provider: str)`

- [ ] **Step 1: Write the failing test**

```python
# tests/application/test_linking.py
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sturnus.application.linking import LinkStateStore, new_state
from sturnus.infrastructure.db.models import Base

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
TTL = timedelta(minutes=10)
ANNA = 100


@pytest.fixture
async def store(clean_database: str) -> LinkStateStore:
    engine = create_async_engine(clean_database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return LinkStateStore(async_sessionmaker(engine, expire_on_commit=False))


def test_states_are_unguessable_and_distinct() -> None:
    states = {new_state() for _ in range(200)}
    assert len(states) == 200
    assert all(len(s) >= 32 for s in states)


async def test_an_issued_state_resolves_to_its_user(store: LinkStateStore) -> None:
    state = await store.issue(ANNA, "outline", T0, TTL)
    pending = await store.consume(state, T0 + timedelta(minutes=1))
    assert pending is not None
    assert pending.discord_user_id == ANNA
    assert pending.provider == "outline"


async def test_a_state_can_only_be_used_once(store: LinkStateStore) -> None:
    """Replaying a callback must not link a second time."""
    state = await store.issue(ANNA, "outline", T0, TTL)
    assert await store.consume(state, T0) is not None
    assert await store.consume(state, T0) is None


async def test_an_expired_state_is_refused(store: LinkStateStore) -> None:
    state = await store.issue(ANNA, "outline", T0, TTL)
    assert await store.consume(state, T0 + TTL + timedelta(seconds=1)) is None


async def test_an_unknown_state_is_refused(store: LinkStateStore) -> None:
    """A forged callback must not resolve to anyone."""
    assert await store.consume("not-a-real-state", T0) is None


async def test_purging_removes_only_expired_states(store: LinkStateStore) -> None:
    old = await store.issue(ANNA, "outline", T0 - timedelta(hours=1), TTL)
    fresh = await store.issue(ANNA, "outline", T0, TTL)
    removed = await store.purge_expired(T0)
    assert removed == 1
    assert await store.consume(old, T0) is None
    assert await store.consume(fresh, T0) is not None
```

- [ ] **Step 2: Run it, confirm it fails, then implement**

`new_state()` uses `secrets.token_urlsafe(32)` — this must be cryptographically random, never `random`.

`consume` deletes the row and returns its content in one transaction, which is what makes single use hold under concurrent callbacks. An expired or unknown state returns `None` without distinguishing the two: telling a caller which one it was reveals whether a state exists.

- [ ] **Step 3: Verify and commit**

```bash
uv run pytest tests/application/test_linking.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add single-use oauth state for account linking"
```

---

### Task 2: Verify Outline's OAuth, then write the client

**Files:**
- Create: `docs/verification/outline-oauth.md`
- Create: `src/sturnus/infrastructure/documents/outline_oauth.py`
- Test: `tests/infrastructure/test_outline_oauth.py`

**Interfaces:**
- Produces: `OutlineOAuth(base_url, client_id, client_secret, redirect_uri)` with `authorize_url(state) -> str` and `identity_from_code(code) -> ExternalIdentity`; `ExternalIdentity(external_user_id: str, display_name: str)`

- [ ] **Step 1: Verify before writing**

Spec 8.4 flags this as unverified, and it is the third such case in this project — the voice-receive spike and the Outline document API were the others. Register an OAuth application on the running Outline instance and establish, recording everything in `docs/verification/outline-oauth.md`:

- The authorisation endpoint and its required query parameters.
- The token endpoint, how the client authenticates to it, and the exact response fields.
- **Which endpoint returns the authenticated user's identity**, and what its id field is called. That id goes into `account_link.external_user_id` and is what mention rendering resolves — a wrong field yields mentions that point nowhere.
- The scope needed to read one's own identity, and whether a narrower scope than full access exists. Ask for the least that works: this token reads an identity once and is then discarded.
- Whether PKCE is supported. If it is, use it even though the client is confidential — it costs a hash and closes code interception.
- What an invalid or reused code returns.

Redact secrets in the document. Revoke the application afterwards if it was created for the test only.

- [ ] **Step 2: Write the test against the verified shape**

```python
# tests/infrastructure/test_outline_oauth.py
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from sturnus.infrastructure.documents.outline_oauth import (
    LinkExchangeError,
    OutlineOAuth,
)

BASE = "https://outline.example"
REDIRECT = "https://sturnus.example/oauth/callback"


def client(transport: httpx.MockTransport | None = None) -> OutlineOAuth:
    return OutlineOAuth(
        base_url=BASE,
        client_id="cid",
        client_secret="csecret",
        redirect_uri=REDIRECT,
        transport=transport,
    )


def test_the_authorize_url_carries_the_state() -> None:
    query = parse_qs(urlparse(client().authorize_url("state-123")).query)
    assert query["state"] == ["state-123"]
    assert query["redirect_uri"] == [REDIRECT]
    assert query["client_id"] == ["cid"]


def test_the_authorize_url_never_carries_the_secret() -> None:
    """It goes to the user's browser."""
    assert "csecret" not in client().authorize_url("s")


async def test_a_code_resolves_to_an_identity() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer"})
        return httpx.Response(200, json={"data": {"id": "9c8b", "name": "Max Example"}})

    identity = await client(httpx.MockTransport(handle)).identity_from_code("code-1")
    assert identity.external_user_id == "9c8b"
    assert identity.display_name == "Max Example"


async def test_a_rejected_code_raises_a_link_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(LinkExchangeError):
        await client(httpx.MockTransport(handle)).identity_from_code("stale")


async def test_no_secret_or_token_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "super-secret-token"})
        return httpx.Response(200, json={"data": {"id": "x", "name": "n"}})

    with caplog.at_level("DEBUG"):
        await client(httpx.MockTransport(handle)).identity_from_code("code-1")

    assert "super-secret-token" not in caplog.text
    assert "csecret" not in caplog.text
    assert "code-1" not in caplog.text
```

> Adjust paths and field names to what Step 1 found. The behavioural assertions — state present, secret absent from the browser-bound URL, nothing sensitive in logs, a clear error on a bad code — hold regardless of the API's shape.

- [ ] **Step 3: Implement, verify and commit**

The client holds no state and stores no token: `identity_from_code` exchanges, reads the identity, and lets the token go out of scope. Spec 8.4's whole simplification rests on that.

```bash
uv run pytest tests/infrastructure/test_outline_oauth.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add outline oauth client for identity establishment"
```

---

### Task 3: The link service

**Files:**
- Create: `src/sturnus/infrastructure/linkserver.py`
- Create: `src/sturnus/entrypoints/link.py`
- Test: `tests/infrastructure/test_linkserver.py`

**Interfaces:**
- Produces: `build_app(oauth, states, links, now) -> aiohttp.web.Application`, `main()`
- **Extends** `AccountLinkRepository`, which Plan 3 created with only a read method:
  - `save(discord_user_id, provider, external_user_id, display_name) -> None` — upserts on `(discord_user_id, provider)`, so re-linking replaces rather than duplicating
  - `delete(discord_user_id, provider) -> bool` — returns whether anything was removed, which `/link remove` reports back

- [ ] **Step 1: Write the failing test**

```python
# tests/infrastructure/test_linkserver.py
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from sturnus.infrastructure.linkserver import build_app

T0 = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)
ANNA = 100


class FakeStates:
    def __init__(self, valid: str | None = "good-state") -> None:
        self.valid = valid
        self.consumed: list[str] = []

    async def consume(self, state: str, now: datetime) -> Any:
        self.consumed.append(state)
        if state != self.valid:
            return None
        from sturnus.application.linking import PendingLink

        self.valid = None  # single use
        return PendingLink(discord_user_id=ANNA, provider="outline")


class FakeOAuth:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def authorize_url(self, state: str) -> str:
        return f"https://outline.example/oauth/authorize?state={state}"

    async def identity_from_code(self, code: str) -> Any:
        from sturnus.infrastructure.documents.outline_oauth import ExternalIdentity

        if self.fail:
            from sturnus.infrastructure.documents.outline_oauth import LinkExchangeError

            raise LinkExchangeError("nope")
        return ExternalIdentity(external_user_id="9c8b", display_name="Max Example")


class FakeLinks:
    def __init__(self) -> None:
        self.saved: list[tuple[int, str, str, str]] = []

    async def save(self, discord_user_id: int, provider: str, external_id: str, name: str) -> None:
        self.saved.append((discord_user_id, provider, external_id, name))


@pytest.fixture
async def client(aiohttp_client: Any) -> TestClient:
    return await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=FakeLinks(), now=lambda: T0)
    )


async def test_healthz_is_served(client: TestClient) -> None:
    assert (await client.get("/healthz")).status == 200


async def test_a_valid_callback_stores_the_link(aiohttp_client: Any) -> None:
    links = FakeLinks()
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=links, now=lambda: T0)
    )
    response = await c.get("/oauth/callback", params={"code": "c", "state": "good-state"})
    assert response.status == 200
    assert links.saved == [(ANNA, "outline", "9c8b", "Max Example")]


async def test_an_unknown_state_is_refused_and_stores_nothing(aiohttp_client: Any) -> None:
    """A forged callback must not link anything."""
    links = FakeLinks()
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=links, now=lambda: T0)
    )
    response = await c.get("/oauth/callback", params={"code": "c", "state": "forged"})
    assert response.status == 400
    assert links.saved == []


async def test_a_replayed_state_is_refused(aiohttp_client: Any) -> None:
    links = FakeLinks()
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=links, now=lambda: T0)
    )
    params = {"code": "c", "state": "good-state"}
    assert (await c.get("/oauth/callback", params=params)).status == 200
    assert (await c.get("/oauth/callback", params=params)).status == 400
    assert len(links.saved) == 1


async def test_a_missing_parameter_is_refused(client: TestClient) -> None:
    assert (await client.get("/oauth/callback", params={"state": "good-state"})).status == 400
    assert (await client.get("/oauth/callback", params={"code": "c"})).status == 400


async def test_a_failed_exchange_reports_an_error_and_stores_nothing(
    aiohttp_client: Any,
) -> None:
    links = FakeLinks()
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(fail=True), states=FakeStates(), links=links, now=lambda: T0)
    )
    response = await c.get("/oauth/callback", params={"code": "c", "state": "good-state"})
    assert response.status >= 400
    assert links.saved == []


async def test_the_error_page_does_not_echo_the_input(aiohttp_client: Any) -> None:
    """Reflecting user input into HTML is how a callback becomes an XSS sink."""
    c = await aiohttp_client(
        build_app(oauth=FakeOAuth(), states=FakeStates(), links=FakeLinks(), now=lambda: T0)
    )
    response = await c.get(
        "/oauth/callback", params={"code": "c", "state": "<script>alert(1)</script>"}
    )
    body = await response.text()
    assert "<script>" not in body
```

- [ ] **Step 2: Extend `AccountLinkRepository`**

Plan 3 built only the read side, because assembly was all that needed it. Add
`save` and `delete` with tests alongside the existing repository tests. `save`
must upsert on the composite key `(discord_user_id, provider)`: someone
re-linking after changing their Outline account would otherwise hit a primary
key violation, and the natural reading of "link my account again" is replace,
not fail.

- [ ] **Step 3: Run the link server test, confirm it fails, then implement**

Three routes: `/healthz`, `/readyz`, and `/oauth/callback`. The callback validates both parameters are present, consumes the state, exchanges the code, saves the mapping, and returns a small self-contained confirmation page telling the person to return to Discord.

Requirements the tests pin, plus two a reviewer must check by reading:

- **No user input is reflected into the response.** Not the state, not the code, not an error from Outline. The confirmation and error pages are static.
- **No secrets, codes or states are logged**, at any level.

This is the only publicly reachable process in the system. It holds the OAuth client secret and a database connection, and nothing else — no Discord token, no S3 credentials, no master key. That separation is the reason it is a separate deployment.

- [ ] **Step 4: Write the entrypoint, restore its console script, verify and commit**

```bash
uv run pytest tests/infrastructure/test_linkserver.py tests/infrastructure/test_repositories.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add the oauth link service"
```

---

### Task 4: The link commands

**Files:**
- Create: `src/sturnus/infrastructure/discord/link_cog.py`
- Test: `tests/infrastructure/discord/test_link_cog.py`

- [ ] **Step 1: Write the tests for the decision logic**

As with the consent commands, the branching lives outside the callbacks. Test: a user with no link gets an authorisation URL; a user who is already linked is told so and offered `/link remove`; the issued URL contains a state that resolves back to that user; `/link remove` deletes the mapping and reports whether anything was removed.

- [ ] **Step 2: Implement the cog**

| Command | Behaviour |
|---|---|
| `/link` | Issues a state, replies **ephemerally** with the authorisation URL |
| `/link status` | Shows whether an account is linked, and which display name it carries |
| `/link remove` | Deletes the mapping; existing protocols keep the name they were written with |

The authorisation URL must never be posted publicly — it carries a state that grants linking to the invoking user's Discord identity. An ephemeral reply is the whole protection.

`/link remove` states plainly that past protocols are unaffected: they are a separate processing result, already written, and rewriting history is not what unlinking means.

- [ ] **Step 3: Verify and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git add -A && git commit -m "feat: add link commands"
```

---

### Task 5: The container image

One image, three entry points (Spec 13.2). Building three images from one codebase would triple build time, scanning and registry storage without separating anything the deployment does not already separate.

**Files:**
- Create: `Dockerfile`, `.dockerignore`
- Modify: `pyproject.toml` (restore all three console scripts)

- [ ] **Step 1: Restore the console scripts**

```toml
[project.scripts]
sturnus-bot = "sturnus.entrypoints.bot:main"
sturnus-link = "sturnus.entrypoints.link:main"
sturnus-worker = "sturnus.entrypoints.worker:main"
```

All three modules now exist. They were removed in Plan 1 precisely because they did not.

- [ ] **Step 2: Write the Dockerfile**

A multi-stage build: install dependencies with `uv` in a builder, copy only the virtual environment and the source into a slim runtime. Requirements:

- **Run as a non-root user.** The bot writes to a mounted volume; the image must not need root to do it.
- **Do not bake the Whisper model into the image.** It is downloaded at first start and cached on a volume — the same pattern the cluster's Ollama installation already uses. A baked-in model would add well over a gigabyte to every pull.
- Set `HF_HOME` (or the equivalent the installed `faster-whisper` honours — verify which) to a path under the mounted cache volume, so the download survives a restart.
- No build tools in the final stage.
- `.dockerignore` excludes `.git`, `.venv`, `docs`, `tests` and the caches.

- [ ] **Step 3: Verify all three entry points start**

```bash
docker build -t sturnus:dev .
docker run --rm sturnus:dev sturnus-bot --help
docker run --rm sturnus:dev sturnus-worker --help
docker run --rm sturnus:dev sturnus-link --help
```

Each must fail with a *configuration* error about missing settings, not with `ModuleNotFoundError`. The distinction matters: the first means the entry point resolves and the process starts, the second means the image is broken in a way no health probe would catch until it crash-loops.

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: add container image with three entrypoints"
```

---

### Task 6: Publishing and release wiring

**Files:**
- Modify: `.github/workflows/release-please.yml`
- Modify: `release-please-config.json`

- [ ] **Step 1: Chain the Docker publish job**

Add a `publish` job to `release-please.yml` that runs only when a release was created, calling the organisation's reusable workflow:

```yaml
  publish:
    needs: release-please
    if: needs.release-please.outputs.release_created == 'true'
    permissions:
      contents: read
      id-token: write   # keyless signing
    uses: OneLiteFeatherNET/workflows/.github/workflows/docker-publish.yml@v2.4.0
    with:
      image-name: "onelitefeather/sturnus"
      version: ${{ needs.release-please.outputs.version }}
      context: "."
    secrets: inherit
```

Pin the full SemVer tag, never `@main` and never a bare major alias — Renovate keeps it current, and an alias defeats its ability to tell what "current" means. **Confirm `@v2.4.0` is still the newest tag** before writing it; this plan was written earlier than it will be executed.

The target is the organisation's Harbor registry, not GHCR. The reusable workflow already knows where to push; do not override it.

Do **not** add a tag-triggered publish workflow. Release Please tags with the default `GITHUB_TOKEN`, which does not re-trigger tag workflows in the same repository, so a second path would either never fire or race this one.

- [ ] **Step 2: Version the chart alongside the application**

Once Task 7 has created the chart, add it to `extra-files`:

```json
"extra-files": [
  { "type": "generic", "path": "pyproject.toml" },
  { "type": "generic", "path": "charts/sturnus/Chart.yaml" }
]
```

Both `version` and `appVersion` in `Chart.yaml` carry the `# x-release-please-version` marker. One version for a chart that ships exactly one application; separate version streams would be effort without benefit.

Order matters: adding this before the chart exists makes Release Please fail on a missing file. Do this step after Task 7.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "ci: publish the container image on release"
```

---

### Task 7: The Helm chart

**Files:**
- Create: `charts/sturnus/Chart.yaml`, `values.yaml`, and templates for the three deployments, their services, the PVCs, the HTTPRoute or Ingress, and a PodDisruptionBudget

- [ ] **Step 1: Write the chart**

Three deployments from one image, differing in command and resources:

| Deployment | Replicas | Command | Resources | Volumes |
|---|---|---|---|---|
| `bot` | 1, fixed | `sturnus-bot` | 1 CPU | RWO PVC for recordings |
| `link` | 2 | `sturnus-link` | minimal | none |
| `worker` | 1 | `sturnus-worker` | 4 CPU, ~2 Gi | PVC for the model cache |

Details that are not optional:

- **`bot` has `replicas: 1` and a `Recreate` strategy.** Two instances would hold two gateway connections and record twice; a rolling update would briefly do the same. The strategy is what prevents it.
- **`bot` gets a PodDisruptionBudget** and a node affinity pinning it to the region, because its PVC binds it to a zone anyway (Spec 13.5).
- **Only `link` is exposed.** The bot holds the Discord token, the S3 credentials and the master key, and must not be reachable from outside. Follow the Outline installation's pattern: exposure through Cloudflare Tunnel, the chart's own route disabled.
- **`terminationGracePeriodSeconds` on `bot` must exceed the time to flush, encrypt and upload a session.** The default 30 seconds is not enough for a multi-hour recording; size it deliberately and say why in a comment. Too short here silently discards exactly what Spec 6.4's SIGTERM handling exists to save.
- Health probes on all three: `/healthz` for liveness, `/readyz` for readiness. The worker's readiness must account for the model load, which takes minutes on first start — a probe that fails during it will restart the pod forever.
- Every secret comes from an existing Kubernetes Secret, never from `values.yaml`.

- [ ] **Step 2: Verify the chart renders**

```bash
helm lint charts/sturnus
helm template sturnus charts/sturnus --values charts/sturnus/values.yaml > /tmp/rendered.yaml
```

Read the rendered output rather than trusting the lint: check that `bot` really has one replica, that no secret value appears literally, that the three commands differ, and that only `link` has a route.

- [ ] **Step 3: Commit, then return to Task 6 Step 2**

```bash
git add -A && git commit -m "feat: add helm chart"
```

---

### Task 8: Operational documentation

**Files:**
- Modify: `README.md`
- Create: `docs/operations.md`

- [ ] **Step 1: Write what an operator needs**

The README states what Sturnus is, what it records, and how to run it locally. `docs/operations.md` covers what nobody can reconstruct from the code:

- **Every environment variable**, what it does, and which of them are secret.
- **How the master key is generated**, how it reaches SOPS, and what happens when it is rotated — old recordings stay readable because `encryption_key_id` names the key that wrapped each data key, and that only works if the old key is kept. Losing a master key means the recordings it wrapped are gone, and no backup restores them.
- **The Discord setup**: which permissions the bot needs, why the recording channel must deny `Speak` to `@everyone` and allow it for the consent role, and why non-recorded channels must exist alongside it (Spec 3.2 — consent that is the only route to participation is not freely given).
- **The first-run checklist**: `/config set` for each required key, then `/config show` to confirm nothing is missing.
- **What to do when a job is dead**, when the queue stalls, and how to tell a transcription failure from a document failure.
- **The retention setting and what changing it means** — it belongs in the privacy policy, and changing the duration changes `policy_version` and re-requires consent (Spec 3.2).

- [ ] **Step 2: Commit**

```bash
git add -A && git commit -m "docs: add operations guide"
```

---

### Task 9: Cluster manifests — in the Kubernetes-FLUX repository

**This task does not touch this repository.** Work in `OneLiteFeatherNET/Kubernetes-FLUX`, on its own branch, with its own pull request.

- [ ] **Step 1: Read the existing patterns before writing anything**

Look at how a comparable application is wired there — Outline is the closest, since it also has a database, an S3 bucket and external exposure. Follow what you find rather than what this plan describes; the plan was written from a snapshot and the repository is the authority.

- [ ] **Step 2: Add the manifests**

- `apps/base/sturnus/` and the cluster overlay, following the existing layout.
- A CloudNativePG database following the `database/` pattern.
- An `ObjectBucketClaim` for the audio bucket, modelled on `outline.yaml`, **with its own credentials** — this bucket holds recorded speech and must not share access with anything else (Spec 12.1).
- SOPS-encrypted secrets: Discord token, Outline service token, OAuth client id and secret, and the audio master key.
- Exposure for `link` only, through Cloudflare Tunnel.

- [ ] **Step 3: Check the bucket's lifecycle rule**

Spec 12.2 requires a lifecycle rule as the second line of defence behind the retention sweep, so a worker that has been down for weeks cannot lead to unbounded retention. Confirm the object store supports it and configure it; if it does not, say so in the pull request rather than leaving the gap unmentioned.

- [ ] **Step 4: Open the pull request**

Describe what is being deployed, that it processes voice recordings, and which secrets it needs. Reviewers of that repository have not read this spec.

---

### Task 10: First deployment

- [ ] **Step 1: Deploy and watch it start**

Confirm in order: migrations applied by the worker; the bot connected and its commands registered; the worker's model downloaded and cached to its volume; the link service reachable at its public URL and answering `/healthz`.

The model download is the slowest step and the most likely to look like a failure. Watch it complete before concluding anything is wrong.

- [ ] **Step 2: Configure a guild**

Set every required key with `/config set`, then `/config show` to confirm none is missing. Set up the recording channel per `docs/operations.md`: `@everyone` denied `Speak`, the consent role allowed, and at least one ordinary voice channel beside it.

- [ ] **Step 3: Run the full path with linked accounts**

Two participants, both consenting, both having run `/link`. Hold a conversation, leave, wait for the document, follow the link posted in the channel.

Verify: both appear as **Outline mentions** rather than plain names; the notifications behave as Task 7 of Plan 3 predicted; the chronology matches what actually happened.

- [ ] **Step 4: Verify the obligations in production**

- `/consent revoke` removes the role, and the next session excludes that person entirely.
- `/audio delete` removes that user's recordings, and the reply matches what was deleted.
- An administrator without the consent role who speaks contributes nothing.
- No transcript content, audio, token or key appears in any log line from any of the three pods.
- The retention sweep removes an expired recording and stamps `audio_deleted_at`.

The last four are legal gates. A failure in any of them means the deployment is stopped, not noted.

- [ ] **Step 5: Record what reality showed**

Measure and write down: latency from session end to posted link, transcription quality on real German speech, CPU and memory for all three pods under load, and the actual size of a recording per speaker-hour.

Every one of these was an estimate in the spec. This is the first time real numbers exist, and they are what tells you whether the resource requests are right and whether `large-v3-turbo` on four cores was the correct choice.

---

## What exists after this plan

The full system as specified: consent enforced on two layers, recordings encrypted and expiring on schedule, transcripts in Outline with real user mentions, and everything running in the cluster under the organisation's existing release and dependency tooling.

## What deliberately remains undone

- **Adapters other than Outline.** The port is drawn and has one implementation; Confluence, Notion or a file store were never in scope, and the port's shape will be tested by the second adapter, not by anticipating it.
- **Admin-settable templates.** The sandbox is in place so the door can be opened safely; the door itself is not part of the MVP.
- **LLM summarisation.** Documents carry the raw transcript. The cluster's Ollama installation makes this a natural next step, and Spec 2 names it as a possible later phase.
- **More than one recording channel per guild.** The data model carries a channel id per session and would allow it; the configuration does not.
