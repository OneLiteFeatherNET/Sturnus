<script setup lang="ts">
/**
 * Setting a server up without leaving the browser.
 *
 * The API for this shipped in #149 and nothing rendered it: an
 * administrator set a guild up by running `/setup` in Discord, or by
 * calling three endpoints by hand. This is the page.
 *
 * **What it is not is a form that writes configuration.** `api` holds no
 * Discord token and never will (Spec 13.2), so it cannot create the
 * consent role, deny `Speak` to `@everyone` or register the command tree.
 * The console writes an *intent*; the bot's ten-second reconcile tick
 * makes it true through the same planner `/setup` uses and writes back
 * what happened. Everything on this page that looks like latency is that
 * round trip, and the page says so rather than showing a spinner over a
 * write that has already returned.
 *
 * Every decision is in `~/utils/onboarding` and tested there — what a
 * status means, whether an empty channel list is a server without rooms or
 * a server nobody has looked at, which channels a request may name, how
 * long to keep watching. What is left here is layout, request plumbing and
 * the timer.
 *
 * Four things this page refuses to do, all four on purpose:
 *
 * - **It never draws a superseded request as a failure.** Nothing went
 *   wrong to one: a newer ask replaced it before the bot reached it, and
 *   it was never attempted. Red on that row would send somebody to check a
 *   permission that was never tested.
 * - **It never renders an empty channel picker without saying which
 *   emptiness it is.** `bot.has_arrived` is false exactly while nothing
 *   has been mirrored. "This server has no voice channels" and "nobody has
 *   looked yet" are opposite instructions, and a picker that could not
 *   tell them apart would send somebody hunting for a bug that is not
 *   there.
 * - **It never offers to untick a channel this server already records.**
 *   Setting up *adds* to `voice_channel_ids` and never removes from it, so
 *   that control would do nothing. The rows are ticked and disabled, and
 *   one sentence points at the page where removal actually lives.
 * - **It never disables the form while a request is pending.** The API
 *   deliberately accepts a second request over a first — refusing one
 *   would leave somebody who mistyped a channel waiting out a tick before
 *   they could correct it. So the button stays live, and
 *   {@link resubmitNote} says what pressing it does.
 */
import UiSelect from '~/components/ui/UiSelect.vue'
import { channelChoices, mirrorFreshness, parseDirectory, resolveChoice } from '~/utils/directory'
import { figureMoment } from '~/utils/format'
import type { Message } from '~/utils/message'
import {
  INVITE_PATH,
  POLL_INTERVAL_MS,
  describeSetupError,
  draftProblem,
  parseInvite,
  parseSetupState,
  pickerState,
  reportRequest,
  requestBody,
  requesterLabel,
  resubmitNote,
  setupPath,
  shouldPoll,
  storedChannels,
  submittedChannels,
} from '~/utils/onboarding'
import {
  chooseGuild,
  guildOptions,
  parseGuilds,
  parseSettings,
  readSelectedGuild,
  writeSelectedGuild,
} from '~/utils/settings'

const { t } = useI18n()
const api = useApi()
const say = useSay()
const session = useSession()

useHead(() => ({ title: t('admin.onboarding.title') }))

/** Whose request is whose. The supersede rule turns on there being two
 *  administrators, so "you asked" and "somebody else did" is the first
 *  thing anybody wants of a request they did not expect. */
const viewer = computed(() => session.value?.discord_user_id ?? null)

/* -------------------------------------------------------------------- */
/* Which server                                                          */
/* -------------------------------------------------------------------- */

const {
  data: guildData,
  error: guildError,
  status: guildStatus,
  refresh: refreshGuilds,
} = await useAsyncData('onboarding-guilds', async () => parseGuilds(await api('/guilds')))

const guilds = computed(() => guildData.value ?? [])

// Server-side there is no browser and therefore no remembered choice, so
// the first render picks the first guild and the remembered one is applied
// after hydration -- the same trade every other admin page makes, and the
// same stored key, so switching servers here carries to Bot Settings.
const selected = ref<string | null>(chooseGuild(guilds.value, null))

onMounted(() => {
  selected.value = chooseGuild(guilds.value, readSelectedGuild(window.localStorage))
})

function chooseGuildFromMenu(guildId: string | null) {
  if (guildId === null) return
  selected.value = guildId
  if (import.meta.client) writeSelectedGuild(window.localStorage, guildId)
}

const guildChoices = computed(() => guildOptions(guilds.value))

/* -------------------------------------------------------------------- */
/* The one step that happens in a browser                                */
/* -------------------------------------------------------------------- */

const { data: inviteData, error: inviteError } = await useAsyncData(
  'onboarding-invite',
  async () => parseInvite(await api(INVITE_PATH)),
)

const invite = computed(() => inviteData.value ?? null)

/* -------------------------------------------------------------------- */
/* What the guild's setup has got to                                     */
/* -------------------------------------------------------------------- */

// The guild the answer belongs to travels *with* the answer rather than in
// a ref of its own: a ref set inside the fetcher would be null after
// hydration, and an answer rendered for a few hundred milliseconds under
// another server's heading is long enough to be acted on.
const {
  data: setupData,
  error: setupError,
  status: setupStatus,
  refresh: refreshSetup,
} = await useAsyncData(
  'onboarding-setup',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, state: null }
    return { guildId, state: parseSetupState(await api(setupPath(guildId))) }
  },
  { watch: [selected] },
)

const state = computed(() =>
  setupData.value && setupData.value.guildId === selected.value ? setupData.value.state : null,
)

const arrived = computed(() => state.value?.botHasArrived ?? false)

/**
 * The names behind the ids, and the members the mirror knows.
 *
 * Decoration, as on Bot Settings: if this refuses, the page says the names
 * could not be read and nothing else about it changes. It is re-read when
 * the bot arrives, because until then it correctly answers with nothing.
 */
const {
  data: directoryData,
  error: directoryError,
  refresh: refreshDirectory,
} = await useAsyncData(
  'onboarding-directory',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, directory: null }
    return { guildId, directory: parseDirectory(await api(`/guilds/${guildId}/directory`)) }
  },
  { watch: [selected] },
)

const directory = computed(() =>
  directoryData.value && directoryData.value.guildId === selected.value
    ? directoryData.value.directory
    : null,
)

const channels = computed(() => directory.value?.channels ?? [])
const members = computed(() => directory.value?.members ?? [])

/**
 * What this server already records.
 *
 * Read so that the picker can say which rows are not offers. Decoration
 * again: a failure here leaves every channel looking un-recorded, which
 * over-states what a request adds rather than under-stating it — the
 * applier unions the lists either way.
 */
const { data: settingsData, error: settingsError } = await useAsyncData(
  'onboarding-settings',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, stored: '' }
    const views = parseSettings(await api(`/guilds/${guildId}/settings`))
    return {
      guildId,
      stored: views.find((view) => view.key === 'voice_channel_ids')?.value ?? '',
    }
  },
  { watch: [selected] },
)

const storedRaw = computed(() =>
  settingsData.value && settingsData.value.guildId === selected.value
    ? settingsData.value.stored
    : '',
)

const stored = computed(() => storedChannels(storedRaw.value, channels.value))

/* -------------------------------------------------------------------- */
/* The draft                                                             */
/* -------------------------------------------------------------------- */

/** Only what somebody ticked. What the server already records is added by
 *  {@link submittedChannels}, which is also why those rows are disabled. */
const ticked = ref<string[]>([])
const roleName = ref('')

const draft = computed(() => ({
  channelIds: submittedChannels(stored.value, ticked.value),
  consentRoleName: roleName.value,
}))

const problem = computed(() => draftProblem(draft.value))

function toggleChannel(id: string, on: boolean) {
  ticked.value = on
    ? [...ticked.value.filter((each) => each !== id), id]
    : ticked.value.filter((each) => each !== id)
}

function isChosen(id: string): boolean {
  return stored.value.recorded.includes(id) || ticked.value.includes(id)
}

const groups = computed(() => channelChoices(channels.value, '').groups)

const picker = computed(() =>
  pickerState({
    botHasArrived: arrived.value,
    directoryFailed: Boolean(directoryError.value),
    channelCount: channels.value.length,
  }),
)

/** The clock arrives on mount and not before. A server render that
 *  computed an age and a browser render a second later would disagree
 *  about the text of the same paragraph, which Vue reports as a hydration
 *  mismatch. */
const now = ref<number | null>(null)
onMounted(() => {
  now.value = Date.now()
})

const freshness = computed(() => mirrorFreshness(directory.value?.syncedAt ?? null, now.value))

/** How many configured channels the mirror cannot name, as a sentence
 *  that counts in whichever language is reading it. */
const staleCount = computed<Message>(() => ({
  key: 'admin.onboarding.staleHeading',
  params: { count: stored.value.stale.length },
}))

/** How long this page has been asking. Declared here rather than in the
 *  watching section below, because `ask` resets it. */
const attempts = ref(0)

/* -------------------------------------------------------------------- */
/* Asking                                                                */
/* -------------------------------------------------------------------- */

const submitting = ref(false)

/**
 * The newest request as it stood the moment this browser asked.
 *
 * Compared against what later polls answer with, which is how the "the
 * newest ask wins" rule is made visible while it is happening rather than
 * only in a status word. It is not strictly this browser's own row — the
 * POST answers with the guild's newest, which is somebody else's if they
 * asked in the same second — and that is the honest answer to both
 * questions, so it is the one kept.
 */
const submitted = ref<string | null>(null)

/** Why the last ask did not go through. A refusal from the API, never a
 *  refusal by the bot: those arrive on the request itself. */
const refusal = ref<Message | null>(null)

const report = computed(() =>
  state.value
    ? reportRequest(state.value, { viewer: viewer.value, submitted: submitted.value })
    : null,
)

const resubmit = computed(() => (state.value ? resubmitNote(state.value) : null))

async function ask() {
  const guildId = selected.value
  if (submitting.value || problem.value !== null || !guildId) return
  submitting.value = true
  refusal.value = null
  try {
    const answer = parseSetupState(
      await api(setupPath(guildId), { method: 'POST', body: requestBody(draft.value) }),
    )
    // 202 answers with the same payload `GET` does, so there is nothing to
    // re-read: assigning it is one request fewer between pressing the
    // button and seeing what it did.
    setupData.value = { guildId, state: answer }
    submitted.value = answer.request?.id ?? null
    attempts.value = 0
    watchOn()
  } catch (error) {
    refusal.value = describeSetupError(error)
  } finally {
    submitting.value = false
  }
}

/* -------------------------------------------------------------------- */
/* Watching                                                              */
/* -------------------------------------------------------------------- */

const watching = computed(() => shouldPoll(state.value, attempts.value))

let timer: ReturnType<typeof setTimeout> | null = null
/** Checked after every `await`, because `clearTimeout` cannot stop a timer
 *  that has already fired — and the continuation after the await is what
 *  installs the next one. The defect this avoids is `requeuePanel`'s:
 *  twenty reads a minute running for the life of the tab. */
let alive = true

function stopWatching() {
  if (timer !== null) clearTimeout(timer)
  timer = null
}

function watchOn() {
  stopWatching()
  if (!import.meta.client || !alive) return
  if (!shouldPoll(state.value, attempts.value)) return
  timer = setTimeout(async () => {
    timer = null
    attempts.value += 1
    const before = arrived.value
    try {
      await refreshSetup()
    } catch {
      // A poll that failed is not a page that failed: the last good answer
      // is still on screen and the next tick tries again. `useAsyncData`
      // has already put the failure in `setupError` for the panel to show
      // if it persists.
    }
    if (!alive) return
    // The moment the bot arrives there is a channel list to fetch, and
    // nothing else would ever ask for it.
    if (!before && arrived.value) void refreshDirectory()
    watchOn()
  }, POLL_INTERVAL_MS)
}

onMounted(watchOn)

onBeforeUnmount(() => {
  alive = false
  stopWatching()
})

// A different server is a different everything: a different request, a
// different channel list, and a draft that named rooms which are not on
// this server at all.
watch(selected, () => {
  ticked.value = []
  roleName.value = ''
  submitted.value = null
  refusal.value = null
  attempts.value = 0
  watchOn()
})

watch(() => state.value?.request?.status, () => watchOn())

async function lookAgain() {
  attempts.value = 0
  refusal.value = null
  await Promise.all([refreshSetup(), refreshDirectory()])
  watchOn()
}
</script>

<template>
  <div class="mx-auto flex max-w-4xl flex-col gap-4">
    <header>
      <h1 class="text-2xl font-semibold">{{ $t('admin.onboarding.title') }}</h1>
      <p class="mt-1 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.onboarding.intro') }}
      </p>
    </header>

    <!-- Step 1 is above the server picker and outside it, because it is
         the step somebody takes when there is no server to pick yet. -->
    <section
      class="rounded-2xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="text-base font-semibold">{{ $t('admin.onboarding.inviteHeading') }}</h2>
      <p class="mt-1 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.onboarding.inviteBody') }}
      </p>

      <p v-if="inviteError" class="mt-3 text-sm" :style="{ color: 'var(--danger)' }">
        {{ $t('admin.onboarding.inviteFailed') }} {{ say(describeSetupError(inviteError)) }}
      </p>

      <a
        v-else-if="invite?.url"
        :href="invite.url"
        target="_blank"
        rel="noopener noreferrer"
        class="mt-3 inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition-colors"
        :style="{ background: 'var(--action)', color: 'var(--action-contrast)' }"
      >
        {{ $t('admin.onboarding.inviteAction') }}
        <span class="sr-only">{{ $t('admin.onboarding.inviteNewTab') }}</span>
        <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <path d="M14 3v2h3.6l-8.3 8.3 1.4 1.4L19 6.4V10h2V3h-7ZM5 5h5V3H3v18h18v-7h-2v5H5V5Z" />
        </svg>
      </a>

      <!-- `url: null` is a deployment with no application id configured.
           A configuration fact rather than a fault, and the two values the
           API still sends are what somebody ticks in Discord's own URL
           generator instead. -->
      <div
        v-else
        class="mt-3 rounded-lg border border-dashed p-3"
        :style="{ borderColor: 'var(--border)' }"
      >
        <p class="text-sm font-medium">{{ $t('admin.onboarding.inviteMissingHeading') }}</p>
        <i18n-t
          keypath="admin.onboarding.inviteMissingBody"
          tag="p"
          class="mt-1 max-w-3xl text-sm"
          :style="{ color: 'var(--text-muted)' }"
        >
          <template #variable>
            <code>STURNUS_DISCORD_CLIENT_ID</code>
          </template>
        </i18n-t>
        <dl class="mt-2 grid gap-1 text-xs sm:grid-cols-[auto_1fr] sm:gap-x-3">
          <dt :style="{ color: 'var(--text-muted)' }">
            {{ $t('admin.onboarding.inviteScopes') }}
          </dt>
          <dd><code class="break-all">{{ invite?.scopes.join(' ') }}</code></dd>
          <dt :style="{ color: 'var(--text-muted)' }">
            {{ $t('admin.onboarding.invitePermissions') }}
          </dt>
          <dd><code class="break-all">{{ invite?.permissions }}</code></dd>
        </dl>
      </div>

      <!-- The permission that fails latest, said where it can still be
           acted on in the same visit to Discord. No bitmask expresses a
           role's position, so the invitation link cannot ask for it and
           only prose can. -->
      <div
        class="mt-4 rounded-lg border p-3"
        :style="{ borderColor: 'var(--color-brand-yellow)' }"
      >
        <p class="text-sm font-semibold">{{ $t('admin.onboarding.roleOrderHeading') }}</p>
        <p class="mt-1 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ $t('admin.onboarding.roleOrder') }}
        </p>
      </div>
    </section>

    <!-- The list of servers itself could not be read. Asked before "you
         administer none", because an empty list and an unread list look
         identical and only one of them is a statement about this person. -->
    <section
      v-if="guildError"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--danger)' }"
    >
      <p class="text-sm font-medium">{{ $t('admin.onboarding.serversFailed') }}</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ say(describeSetupError(guildError)) }}
      </p>
      <button
        type="button"
        class="mt-3 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
        :style="{ color: 'var(--action)' }"
        :disabled="guildStatus === 'pending'"
        @click="refreshGuilds()"
      >
        {{ $t('error.retry') }}
      </button>
    </section>

    <!-- On every other admin page an empty guild list is a dead end. Here
         it is the ordinary middle of the flow: step 1 has just been done,
         or has not been done yet. -->
    <section
      v-else-if="guilds.length === 0"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="text-base font-semibold">{{ $t('admin.onboarding.noneYetHeading') }}</h2>
      <p class="mt-2 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.onboarding.noneYetBody') }}
      </p>
      <i18n-t
        keypath="admin.onboarding.noneYetRole"
        tag="p"
        class="mt-2 max-w-3xl text-sm"
        :style="{ color: 'var(--text-muted)' }"
      >
        <template #setting>
          <code>admin_role_id</code>
        </template>
      </i18n-t>
      <button
        type="button"
        class="mt-3 rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
        :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
        :disabled="guildStatus === 'pending'"
        @click="refreshGuilds()"
      >
        {{ $t('admin.onboarding.refresh') }}
      </button>
    </section>

    <template v-else>
      <section
        class="rounded-2xl border p-4"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <h2 class="text-base font-semibold">{{ $t('admin.onboarding.serverHeading') }}</h2>
        <div class="mt-2 max-w-md">
          <UiSelect
            :options="guildChoices"
            :model-value="selected"
            :label="$t('admin.onboarding.whichServer')"
            @update:model-value="chooseGuildFromMenu"
          />
        </div>
      </section>

      <!-- `!state` as well as `pending`: this page re-reads itself every
           three seconds while it is watching, and a skeleton keyed off the
           status alone would blank the panel somebody is reading, four
           times a minute. The skeleton is for having no answer yet, which
           is the first load and a change of server. -->
      <div v-if="setupStatus === 'pending' && !state && !setupError" aria-busy="true">
        <p class="sr-only">{{ $t('admin.onboarding.loading') }}</p>
        <div
          class="h-56 animate-pulse rounded-2xl border motion-reduce:animate-none"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        />
      </div>

      <section
        v-else-if="setupError"
        class="rounded-2xl border p-6"
        :style="{ borderColor: 'var(--danger)' }"
      >
        <p class="text-sm font-medium">{{ $t('admin.onboarding.loadFailed') }}</p>
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ say(describeSetupError(setupError)) }}
        </p>
        <button
          type="button"
          class="mt-3 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
          :style="{ color: 'var(--action)' }"
          :disabled="setupStatus === 'pending'"
          @click="lookAgain()"
        >
          {{ $t('error.retry') }}
        </button>
      </section>

      <template v-else>
        <!-- Step 3 -->
        <section
          class="rounded-2xl border p-4"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <h2 class="text-base font-semibold">{{ $t('admin.onboarding.configureHeading') }}</h2>
          <p class="mt-1 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ $t('admin.onboarding.configureBody') }}
          </p>

          <!-- The three ways there can be nothing to tick, each said as
               itself. Only `ready` renders a picker. -->
          <div
            v-if="picker !== 'ready'"
            aria-live="polite"
            class="mt-3 rounded-lg border border-dashed p-4"
            :style="{ borderColor: 'var(--border)' }"
          >
            <p class="flex flex-wrap items-center gap-2 text-sm font-medium">
              <span
                v-if="picker === 'waiting'"
                aria-hidden="true"
                class="inline-block h-2 w-2 shrink-0 animate-pulse rounded-full motion-reduce:animate-none"
                :style="{ background: 'var(--action)' }"
              />
              {{ $t(`admin.onboarding.${picker}Heading`) }}
            </p>
            <p class="mt-1 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ $t(`admin.onboarding.${picker}Body`) }}
            </p>
            <button
              v-if="picker !== 'waiting'"
              type="button"
              class="mt-3 rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
              :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
              @click="lookAgain()"
            >
              {{ $t('admin.onboarding.refresh') }}
            </button>
          </div>

          <template v-else>
            <p class="mt-3 text-sm font-medium">{{ $t('admin.onboarding.channelsLabel') }}</p>
            <p
              class="mt-0.5 text-xs"
              :style="{ color: freshness.stale ? 'var(--color-brand-yellow)' : 'var(--text-muted)' }"
            >
              {{ $t(freshness.key, freshness.params) }}
            </p>

            <div
              role="group"
              :aria-label="$t('admin.onboarding.channelsLabel')"
              class="mt-2 max-h-72 overflow-y-auto rounded-lg border p-2"
              :style="{ borderColor: 'var(--border)', background: 'var(--surface-raised)' }"
            >
              <fieldset
                v-for="(kind, index) in groups"
                :key="`${kind.kind}-${index}`"
                class="mb-3 last:mb-0"
              >
                <!-- A kind this console has no word for is rendered as the
                     kind Discord called it. -->
                <legend
                  class="mb-1 text-xs font-medium uppercase tracking-wide"
                  :style="{ color: 'var(--text-muted)' }"
                >
                  {{ kind.labelKey ? $t(kind.labelKey) : kind.raw }}
                </legend>
                <label
                  v-for="choice in kind.choices"
                  :key="choice.id"
                  class="flex items-start gap-2 rounded-md px-2 py-1.5 hover:bg-[var(--surface)]"
                  :class="stored.recorded.includes(choice.id) ? '' : 'cursor-pointer'"
                >
                  <input
                    type="checkbox"
                    class="mt-1 shrink-0"
                    :checked="isChosen(choice.id)"
                    :disabled="submitting || stored.recorded.includes(choice.id)"
                    @change="toggleChannel(choice.id, ($event.target as HTMLInputElement).checked)"
                  >
                  <span class="min-w-0">
                    <span class="block break-words text-sm">
                      {{ choice.label }}
                      <!-- A word, not only a disabled tick: a state said
                           by nothing but a rendering style is a state
                           nobody has been told. -->
                      <span
                        v-if="stored.recorded.includes(choice.id)"
                        class="ml-1 rounded-full px-2 py-0.5 text-xs"
                        :style="{ background: 'var(--surface-sunken)', color: 'var(--text-muted)' }"
                      >{{ $t('admin.onboarding.alreadyRecorded') }}</span>
                    </span>
                    <code
                      class="block break-all font-mono text-xs"
                      :style="{ color: 'var(--text-muted)' }"
                    >{{ choice.id }}</code>
                  </span>
                </label>
              </fieldset>
            </div>

            <i18n-t
              v-if="stored.recorded.length > 0"
              keypath="admin.onboarding.alreadyRecordedNote"
              tag="p"
              class="mt-2 max-w-3xl text-xs"
              :style="{ color: 'var(--text-muted)' }"
            >
              <template #setting>
                <NuxtLink
                  to="/admin/bot-settings"
                  class="font-medium hover:underline"
                  :style="{ color: 'var(--action)' }"
                >
                  <code>voice_channel_ids</code>
                </NuxtLink>
              </template>
            </i18n-t>

            <!-- A configured channel the mirror cannot name. Never carried
                 into a request: the applier refuses a channel it cannot
                 see, and one refusal fails the whole intent. -->
            <div
              v-if="stored.stale.length > 0"
              class="mt-2 rounded-lg border p-3"
              :style="{ borderColor: 'var(--color-brand-yellow)' }"
            >
              <p class="text-sm font-medium">{{ say(staleCount) }}</p>
              <i18n-t
                keypath="admin.onboarding.staleBody"
                tag="p"
                class="mt-1 max-w-3xl text-xs"
                :style="{ color: 'var(--text-muted)' }"
              >
                <template #setting>
                  <NuxtLink
                    to="/admin/bot-settings"
                    class="font-medium hover:underline"
                    :style="{ color: 'var(--action)' }"
                  >
                    <code>voice_channel_ids</code>
                  </NuxtLink>
                </template>
              </i18n-t>
              <ul class="mt-1 flex flex-wrap gap-2">
                <li
                  v-for="id in stored.stale"
                  :key="id"
                  class="font-mono text-xs"
                  :style="{ color: 'var(--text-muted)' }"
                >
                  {{ id }}
                </li>
              </ul>
            </div>

            <p v-if="settingsError" class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
              {{ say(describeSetupError(settingsError)) }}
            </p>

            <div class="mt-4 max-w-md">
              <label
                for="onboarding-role"
                class="block text-sm font-medium"
              >{{ $t('admin.onboarding.roleNameLabel') }}</label>
              <input
                id="onboarding-role"
                v-model="roleName"
                type="text"
                autocomplete="off"
                :placeholder="$t('admin.onboarding.roleNamePlaceholder')"
                :disabled="submitting"
                aria-describedby="onboarding-role-hint"
                class="mt-1 w-full rounded-lg border px-3 py-2 text-sm disabled:opacity-60"
                :style="{
                  borderColor: 'var(--control-border)',
                  background: 'var(--surface-raised)',
                  color: 'var(--text)',
                }"
              >
              <p
                id="onboarding-role-hint"
                class="mt-1 text-xs"
                :style="{ color: 'var(--text-muted)' }"
              >
                {{ $t('admin.onboarding.roleNameHint') }}
              </p>
            </div>

            <div class="mt-4 flex flex-wrap items-center gap-3">
              <button
                type="button"
                class="rounded-lg px-3 py-2 text-sm font-medium transition-colors disabled:opacity-60"
                :style="{ background: 'var(--action)', color: 'var(--action-contrast)' }"
                :disabled="submitting || problem !== null"
                @click="ask()"
              >
                {{ submitting ? $t('admin.onboarding.submitting') : $t('admin.onboarding.submit') }}
              </button>
              <!-- This page does not offer an action it knows will fail,
                   so the reason sits beside the disabled button rather
                   than arriving as a 400. -->
              <p v-if="problem" class="text-xs" :style="{ color: 'var(--text-muted)' }">
                {{ say(problem) }}
              </p>
            </div>

            <p
              v-if="resubmit"
              class="mt-2 max-w-3xl text-xs"
              :style="{ color: 'var(--text-muted)' }"
            >
              {{ say(resubmit) }}
            </p>

            <p
              v-if="refusal"
              role="status"
              class="mt-3 rounded-lg border px-3 py-2 text-sm"
              :style="{ borderColor: 'var(--danger)', color: 'var(--text)' }"
            >
              {{ say(refusal) }}
            </p>
          </template>
        </section>

        <!-- Step 4. Absent entirely until somebody has asked: a panel
             saying nothing has happened is not worth the room. -->
        <section
          v-if="report"
          class="rounded-2xl border p-4"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <h2 class="text-base font-semibold">{{ $t('admin.onboarding.requestHeading') }}</h2>

          <!-- Announced when it changes, because the change is the point:
               this panel goes from "waiting" to an outcome without
               anybody touching the page, and a reader who is not watching
               it would otherwise never learn that it had. -->
          <div class="mt-2 flex flex-wrap items-center gap-2" aria-live="polite">
            <!-- The word carries the state; the colour only agrees with
                 it. `neutral` is deliberately not the failure colour --
                 nothing went wrong to a superseded request. -->
            <span
              class="rounded-full border px-2.5 py-1 text-xs font-medium"
              :style="{
                borderColor: report.tone === 'bad'
                  ? 'var(--danger)'
                  : report.tone === 'good'
                    ? 'var(--positive)'
                    : report.tone === 'stalled'
                      ? 'var(--color-brand-yellow)'
                      : 'var(--control-border)',
                color: report.tone === 'bad'
                  ? 'var(--danger)'
                  : report.tone === 'good'
                    ? 'var(--positive)'
                    : 'var(--text)',
              }"
            >{{ say(report.badge) }}</span>
            <p class="text-sm font-medium">{{ say(report.heading) }}</p>
          </div>

          <p
            v-for="(note, index) in report.notes"
            :key="index"
            class="mt-2 max-w-3xl text-sm"
            :style="{ color: 'var(--text-muted)' }"
          >
            {{ say(note) }}
          </p>

          <!-- The bot's own words, verbatim and multi-line. Nothing here
               keys off them: they name which channel, which permission and
               what to do about it, which no status word could. -->
          <div
            v-if="report.error"
            class="mt-3 rounded-lg border p-3"
            :style="{ borderColor: 'var(--danger)' }"
          >
            <p class="text-sm font-medium">{{ $t('admin.onboarding.errorHeading') }}</p>
            <p
              class="mt-1 max-w-3xl whitespace-pre-line break-words text-sm"
              :style="{ color: 'var(--text)' }"
            >{{ report.error }}</p>
          </div>

          <dl
            v-if="state?.request"
            class="mt-3 grid gap-x-4 gap-y-1 text-xs sm:grid-cols-[auto_1fr]"
            :style="{ color: 'var(--text-muted)' }"
          >
            <dt class="font-medium">{{ say(requesterLabel(state.request, viewer, members)) }}</dt>
            <dd>
              {{ $t('admin.onboarding.askedAt', {
                moment: say(figureMoment(state.request.requestedAt)),
              }) }}
              <template v-if="state.request.settledAt">
                · {{ $t('admin.onboarding.settledAt', {
                  moment: say(figureMoment(state.request.settledAt)),
                }) }}
              </template>
            </dd>

            <dt class="font-medium">{{ $t('admin.onboarding.askedChannelsLabel') }}</dt>
            <dd>
              <ul class="flex flex-wrap gap-x-3 gap-y-1">
                <!-- An id the mirror has no row for renders as the id it
                     is, which is `~/utils/directory`'s single answer to an
                     unresolved snowflake and not a second one invented
                     here. -->
                <li v-for="id in state.request.channelIds" :key="id" class="break-all">
                  {{ resolveChoice(channels, id).label }}
                </li>
              </ul>
            </dd>

            <dt class="font-medium">{{ $t('admin.onboarding.askedRoleLabel') }}</dt>
            <dd class="break-words">
              {{ state.request.consentRoleName ?? $t('admin.onboarding.askedRoleNone') }}
            </dd>
          </dl>

          <div class="mt-3 flex flex-wrap items-center gap-3">
            <p
              v-if="watching"
              role="status"
              class="flex items-center gap-2 text-xs"
              :style="{ color: 'var(--text-muted)' }"
            >
              <span
                aria-hidden="true"
                class="inline-block h-2 w-2 animate-pulse rounded-full motion-reduce:animate-none"
                :style="{ background: 'var(--action)' }"
              />
              {{ $t('admin.onboarding.watchingLive') }}
            </p>
            <p
              v-else-if="state?.request?.status === 'pending'"
              role="status"
              class="text-xs"
              :style="{ color: 'var(--text-muted)' }"
            >
              {{ $t('admin.onboarding.watchingStopped') }}
            </p>
            <button
              type="button"
              class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
              :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
              @click="lookAgain()"
            >
              {{ $t('admin.onboarding.refresh') }}
            </button>
          </div>

          <!-- What setting up never decides. The link is here rather than
               only in the sentence, because this is the moment somebody
               has finished one thing and is looking for the next. -->
          <div v-if="report.tone === 'good'" class="mt-3 flex flex-wrap gap-4">
            <NuxtLink
              to="/admin/bot-settings"
              class="text-sm font-medium transition-colors hover:underline"
              :style="{ color: 'var(--action)' }"
            >
              {{ $t('admin.onboarding.toBotSettings') }}
            </NuxtLink>
            <NuxtLink
              to="/admin/destinations"
              class="text-sm font-medium transition-colors hover:underline"
              :style="{ color: 'var(--action)' }"
            >
              {{ $t('admin.onboarding.toDestinations') }}
            </NuxtLink>
          </div>
        </section>
      </template>
    </template>
  </div>
</template>
