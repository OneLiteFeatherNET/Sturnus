<script setup lang="ts">
/**
 * A guild's own sign-in link.
 *
 * A guild has been able to sign its people in against **its own Outline**
 * since #147 — that is the whole of §2.2, and it exists because a
 * deployment serving several organisations cannot ask them all to keep
 * accounts in one Outline. None of it was reachable from a browser: the
 * `guild_oauth_client` rows exist, five routes write them, and until this
 * page the only way to register a client was `curl`. **A deployment that
 * configures none of this behaves exactly as it did before**, which is why
 * nothing on this page is required of anybody and why the first thing it
 * says is what happens if it is left alone.
 *
 * Everything that is a *decision* lives in `~/utils/oauthClient` and is
 * tested there: what a sign-in name may be, what an identity provider's
 * address may be, what the link is, what state it is in, what may be done
 * to the credential, and what is wrong with a draft. What is left here is
 * layout, request plumbing and which panel is open. The guild picker is not
 * reimplemented either — it is the same functions Bot Settings,
 * Destinations and the Queue use, remembered choice included, so switching
 * servers on one admin page carries to the others.
 *
 * **Four things this page refuses to do, and all four are the same
 * refusal wearing different clothes: it will not become a way to find out
 * which organisations use this service.**
 *
 * - **It never asks whether a sign-in name is free.** No "check
 *   availability", no green tick, no list of taken names — because the API
 *   deliberately answers one 409 for "another guild holds it" and for "this
 *   deployment reserves it", so that which of the two it was cannot be read
 *   off the reply. A console that distinguished them would re-introduce the
 *   oracle one layer up, and it would be reachable by anybody who
 *   administers any guild anywhere.
 * - **It never lists guilds it is not already entitled to list.** The
 *   switcher shows what `GET /guilds` shows, which is the guilds this
 *   person administers and which every other admin page already shows them.
 *   There is no directory of registered slugs on this page and there is no
 *   endpoint that could serve one.
 * - **It never draws a half-configured link as working.** Between
 *   registering a client and supplying its secret, the link answers exactly
 *   as a name nobody has registered — same status, same body — and that is
 *   §2.2 working rather than a fault. So it is said in as many words, in
 *   its own state with its own heading, next to the link itself.
 * - **It never renders a box for a credential beside a credential.** The
 *   registration form has no secret field at all; `SignInClientSecret` is a
 *   separate control with its own request, and its input does not exist
 *   until somebody deliberately opens it.
 *
 * And one thing it insists on saying: **this governs the console sign-in
 * and nothing else.** The Discord account-link flow stays on the
 * environment-configured client permanently, because `api` holds the master
 * key and `link` does not — `charts/sturnus/templates/_helpers.tpl` refuses
 * to render it onto that component at all. That asymmetry is the
 * architecture rather than a gap in it, and an interface that implied
 * otherwise would be promising something the deployment is built to
 * prevent.
 */
import UiSelect from '~/components/ui/UiSelect.vue'
import type { Message } from '~/utils/message'
import {
  SCOPE_NOTE_KEY,
  type ClientDraft,
  type GuildOAuthClient,
  clientPath,
  clientSecretPath,
  describeClientError,
  clientDraftBody,
  clientDraftOf,
  emptyClientDraft,
  isMissingRegistration,
  linkState,
  parseClient,
  signInUrl,
} from '~/utils/oauthClient'
import {
  chooseGuild,
  guildOptions,
  parseGuilds,
  readSelectedGuild,
  writeSelectedGuild,
} from '~/utils/settings'

const { t } = useI18n()
const api = useApi()
const say = useSay()

useHead(() => ({ title: t('admin.signInLink.title') }))

/** The console's own origin, so the link can be shown in full rather than
 *  as a path somebody has to assemble. `useRequestURL` answers the incoming
 *  request's URL on the server and `window.location` in the browser, so the
 *  value does not change under the reader on hydration — which a
 *  `window.location.origin` read in `onMounted` would. */
const origin = useRequestURL().origin

const { data: guildData, error: guildError } = await useAsyncData(
  'sign-in-link-guilds',
  async () => parseGuilds(await api('/guilds')),
)

const guilds = computed(() => guildData.value ?? [])

// Server-side there is no browser and therefore no remembered choice, so
// the first render picks the first guild. The remembered one is applied
// after hydration -- the same trade the sidebar and the other admin pages
// make: a correct first paint for everybody, and one repaint for the person
// who has two guilds and last worked on the second.
const selected = ref<string | null>(chooseGuild(guilds.value, null))

onMounted(() => {
  selected.value = chooseGuild(guilds.value, readSelectedGuild(window.localStorage))
})

function chooseGuildFromMenu(guildId: string | null) {
  // The dropdown's model is nullable so that a page resetting a filter has
  // a way to say so. This page has no such state: there is always a guild
  // being configured.
  if (guildId === null) return
  selected.value = guildId
  if (import.meta.client) writeSelectedGuild(window.localStorage, guildId)
}

const guildChoices = computed(() => guildOptions(guilds.value))

/**
 * The registration, and the guild it belongs to.
 *
 * The guild travels *with* the answer rather than in a ref of its own: a
 * ref set inside the fetcher would be null after hydration, because the
 * server ran the fetch and the client did not. Rendering one guild's
 * sign-in configuration under another guild's heading is worse here than
 * on any other admin page — the value on screen is a credential's
 * registration, and the button under it writes.
 *
 * A 404 is **not** an error. The API answers one 404 to a guild with no
 * client, a guild nobody administers, and a guild that does not exist,
 * deliberately and as one answer; this page only ever asks about guilds the
 * viewer administers, so the reading it can act on is "there is nothing
 * registered yet", which is a state with its own panel.
 */
const {
  data: clientData,
  error: clientError,
  status: clientStatus,
  refresh,
} = await useAsyncData(
  'sign-in-link-client',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, client: null as GuildOAuthClient | null }
    try {
      return { guildId, client: parseClient(await api(clientPath(guildId))) }
    } catch (error) {
      if (isMissingRegistration(error)) return { guildId, client: null }
      throw error
    }
  },
  { watch: [selected] },
)

/** Nothing is shown while the answer on hand belongs to another guild. */
const client = computed(() =>
  clientData.value && clientData.value.guildId === selected.value ? clientData.value.client : null,
)

/** Whether the answer on hand is this guild's at all. Distinct from
 *  `client === null`, which is also what a guild with no registration
 *  looks like: one is "nothing here yet" and the other is "not read yet",
 *  and they get different panels. */
const settled = computed(
  () => clientData.value !== null && clientData.value !== undefined
    && clientData.value.guildId === selected.value,
)

const state = computed(() => linkState(client.value))
const link = computed(() => (client.value ? signInUrl(origin, client.value.slug) : ''))

/* -------------------------------------------------------------------- */
/* Which panel is open, and what happened last                           */
/* -------------------------------------------------------------------- */

/** The registration form, open either to register a first client or to
 *  change the one that is there. One form for both, because the API's `PUT`
 *  is a whole replacement rather than a patch. */
const editing = ref(false)
/** Whether removing the registration is being confirmed. */
const removing = ref(false)
/** Whether a request is running. One at a time: every write here re-reads
 *  the registration afterwards, and two in flight would race over which
 *  answer wins. */
const busy = ref(false)
/** What the last write did, or why it did not. */
const outcome = ref<{ tone: 'good' | 'bad'; message: Message } | null>(null)
/** Whether the link was copied, cleared on the next thing that happens. */
const copied = ref(false)

function closePanels() {
  editing.value = false
  removing.value = false
}

// A registration belongs to a guild, and an open form belongs to a
// registration. Switching servers is a different one of each.
watch(selected, () => {
  outcome.value = null
  copied.value = false
  closePanels()
})

/**
 * Runs one write, then re-reads the registration.
 *
 * Re-read rather than patched in place: `has_secret` after a write is the
 * API's answer and not this page's arithmetic, and a second administrator
 * in another tab can have changed the rest of the row. It is one request
 * against one row.
 */
async function write(request: () => Promise<unknown>, done: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  outcome.value = null
  copied.value = false
  try {
    await request()
    await refresh()
    closePanels()
    outcome.value = { tone: 'good', message: { key: done } }
  } catch (error) {
    outcome.value = { tone: 'bad', message: describeClientError(error) }
  } finally {
    busy.value = false
  }
}

const guildId = computed(() => selected.value ?? '')

function save(draft: ClientDraft) {
  void write(
    () => api(clientPath(guildId.value), { method: 'PUT', body: clientDraftBody(draft) }),
    client.value ? 'admin.signInLink.savedChanged' : 'admin.signInLink.savedRegistered',
  )
}

function remove() {
  void write(
    () => api(clientPath(guildId.value), { method: 'DELETE' }),
    'admin.signInLink.savedRemoved',
  )
}

/** The credential's own request, which is why saving the form above cannot
 *  touch it. The value is passed straight through and never held anywhere
 *  else — not in a ref on this page, not in the outcome, not in a log. */
function storeSecret(secret: string) {
  void write(
    () => api(clientSecretPath(guildId.value), {
      method: 'PUT',
      body: { client_secret: secret },
    }),
    'admin.signInLink.savedSecretSet',
  )
}

/** `DELETE` rather than an empty `PUT`: clearing is a different act with a
 *  different route, and the registration stays behind so that nobody else
 *  can claim the slug in the meantime. */
function clearSecret() {
  void write(
    () => api(clientSecretPath(guildId.value), { method: 'DELETE' }),
    'admin.signInLink.savedSecretCleared',
  )
}

function startEditing() {
  outcome.value = null
  removing.value = false
  editing.value = true
}

/**
 * Copies the link, where the browser allows it.
 *
 * A convenience over the box, never the only way to get the value: the
 * input beside it holds the whole link, is selectable, and is readable by a
 * screen reader. `navigator.clipboard` is absent over plain HTTP and can be
 * refused outright, so the failure is silent and the box is still there.
 */
async function copyLink() {
  if (!link.value) return
  try {
    await navigator.clipboard.writeText(link.value)
    copied.value = true
  } catch {
    copied.value = false
  }
}

/** The tone of the link panel's border. Three states, three colours: a
 *  registration that cannot sign anybody in yet drawn in the same green as
 *  a working one is the lie this page exists to not tell. */
const stateColour = computed(() => ({
  absent: 'var(--border)',
  incomplete: 'var(--color-brand-yellow)',
  live: 'var(--positive)',
}[state.value.tone]))
</script>

<template>
  <div class="mx-auto flex max-w-4xl flex-col gap-4">
    <header>
      <h1 class="text-2xl font-semibold">{{ $t('admin.signInLink.title') }}</h1>
      <p class="mt-1 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.signInLink.intro') }}
      </p>
      <!-- What happens if this page is left alone, said before anything
           that could be typed into. Nothing here is required of anybody. -->
      <p class="mt-2 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.signInLink.optional') }}
      </p>
    </header>

    <!-- The list of servers itself could not be read. Asked before "you
         administer none", because an empty list and an unread list look
         identical and only one of them is a statement about this person. -->
    <section
      v-if="guildError"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--danger)' }"
    >
      <p class="text-sm font-medium">{{ $t('admin.signInLink.serversFailed') }}</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ say(describeClientError(guildError)) }}
      </p>
    </section>

    <!-- Nobody administers anything. Every one of these endpoints answers
         404 to this person for every guild, so there is nothing to show and
         one paragraph saying how that changes. -->
    <section
      v-else-if="guilds.length === 0"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="text-base font-semibold">{{ $t('admin.signInLink.noGuildsHeading') }}</h2>
      <p class="mt-2 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.signInLink.noGuildsBody') }}
      </p>
      <i18n-t
        keypath="admin.signInLink.noGuildsRole"
        tag="p"
        class="mt-2 max-w-3xl text-sm"
        :style="{ color: 'var(--text-muted)' }"
      >
        <template #setting>
          <code>admin_role_id</code>
        </template>
      </i18n-t>
    </section>

    <template v-else>
      <!-- The switcher scopes the whole page, so it sits above everything
           it scopes. -->
      <section
        class="rounded-2xl border p-4"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <span class="block text-sm font-medium">{{ $t('admin.signInLink.whichServer') }}</span>
        <div class="mt-1 max-w-md">
          <UiSelect
            :options="guildChoices"
            :model-value="selected"
            :label="$t('admin.signInLink.whichServer')"
            @update:model-value="chooseGuildFromMenu"
          />
        </div>
      </section>

      <!-- What the last write did, good or bad, said once and near the
           thing it changed. -->
      <p
        v-if="outcome"
        role="status"
        class="rounded-lg border px-3 py-2 text-sm"
        :style="{
          borderColor: outcome.tone === 'good' ? 'var(--positive)' : 'var(--danger)',
          color: 'var(--text)',
        }"
      >
        {{ say(outcome.message) }}
      </p>

      <div v-if="clientStatus === 'pending' && !clientError" aria-busy="true">
        <p class="sr-only">{{ $t('admin.signInLink.loading') }}</p>
        <div
          class="h-40 animate-pulse rounded-2xl border motion-reduce:animate-none"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        />
      </div>

      <section
        v-else-if="clientError"
        class="rounded-2xl border p-6"
        :style="{ borderColor: 'var(--danger)' }"
      >
        <p class="text-sm font-medium">{{ $t('admin.signInLink.loadFailed') }}</p>
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ say(describeClientError(clientError)) }}
        </p>
        <button
          type="button"
          class="mt-3 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
          :style="{ color: 'var(--action)' }"
          :disabled="clientStatus === 'pending'"
          @click="refresh()"
        >
          {{ $t('error.retry') }}
        </button>
      </section>

      <template v-else-if="settled">
        <!-- What state this guild's link is in, and the link itself. Three
             states, three headings, three colours. -->
        <section
          class="rounded-2xl border p-4"
          :style="{ borderColor: stateColour, background: 'var(--surface)' }"
        >
          <h2 class="text-base font-semibold">{{ $t(state.headingKey) }}</h2>
          <p class="mt-1 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ say(state.detail) }}
          </p>

          <!-- The link, in a box that can be selected, read out and copied.
               Read-only rather than disabled: a disabled input is skipped by
               the keyboard and by several screen readers, and this value is
               the one thing on the page somebody came to take away. -->
          <div v-if="state.showLink" class="mt-3">
            <label class="block text-sm font-medium" for="sign-in-link-value">
              {{ $t('admin.signInLink.linkLabel') }}
            </label>
            <div class="mt-1 flex flex-wrap items-center gap-2">
              <input
                id="sign-in-link-value"
                :value="link"
                type="text"
                readonly
                spellcheck="false"
                class="min-w-0 flex-1 rounded-lg border px-3 py-2 font-mono text-sm"
                :style="{
                  borderColor: 'var(--control-border)',
                  background: 'var(--surface-sunken)',
                  color: 'var(--text)',
                }"
                @focus="($event.target as HTMLInputElement).select()"
              >
              <button
                type="button"
                class="rounded-lg border px-3 py-2 text-sm transition-colors"
                :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
                @click="copyLink"
              >
                {{ copied ? $t('admin.signInLink.copied') : $t('admin.signInLink.copy') }}
              </button>
            </div>
            <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
              {{ $t('admin.signInLink.linkHint') }}
            </p>
          </div>
        </section>

        <!-- The registration. One panel whether there is one yet or not:
             registering and re-registering are the same request. -->
        <section
          class="rounded-2xl border p-4"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <h2 class="text-base font-semibold">
            {{ $t('admin.signInLink.registrationHeading') }}
          </h2>

          <dl v-if="client && !editing" class="mt-3 grid gap-3 sm:grid-cols-2">
            <div>
              <dt class="text-xs" :style="{ color: 'var(--text-muted)' }">
                {{ $t('admin.signInLink.slugLabel') }}
              </dt>
              <dd class="mt-0.5 font-mono text-sm break-all">{{ client.slug }}</dd>
            </div>
            <div>
              <dt class="text-xs" :style="{ color: 'var(--text-muted)' }">
                {{ $t('admin.signInLink.providerLabel') }}
              </dt>
              <dd class="mt-0.5 font-mono text-sm break-all">{{ client.provider }}</dd>
            </div>
            <div>
              <dt class="text-xs" :style="{ color: 'var(--text-muted)' }">
                {{ $t('admin.signInLink.baseUrlLabel') }}
              </dt>
              <dd class="mt-0.5 font-mono text-sm break-all">{{ client.baseUrl }}</dd>
            </div>
            <div>
              <dt class="text-xs" :style="{ color: 'var(--text-muted)' }">
                {{ $t('admin.signInLink.clientIdLabel') }}
              </dt>
              <dd class="mt-0.5 font-mono text-sm break-all">{{ client.clientId }}</dd>
            </div>
            <div class="sm:col-span-2">
              <dt class="text-xs" :style="{ color: 'var(--text-muted)' }">
                {{ $t('admin.signInLink.redirectLabel') }}
              </dt>
              <!-- Named rather than left blank. "Nothing here" and "this
                   deployment's own callback" are the same empty cell and
                   very different configurations. -->
              <dd class="mt-0.5 font-mono text-sm break-all">
                {{ client.redirectUri ?? $t('admin.signInLink.redirectDefaultValue') }}
              </dd>
            </div>
          </dl>

          <p
            v-else-if="!client && !editing"
            class="mt-2 max-w-3xl text-sm"
            :style="{ color: 'var(--text-muted)' }"
          >
            {{ $t('admin.signInLink.registrationNone') }}
          </p>

          <SignInClientForm
            v-if="editing"
            class="mt-3"
            :mode="client ? 'change' : 'register'"
            :initial="client ? clientDraftOf(client) : emptyClientDraft()"
            :busy="busy"
            @submit="save"
            @cancel="editing = false"
          />

          <div v-else class="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-60"
              :style="{ background: 'var(--action)', color: 'var(--action-contrast)' }"
              :disabled="busy"
              @click="startEditing"
            >
              {{ client
                ? $t('admin.signInLink.changeRegistration')
                : $t('admin.signInLink.registerClient') }}
            </button>
            <button
              v-if="client"
              type="button"
              class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
              :style="{ borderColor: 'var(--danger)', color: 'var(--danger)' }"
              :disabled="busy"
              @click="removing = !removing"
            >
              {{ $t('admin.signInLink.removeRegistration') }}
            </button>
          </div>

          <!-- Removing, confirmed, and told what it does. It frees the
               name, which is the consequence nobody expects: a link
               somebody has handed out stops working and the word behind it
               becomes available to another guild. -->
          <div
            v-if="removing && client"
            class="mt-3 rounded-lg border p-3"
            :style="{ borderColor: 'var(--danger)' }"
          >
            <p class="text-sm font-semibold">
              {{ $t('admin.signInLink.removeConfirmHeading') }}
            </p>
            <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
              {{ $t('admin.signInLink.removeConfirmBody') }}
            </p>
            <div class="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-60"
                :style="{ background: 'var(--danger)', color: 'var(--danger-contrast)' }"
                :disabled="busy"
                @click="remove"
              >
                {{ $t('admin.signInLink.removeConfirm') }}
              </button>
              <button
                type="button"
                class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
                :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
                :disabled="busy"
                @click="removing = false"
              >
                {{ $t('admin.signInLink.cancel') }}
              </button>
            </div>
          </div>

          <!-- Written, never read. Its own control and its own routes, so
               that saving the registration above cannot touch it. Only once
               there is a registration to attach it to: the API answers 404
               to a secret written against nothing. -->
          <SignInClientSecret
            v-if="client"
            class="mt-3"
            :client="client"
            :busy="busy"
            @store="storeSecret"
            @clear="clearSecret"
          />
        </section>

        <!-- What this does not govern, said on the page that governs the
             rest of it. -->
        <section
          class="rounded-2xl border p-4"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <h2 class="text-base font-semibold">{{ $t('admin.signInLink.scopeHeading') }}</h2>
          <i18n-t
            :keypath="SCOPE_NOTE_KEY"
            tag="p"
            class="mt-1 max-w-3xl text-sm"
            :style="{ color: 'var(--text-muted)' }"
          >
            <template #command>
              <code>/link</code>
            </template>
          </i18n-t>
        </section>
      </template>
    </template>
  </div>
</template>
