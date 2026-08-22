<script setup lang="ts">
/**
 * The bot's runtime configuration, per guild.
 *
 * Everything that is a *decision* here -- what a `takes_effect` value
 * means in words, whether a key may be cleared, whether a change needs
 * confirming first, whether a value is plausible -- lives in
 * `~/utils/settings` and is tested there. What is left in this file is
 * layout, request plumbing and per-key state.
 *
 * Two things this page refuses to do, both of them on purpose:
 *
 * - **It never says "Saved" and stops there.** `takes_effect` is the
 *   whole reason this page is not a form with a submit button: a value
 *   read once at process start is *not* in force after a successful
 *   write, and reporting it as if it were is exactly the defect the
 *   Discord `/config` replies were built to stop telling.
 * - **It never offers an action it knows will fail.** A required key has
 *   no clear control at all, only the sentence saying why.
 */
import {
  type SettingView,
  type WriteOutcome,
  chooseGuild,
  clearability,
  confirmation,
  describeError,
  effectBadge,
  fieldHints,
  guildLabel,
  inputKind,
  keyLabel,
  missingRequired,
  orderSettings,
  parseGuilds,
  parseSettings,
  readSelectedGuild,
  validateValue,
  writeOutcome,
  writeSelectedGuild,
} from '~/utils/settings'

useHead({ title: 'Bot Settings' })

const api = useApi()

const { data: guildData, error: guildError } = await useAsyncData('settings-guilds', async () =>
  parseGuilds(await api('/guilds')),
)

const guilds = computed(() => guildData.value ?? [])

// Server-side there is no browser and therefore no remembered choice, so
// the first render picks the first guild. The remembered one is applied
// after hydration -- the same trade the sidebar makes: a correct first
// paint for everybody, and one repaint for the person who has two guilds
// and last edited the second.
const selected = ref<string | null>(chooseGuild(guilds.value, null))

onMounted(() => {
  selected.value = chooseGuild(guilds.value, readSelectedGuild(window.localStorage))
})

function selectGuild(guildId: string) {
  selected.value = guildId
  if (import.meta.client) writeSelectedGuild(window.localStorage, guildId)
}

// The guild the rows belong to travels *with* the rows rather than in a
// ref of its own. A ref set inside the fetcher would be null after
// hydration -- the server ran the fetch, the client did not -- and the
// list would vanish on every first paint.
const {
  data: settingData,
  error: settingError,
  status: settingStatus,
  refresh,
} = await useAsyncData(
  'settings-keys',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, views: [] as SettingView[] }
    return {
      guildId,
      views: orderSettings(parseSettings(await api(`/guilds/${guildId}/settings`))),
    }
  },
  { watch: [selected] },
)

/** Nothing is shown while the answer on hand belongs to another guild.
 *  An administrator of two servers editing values loaded from the other
 *  one is the exact confusion the switcher exists to prevent, and a list
 *  that lingers for a few hundred milliseconds under the new heading is
 *  long enough to type into. */
const views = computed(() =>
  settingData.value && settingData.value.guildId === selected.value ? settingData.value.views : [],
)
const missing = computed(() => missingRequired(views.value))
const currentGuild = computed(() => guilds.value.find((guild) => guild.id === selected.value) ?? null)

/** The key whose confirmation panel is open, and what it would do. */
const confirming = ref<{ key: string; action: 'save' | 'clear' } | null>(null)

/** The value in each field. Separate from the loaded view so an unsaved
 *  edit is visibly not what the bot is using. */
const drafts = ref<Record<string, string>>({})
const busy = ref<Record<string, boolean>>({})
const failures = ref<Record<string, string>>({})
const outcomes = ref<Record<string, WriteOutcome | null>>({})

// Every key name exists in every guild, so a draft left over from the
// previous selection would silently reappear in the new one's field --
// and be written to the wrong server by somebody who never typed it
// there.
watch(selected, () => {
  drafts.value = {}
  busy.value = {}
  failures.value = {}
  outcomes.value = {}
  confirming.value = null
})
// A reload must not throw away an edit somebody is in the middle of, so
// only keys with no draft yet are seeded. The key that was just written is
// reseeded explicitly by `commit`, which is the one case where the server
// knows better than the field.
watch(
  views,
  (loaded) => {
    for (const view of loaded) {
      if (drafts.value[view.key] === undefined) drafts.value[view.key] = view.value ?? ''
    }
  },
  { immediate: true },
)

const edited = (view: SettingView) => (drafts.value[view.key] ?? '') !== (view.value ?? '')

/** The client-side objection to what is currently typed, if any. Shown
 *  while it is typed rather than after Save, which is the whole point of
 *  checking anything on this side at all -- the server's verdict is the
 *  one that decides, and arrives either way. */
function liveIssue(view: SettingView): string {
  if (!edited(view)) return ''
  const checked = validateValue(view, drafts.value[view.key] ?? '')
  return checked.ok ? '' : checked.message
}

/** Why a clear is not offered. Empty when it is. */
function clearReason(view: SettingView): string {
  const verdict = clearability(view)
  return verdict.clearable ? '' : verdict.reason
}

function request(view: SettingView, action: 'save' | 'clear') {
  failures.value[view.key] = ''
  if (action === 'save') {
    const checked = validateValue(view, drafts.value[view.key] ?? '')
    if (!checked.ok) {
      failures.value[view.key] = checked.message
      return
    }
  }
  // The warning comes *before* the write, never after it. Invalidating
  // consent stops people being recorded -- including in a meeting running
  // right now -- and that is not something to discover from the meeting.
  if (confirmation(view)) {
    confirming.value = { key: view.key, action }
    return
  }
  void commit(view, action)
}

async function commit(view: SettingView, action: 'save' | 'clear') {
  confirming.value = null
  failures.value[view.key] = ''
  outcomes.value[view.key] = null
  busy.value[view.key] = true
  const guildId = selected.value
  try {
    let written = view
    if (action === 'save') {
      const checked = validateValue(view, drafts.value[view.key] ?? '')
      if (!checked.ok) {
        failures.value[view.key] = checked.message
        return
      }
      const reread = await api<Record<string, unknown>>(
        `/guilds/${guildId}/settings/${view.key}`,
        { method: 'PUT', body: { value: checked.value } },
      )
      // The endpoint answers with the key re-read, which is the honest
      // source for what happens next -- not the description this page
      // happened to load a minute ago.
      written = parseSettings([{ key: view.key, ...reread }])[0] ?? view
    } else {
      await api(`/guilds/${guildId}/settings/${view.key}`, { method: 'DELETE' })
    }
    outcomes.value[view.key] = writeOutcome(written, action === 'save' ? 'saved' : 'cleared')
    await refresh()
    const fresh = views.value.find((candidate) => candidate.key === view.key)
    drafts.value[view.key] = fresh?.value ?? ''
  } catch (error) {
    failures.value[view.key] = describeError(error)
  } finally {
    busy.value[view.key] = false
  }
}

/** The four tones are four colours on purpose: a change that needs a pod
 *  restart rendered in the same green as one already in force is the lie
 *  this page exists to avoid. */
const TONE_COLOUR: Record<string, string> = {
  live: 'var(--positive)',
  soon: 'var(--action)',
  restart: 'var(--color-brand-yellow)',
  unknown: 'var(--color-brand-magenta)',
}
</script>

<template>
  <div class="max-w-3xl">
    <h1 class="mb-1 text-2xl font-semibold">Settings</h1>
    <p class="mb-8 text-sm" :style="{ color: 'var(--text-muted)' }">
      Sturnus's runtime configuration for one server. Every key says when a change to it actually
      reaches the running bot.
    </p>

    <p
      v-if="guildError"
      class="rounded-xl border p-4 text-sm"
      :style="{ borderColor: 'var(--danger)', background: 'var(--surface)' }"
    >
      {{ describeError(guildError) }}
    </p>

    <!-- Somebody who administers nothing gets the reason and the way in,
         not an empty page. -->
    <section
      v-else-if="guilds.length === 0"
      class="rounded-xl border p-6"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="mb-2 text-base font-semibold">There is nothing here for you yet</h2>
      <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
        This section configures the bot for one Discord server, and it is open to the
        administrators of a server where Sturnus is running. You administer none of them right now.
      </p>
      <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
        Administrators are the members holding the Discord role that server names in its
        <code class="rounded bg-[var(--surface-raised)] px-1">admin_role_id</code> setting. Somebody
        who already has it can grant you that role, or point
        <code class="rounded bg-[var(--surface-raised)] px-1">admin_role_id</code> at a role you
        hold — Sturnus mirrors the membership from Discord, so the change reaches this console on
        its own.
      </p>
    </section>

    <template v-else>
      <section
        class="mb-6 rounded-xl border p-4"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <!-- With more than one guild the switcher is the only thing
             standing between an administrator and editing the wrong
             server, so the current one is named here and repeated in
             every panel heading below. -->
        <label
          v-if="guilds.length > 1"
          class="mb-2 block text-xs font-medium uppercase tracking-wide"
          :style="{ color: 'var(--text-muted)' }"
          for="guild-switcher"
        >
          Editing which server
        </label>
        <select
          v-if="guilds.length > 1"
          id="guild-switcher"
          class="w-full rounded-lg border px-3 py-2 text-sm"
          :style="{
            borderColor: 'var(--border)',
            background: 'var(--surface-raised)',
            color: 'var(--text)',
          }"
          :value="selected"
          @change="selectGuild(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="guild in guilds" :key="guild.id" :value="guild.id">
            {{ guildLabel(guild) }}
          </option>
        </select>
        <p v-else class="text-sm">
          <span :style="{ color: 'var(--text-muted)' }">Editing</span>
          <span class="ml-1 font-medium">{{ currentGuild ? guildLabel(currentGuild) : '—' }}</span>
        </p>
        <p v-if="currentGuild" class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
          Guild ID
          <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">{{ currentGuild.id }}</code>
        </p>
      </section>

      <p
        v-if="settingError"
        class="mb-6 rounded-xl border p-4 text-sm"
        :style="{ borderColor: 'var(--danger)', background: 'var(--surface)' }"
      >
        {{ describeError(settingError) }}
      </p>

      <section
        v-else-if="missing.length > 0"
        class="mb-6 rounded-xl border p-4"
        :style="{ borderColor: 'var(--color-brand-yellow)', background: 'var(--surface)' }"
      >
        <h2 class="mb-1 text-sm font-semibold">Sturnus is not watching this server yet</h2>
        <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ missing.length }} required
          {{ missing.length === 1 ? 'key has' : 'keys have' }} no value, and the bot builds nothing
          for a server until all of them do:
          <code
            v-for="key in missing"
            :key="key"
            class="mr-1 rounded bg-[var(--surface-raised)] px-1 font-mono"
            >{{ key }}</code
          >
        </p>
      </section>

      <p
        v-if="settingStatus === 'pending'"
        class="text-sm"
        :style="{ color: 'var(--text-muted)' }"
      >
        Reading this server's configuration…
      </p>

      <div class="flex flex-col gap-4">
        <article
          v-for="view in views"
          :key="view.key"
          class="rounded-xl border p-4"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <header class="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h2 class="text-sm font-semibold">
                {{ keyLabel(view.key) }}
                <span
                  v-if="view.required"
                  class="ml-1 align-middle text-xs font-normal"
                  :style="{ color: 'var(--text-muted)' }"
                  >· required</span
                >
              </h2>
              <code class="font-mono text-xs" :style="{ color: 'var(--text-muted)' }">{{
                view.key
              }}</code>
            </div>
            <!-- When a change lands is said before the edit as well as
                 after it: knowing a key needs a pod restart is worth far
                 more while deciding to change it. -->
            <span
              class="shrink-0 self-start rounded-full border px-2.5 py-1 text-xs font-medium"
              :style="{
                borderColor: TONE_COLOUR[effectBadge(view).tone],
                color: TONE_COLOUR[effectBadge(view).tone],
              }"
              :title="effectBadge(view).detail"
            >
              {{ effectBadge(view).label }}
            </span>
          </header>

          <textarea
            v-if="inputKind(view) === 'multiline'"
            v-model="drafts[view.key]"
            :aria-label="keyLabel(view.key)"
            :disabled="busy[view.key]"
            rows="4"
            class="w-full rounded-lg border px-3 py-2 font-mono text-sm"
            :style="{
              borderColor: 'var(--border)',
              background: 'var(--surface-raised)',
              color: 'var(--text)',
            }"
          />
          <!-- `type="text"` with a numeric inputmode rather than
               `type="number"`: `admin_role_id` is an integer key *and* a
               Discord snowflake, and a number input rounds it through a
               float and silently returns an id ending in other digits. -->
          <input
            v-else
            v-model="drafts[view.key]"
            :aria-label="keyLabel(view.key)"
            :disabled="busy[view.key]"
            type="text"
            :inputmode="inputKind(view) === 'integer' ? 'numeric' : 'text'"
            class="w-full rounded-lg border px-3 py-2 font-mono text-sm"
            :style="{
              borderColor: 'var(--border)',
              background: 'var(--surface-raised)',
              color: 'var(--text)',
            }"
          >

          <p
            v-for="hint in fieldHints(view)"
            :key="hint"
            class="mt-1.5 text-xs"
            :style="{ color: 'var(--text-muted)' }"
          >
            {{ hint }}
          </p>

          <p
            v-if="liveIssue(view)"
            class="mt-1.5 text-xs"
            :style="{ color: 'var(--color-brand-yellow)' }"
          >
            {{ liveIssue(view) }}
          </p>

          <div class="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              class="rounded-lg px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
              :style="{
                background:
                  'linear-gradient(120deg, var(--color-brand-blue), var(--color-brand-magenta))',
              }"
              :disabled="busy[view.key] || !edited(view)"
              @click="request(view, 'save')"
            >
              {{ busy[view.key] ? 'Writing…' : 'Save' }}
            </button>

            <button
              v-if="clearability(view).clearable"
              type="button"
              class="rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
              :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
              :disabled="busy[view.key]"
              @click="request(view, 'clear')"
            >
              Clear
            </button>
            <!-- No clear button for a required key. The API answers 409,
                 and an interface that offers an action it knows will fail
                 is worse than one that explains why it cannot. -->
            <span v-else class="text-xs" :style="{ color: 'var(--text-muted)' }">
              {{ clearReason(view) }}
            </span>

            <span v-if="edited(view)" class="text-xs" :style="{ color: 'var(--text-muted)' }">
              Not written yet
            </span>
          </div>

          <div
            v-if="confirming && confirming.key === view.key"
            class="mt-3 rounded-lg border p-3"
            :style="{ borderColor: 'var(--danger)', background: 'var(--surface-raised)' }"
          >
            <p class="mb-1 text-sm font-semibold">{{ confirmation(view)?.title }}</p>
            <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ confirmation(view)?.consequence }}
            </p>
            <div class="flex flex-wrap gap-2">
              <button
                type="button"
                class="rounded-lg px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90"
                :style="{ background: 'var(--danger)' }"
                @click="commit(view, confirming.action)"
              >
                {{ confirmation(view)?.confirmLabel }}
              </button>
              <button
                type="button"
                class="rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface)]"
                :style="{ borderColor: 'var(--border)' }"
                @click="confirming = null"
              >
                Cancel
              </button>
            </div>
          </div>

          <div
            v-if="outcomes[view.key]"
            class="mt-3 rounded-lg border p-3"
            :style="{
              borderColor: TONE_COLOUR[outcomes[view.key]!.tone],
              background: 'var(--surface-raised)',
            }"
          >
            <p class="text-sm font-semibold">{{ outcomes[view.key]!.headline }}</p>
            <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ outcomes[view.key]!.detail }}
            </p>
          </div>

          <p
            v-if="failures[view.key]"
            class="mt-3 rounded-lg border p-3 text-sm"
            :style="{ borderColor: 'var(--danger)' }"
          >
            {{ failures[view.key] }}
          </p>
        </article>
      </div>
    </template>
  </div>
</template>
