<script setup lang="ts">
/**
 * The settings a guild changes often, on the page an administrator lands on.
 *
 * Deliberately not Bot Settings in miniature. Which keys belong here is
 * `~/utils/quickSettings`' decision, keyed by setting name rather than
 * hard-coded in this template, and the registry is what decides whether any
 * of them exists: a name nothing serves is simply absent, so a setting
 * renamed or added upstream cannot take the dashboard down.
 *
 * **Every write goes to the same endpoint the full page uses**, and every
 * rule about a key comes from `~/utils/settings`, which is what that page
 * asks too:
 *
 * - `validateValue` before sending, so a typo is caught while the cursor is
 *   still in the field and the value that is sent is the value that page
 *   would have sent.
 * - `clearability` -- the console's mirror of the API's `may_clear` -- to
 *   decide whether a clear is offered at all. A required key has no default
 *   to fall back to, so clearing it takes the guild out of service; the API
 *   answers 409 and this band does not offer the button.
 * - `effectBadge` before the edit and `writeOutcome` after it, so a key that
 *   needs a pod restart never reports itself as being in force. That is the
 *   defect the Discord `/config` replies were built to stop, and a band that
 *   said "Saved" for all three timings would reintroduce it a second time.
 * - `confirmation`, by way of `mayWriteHere`: a key that invalidates consent
 *   is **not offered here at all**, and one sentence says where it lives.
 *   This band has no room for that dialogue, and offering a control that
 *   skips a warning the full page insists on would be worse than not
 *   offering it.
 *
 * Those per-key sentences are still English, exactly as they are on
 * `/admin/bot-settings`. `admin.settings.*` is unpopulated on purpose --
 * converting the administrative area is one sweep that moves all four of its
 * pages together, and half-translating it from here would leave a German
 * page with an English hole in the middle of it. The band's own chrome is
 * keyed.
 *
 * The guild switcher is the existing one rather than a second convention:
 * `chooseGuild`, `sturnus.settings.guild` in `localStorage`, and the
 * "Showing …" line when there is only one. An administrator who last edited
 * their second server finds it selected here too.
 */
import {
  type SettingView,
  type WriteOutcome,
  chooseGuild,
  clearability,
  describeError,
  effectBadge,
  type GuildRef,
  guildLabel,
  inputKind,
  keyLabel,
  orderSettings,
  parseSettings,
  readSelectedGuild,
  validateValue,
  writeOutcome,
  writeSelectedGuild,
} from '~/utils/settings'
import { BOT_SETTINGS_PATH, mayClearHere, mayWriteHere, selectQuickSettings } from '~/utils/quickSettings'

const props = defineProps<{ guilds: readonly GuildRef[] }>()

const api = useApi()

// Server-side there is no browser and therefore no remembered choice, so the
// first render picks the first guild. The remembered one is applied after
// hydration -- the same trade the sidebar and Bot Settings make.
const selected = ref<string | null>(chooseGuild(props.guilds, null))

onMounted(() => {
  selected.value = chooseGuild(props.guilds, readSelectedGuild(window.localStorage))
})

// The guilds arrive lazily from the band above, so the first value this
// component sees may well be an empty list.
watch(
  () => props.guilds,
  (guilds) => {
    if (!guilds.some((guild) => guild.id === selected.value)) {
      selected.value = chooseGuild(
        guilds,
        import.meta.client ? readSelectedGuild(window.localStorage) : null,
      )
    }
  },
  { immediate: true },
)

function selectGuild(guildId: string) {
  selected.value = guildId
  if (import.meta.client) writeSelectedGuild(window.localStorage, guildId)
}

// The guild the rows belong to travels *with* the rows rather than in a ref
// of its own. A ref set inside the fetcher would be null after hydration --
// the server ran the fetch, the client did not -- and the list would vanish
// on every first paint.
const {
  data: settingData,
  error: settingError,
  status: settingStatus,
  refresh,
} = useAsyncData(
  'quick-settings-keys',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, views: [] as SettingView[] }
    return {
      guildId,
      views: orderSettings(parseSettings(await api(`/guilds/${guildId}/settings`))),
    }
  },
  { lazy: true, watch: [selected] },
)

/** Nothing is shown while the answer on hand belongs to another guild. An
 *  administrator of two servers editing values loaded from the other one is
 *  the exact confusion the switcher exists to prevent. */
const views = computed(() =>
  settingData.value && settingData.value.guildId === selected.value ? settingData.value.views : [],
)

const selection = computed(() => selectQuickSettings(views.value))
const currentGuild = computed(
  () => props.guilds.find((guild) => guild.id === selected.value) ?? null,
)
const loading = computed(() => settingStatus.value === 'pending' || settingStatus.value === 'idle')

/** The value in each field. Separate from the loaded view so an unsaved edit
 *  is visibly not what the bot is using. */
const drafts = ref<Record<string, string>>({})
const busy = ref<Record<string, boolean>>({})
const failures = ref<Record<string, string>>({})
const outcomes = ref<Record<string, WriteOutcome | null>>({})

// Every key name exists in every guild, so a draft left over from the
// previous selection would silently reappear in the new one's field -- and
// be written to the wrong server by somebody who never typed it there.
watch(selected, () => {
  drafts.value = {}
  busy.value = {}
  failures.value = {}
  outcomes.value = {}
})

// A reload must not throw away an edit somebody is in the middle of, so only
// keys with no draft yet are seeded. The key that was just written is
// reseeded explicitly by `commit`.
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

/** The client-side objection to what is currently typed, if any. Shown while
 *  it is typed rather than after Save; the server's verdict is the one that
 *  decides, and arrives either way. */
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

async function commit(view: SettingView, action: 'save' | 'clear') {
  // Second gate on the rule `selectQuickSettings` already applied. A key
  // needing a confirmation this band does not host must not be written from
  // it even if a control for it somehow reached the screen.
  if (!mayWriteHere(view)) return
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
      // The endpoint answers with the key re-read, which is the honest source
      // for what happens next -- not the description this page happened to
      // load a minute ago.
      written = parseSettings([{ key: view.key, ...reread }])[0] ?? view
    }
    else {
      await api(`/guilds/${guildId}/settings/${view.key}`, { method: 'DELETE' })
    }
    outcomes.value[view.key] = writeOutcome(written, action === 'save' ? 'saved' : 'cleared')
    await refresh()
    const fresh = views.value.find((candidate) => candidate.key === view.key)
    drafts.value[view.key] = fresh?.value ?? ''
  }
  catch (error) {
    failures.value[view.key] = describeError(error)
  }
  finally {
    busy.value[view.key] = false
  }
}

/** The four tones are four colours on purpose: a change that needs a pod
 *  restart rendered in the same green as one already in force is the lie
 *  this badge exists to avoid. */
const TONE_COLOUR: Record<string, string> = {
  live: 'var(--positive)',
  soon: 'var(--action)',
  restart: 'var(--color-brand-yellow)',
  unknown: 'var(--color-brand-magenta)',
}
</script>

<template>
  <section>
    <div class="mb-1 flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="text-base font-semibold">{{ $t('dashboard.quick.settings.title') }}</h2>
      <NuxtLink
        :to="BOT_SETTINGS_PATH"
        class="text-sm underline underline-offset-2"
        :style="{ color: 'var(--action)' }"
      >
        {{ $t('dashboard.quick.settings.more') }}
      </NuxtLink>
    </div>
    <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('dashboard.quick.settings.intro') }}
    </p>

    <!-- The existing switcher convention rather than a second one: the same
         `localStorage` key Bot Settings writes, and the "Showing …" line
         when there is only one server to show. -->
    <div class="mb-3">
      <label
        v-if="guilds.length > 1"
        class="mb-1 block text-xs font-medium tracking-wide uppercase"
        :style="{ color: 'var(--text-muted)' }"
        for="quick-guild-switcher"
      >
        {{ $t('dashboard.quick.settings.whichServer') }}
      </label>
      <select
        v-if="guilds.length > 1"
        id="quick-guild-switcher"
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
      <p v-else-if="currentGuild" class="text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('dashboard.quick.settings.showing', { guild: guildLabel(currentGuild) }) }}
      </p>
    </div>

    <div v-if="loading" aria-busy="true">
      <p class="sr-only">{{ $t('dashboard.quick.settings.loading') }}</p>
      <div class="flex flex-col gap-4">
        <div
          v-for="n in 3"
          :key="n"
          class="h-32 animate-pulse rounded-xl border motion-reduce:animate-none"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        />
      </div>
    </div>

    <p
      v-else-if="settingError"
      class="rounded-xl border p-4 text-sm"
      :style="{ borderColor: 'var(--danger)', background: 'var(--surface)' }"
    >
      {{ describeError(settingError) }}
    </p>

    <!-- The registry served none of the keys this band edits. A real answer
         rather than a failure, and the whole configuration is one link
         away. -->
    <p
      v-else-if="selection.shown.length === 0 && selection.withheld.length === 0"
      class="rounded-xl border p-4 text-sm"
      :style="{
        borderColor: 'var(--border)',
        background: 'var(--surface)',
        color: 'var(--text-muted)',
      }"
    >
      {{ $t('dashboard.quick.settings.none') }}
    </p>

    <div v-else class="flex flex-col gap-4">
      <article
        v-for="view in selection.shown"
        :key="view.key"
        class="rounded-xl border p-4"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <header class="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <h3 class="text-sm font-semibold">{{ keyLabel(view.key) }}</h3>
            <code class="font-mono text-xs" :style="{ color: 'var(--text-muted)' }">{{
              view.key
            }}</code>
          </div>
          <!-- When a change lands is said before the edit as well as after
               it: knowing a key needs a pod restart is worth far more while
               deciding to change it. -->
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

        <!-- The same two controls the full page picks between, chosen by the
             same `inputKind`. A list of several channel snowflakes runs past
             eighty characters, and in a one-line field it can only be edited
             by scrolling through it a word at a time. -->
        <textarea
          v-if="inputKind(view) === 'multiline'"
          v-model="drafts[view.key]"
          :aria-label="keyLabel(view.key)"
          :disabled="busy[view.key]"
          rows="3"
          class="w-full rounded-lg border px-3 py-2 font-mono text-sm"
          :style="{
            borderColor: 'var(--border)',
            background: 'var(--surface-raised)',
            color: 'var(--text)',
          }"
        />
        <!-- `type="text"` with a numeric inputmode rather than
             `type="number"`: an integer key can also be a Discord snowflake,
             and a number input rounds it through a float and silently
             returns an id ending in other digits. -->
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
          v-if="liveIssue(view)"
          class="mt-1.5 text-xs"
          :style="{ color: 'var(--color-brand-yellow)' }"
        >
          {{ liveIssue(view) }}
        </p>

        <div class="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            class="rounded-lg px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40 motion-reduce:transition-none"
            :style="{
              background:
                'linear-gradient(120deg, var(--color-brand-blue), var(--color-brand-magenta))',
            }"
            :disabled="busy[view.key] || !edited(view)"
            @click="commit(view, 'save')"
          >
            {{
              busy[view.key]
                ? $t('dashboard.quick.settings.saving')
                : $t('dashboard.quick.settings.save')
            }}
          </button>

          <button
            v-if="mayClearHere(view)"
            type="button"
            class="rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40 motion-reduce:transition-none"
            :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
            :disabled="busy[view.key]"
            @click="commit(view, 'clear')"
          >
            {{ $t('dashboard.quick.settings.clear') }}
          </button>
          <!-- No clear button for a required key: the API answers 409, and
               an interface that offers an action it knows will fail is worse
               than one that explains why it cannot. -->
          <span v-else class="text-xs" :style="{ color: 'var(--text-muted)' }">
            {{ clearReason(view) }}
          </span>

          <span v-if="edited(view)" class="text-xs" :style="{ color: 'var(--text-muted)' }">
            {{ $t('dashboard.quick.settings.unsaved') }}
          </span>
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

      <!-- A key this band will not write, named rather than dropped
           silently: an absence nobody explains reads as a control that
           failed to render. -->
      <p
        v-for="key in selection.withheld"
        :key="key"
        class="rounded-xl border p-4 text-sm"
        :style="{
          borderColor: 'var(--border)',
          background: 'var(--surface)',
          color: 'var(--text-muted)',
        }"
      >
        {{ $t('dashboard.quick.settings.withheld', { key: keyLabel(key) }) }}
      </p>
    </div>
  </section>
</template>
