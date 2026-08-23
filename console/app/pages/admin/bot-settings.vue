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
 *
 * Four of the keys hold ids rather than words, and for those the page asks
 * the API for the guild's mirror of Discord and for Outline's collections
 * and offers a picker over names — see `~/utils/directory`. The id is
 * still the value; the picker only spares somebody the copy-paste. Both of
 * those endpoints are decoration: if either one refuses, the keys it
 * served fall back to the text input this page has always had, with one
 * sentence saying why. A configuration page that cannot be used because a
 * name list is down would be worse than one that asks for ids.
 */
import {
  type Choice,
  addToIdList,
  blankOption,
  channelChoices,
  controlKind,
  idListHas,
  mirrorFreshness,
  parseCollections,
  parseDirectory,
  removeFromIdList,
  singleChoices,
  type ControlKind,
  type Freshness,
  type NamedRow,
} from '~/utils/directory'
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

/* -------------------------------------------------------------------- */
/* The names behind the ids                                              */
/* -------------------------------------------------------------------- */

// Both of these are allowed to fail. `useAsyncData` puts a rejection in
// its `error` rather than throwing, which is the whole reason the page can
// treat a missing endpoint as "no pickers today" instead of as a page that
// does not render. The guild travels with the rows for the same reason it
// does above: a channel list belonging to the other server is worse than
// no channel list.
const { data: directoryData, error: directoryError } = await useAsyncData(
  'settings-directory',
  async () => {
    const guildId = selected.value
    if (!guildId) return null
    return { guildId, mirror: parseDirectory(await api(`/guilds/${guildId}/directory`)) }
  },
  { watch: [selected] },
)

const { data: collectionData, error: collectionError } = await useAsyncData(
  'settings-collections',
  async () => parseCollections(await api('/outline/collections')),
)

const mirror = computed(() =>
  directoryData.value && directoryData.value.guildId === selected.value
    ? directoryData.value.mirror
    : null,
)
const collections = computed(() => collectionData.value ?? null)

/** The clock arrives on mount and not before. A server render that
 *  computed an age and a browser render a second later would disagree
 *  about the text of the same paragraph, which Vue reports as a hydration
 *  mismatch — so until this is set the freshness sentence names the
 *  instant and no age, in both renders. */
const now = ref<number | null>(null)
onMounted(() => {
  now.value = Date.now()
})

/** The keys whose picker somebody has stepped around. A mirror can be
 *  stale, and an administrator who knows the id must not be locked out by
 *  a list that has not caught up. */
const manual = ref<Record<string, boolean>>({})

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
  // A picker stepped around on one server says nothing about the next
  // one, whose mirror may be perfectly current.
  manual.value = {}
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

/** Whether the mirror has anything to offer for this key at all. An empty
 *  list is not a picker: a select with no options says "this server has no
 *  roles", which is a claim, and the wrong one. */
function pickerAvailable(view: SettingView): boolean {
  const wanted = controlKind(view)
  if (wanted === 'plain') return false
  if (wanted === 'collection') return (collections.value?.collections.length ?? 0) > 0
  const rows = wanted === 'channels' ? mirror.value?.channels : mirror.value?.roles
  return (rows?.length ?? 0) > 0
}

/** Which control a key actually gets, as opposed to which one it deserves.
 *  `plain` is the text input or textarea this page has always had, and it
 *  is where every key ends up when the names cannot be had. */
function control(view: SettingView): ControlKind {
  if (manual.value[view.key] || !pickerAvailable(view)) return 'plain'
  return controlKind(view)
}

/** True while a key would have had a picker, whether or not it got one. */
const wantsPicker = (view: SettingView) => controlKind(view) !== 'plain'

function rowsFor(view: SettingView): NamedRow[] {
  if (controlKind(view) === 'collection') return collections.value?.collections ?? []
  return mirror.value?.roles ?? []
}

const channelsFor = (view: SettingView) =>
  channelChoices(mirror.value?.channels ?? [], drafts.value[view.key] ?? '')

const singleFor = (view: SettingView) => singleChoices(rowsFor(view), drafts.value[view.key] ?? '')

const currentFor = (view: SettingView): Choice | null => singleFor(view).current

function toggleChannel(view: SettingView, id: string, chosen: boolean) {
  const raw = drafts.value[view.key] ?? ''
  // Only a real add or remove rewrites the value. `10, 11` and `10,11`
  // configure the same two channels, and canonicalising one into the other
  // on sight would make opening this page look like an unsaved edit.
  drafts.value[view.key] = chosen ? addToIdList(raw, id) : removeFromIdList(raw, id)
}

/**
 * Why a key that should have had a picker is showing a text field.
 *
 * Three different sentences, because they ask for three different things:
 * a failed request is somebody's to look into, an unswept mirror resolves
 * itself, and a mirror that swept and found nothing is a server that has
 * no such rows at all.
 */
function fallbackNote(view: SettingView): Freshness {
  if (controlKind(view) === 'collection') {
    if (collectionError.value) {
      return { key: 'admin.settings.collectionsUnavailable', params: {}, stale: true }
    }
    return collections.value?.syncedAt
      ? { key: 'admin.settings.mirrorEmpty', params: {}, stale: true }
      : mirrorFreshness(null, now.value)
  }
  if (directoryError.value) {
    return { key: 'admin.settings.directoryUnavailable', params: {}, stale: true }
  }
  return mirror.value?.syncedAt
    ? { key: 'admin.settings.mirrorEmpty', params: {}, stale: true }
    : mirrorFreshness(null, now.value)
}

/**
 * The sentence under a control, as a list of nought or one.
 *
 * A list rather than a nullable value so the template can `v-for` over it:
 * `$t` needs the key and its arguments together, and a template that had
 * to assert a value is not null three times to render one paragraph is a
 * template nobody edits twice.
 *
 * A picker always says how fresh its list is. A field that would have been
 * a picker says why it is not. A picker somebody has deliberately stepped
 * around says what typing an id here means.
 */
function mirrorNotes(view: SettingView): Freshness[] {
  if (!wantsPicker(view)) return []
  if (manual.value[view.key]) {
    return pickerAvailable(view)
      ? [{ key: 'admin.settings.manualNote', params: {}, stale: false }]
      : [fallbackNote(view)]
  }
  if (control(view) === 'plain') return [fallbackNote(view)]
  const at
    = controlKind(view) === 'collection' ? collections.value?.syncedAt : mirror.value?.syncedAt
  return [mirrorFreshness(at ?? null, now.value)]
}

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

          <!-- A picker over names, for the keys that hold ids. The value
               written is still the id: the checkbox list serialises back
               into the same comma-separated string `voice_channel_ids` has
               always held, and the selects hand over one id each. -->
          <div
            v-if="control(view) === 'channels'"
            role="group"
            :aria-label="keyLabel(view.key)"
            class="max-h-64 overflow-y-auto rounded-lg border p-2"
            :style="{ borderColor: 'var(--border)', background: 'var(--surface-raised)' }"
          >
            <fieldset
              v-for="(group, index) in channelsFor(view).groups"
              :key="`${group.kind}-${index}`"
              class="mb-3 last:mb-0"
            >
              <!-- A kind this console has no word for is rendered as the
                   kind Discord called it. Dropping the group would hide a
                   recordable channel and say nothing about it. -->
              <legend
                class="mb-1 text-xs font-medium uppercase tracking-wide"
                :style="{ color: group.unresolved ? 'var(--color-brand-yellow)' : 'var(--text-muted)' }"
              >
                {{ group.labelKey ? $t(group.labelKey) : group.raw }}
              </legend>
              <label
                v-for="choice in group.choices"
                :key="choice.id"
                class="flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 hover:bg-[var(--surface)]"
              >
                <input
                  type="checkbox"
                  class="mt-1 shrink-0"
                  :checked="idListHas(drafts[view.key] ?? '', choice.id)"
                  :disabled="busy[view.key]"
                  @change="toggleChannel(view, choice.id, ($event.target as HTMLInputElement).checked)"
                >
                <span class="min-w-0">
                  <span class="block break-words text-sm">{{ choice.label }}</span>
                  <code
                    class="block break-all font-mono text-xs"
                    :style="{ color: 'var(--text-muted)' }"
                    >{{ choice.id }}</code
                  >
                </span>
              </label>
            </fieldset>
          </div>

          <!-- `:value` and `@change` rather than `v-model`: the option
               values are the ids the mirror holds, and a stored value with
               a stray space would match none of them and leave the select
               showing the first role as though it were configured. -->
          <select
            v-else-if="control(view) === 'role' || control(view) === 'collection'"
            :aria-label="keyLabel(view.key)"
            :disabled="busy[view.key]"
            class="w-full rounded-lg border px-3 py-2 text-sm"
            :style="{
              borderColor: 'var(--border)',
              background: 'var(--surface-raised)',
              color: 'var(--text)',
            }"
            :value="currentFor(view)?.id ?? ''"
            @change="drafts[view.key] = ($event.target as HTMLSelectElement).value"
          >
            <!-- "Not set" is offered only where an empty value is one the
                 API accepts. For a required or integer key it is a
                 placeholder that disappears once something is chosen --
                 this page does not offer an action it knows will fail. -->
            <option
              v-if="blankOption(view, currentFor(view)) === 'placeholder'"
              value=""
              disabled
            >
              {{ $t('admin.settings.choose') }}
            </option>
            <option v-else-if="blankOption(view, currentFor(view)) === 'offer'" value="">
              {{ $t('admin.settings.notSet') }}
            </option>
            <option v-for="choice in singleFor(view).choices" :key="choice.id" :value="choice.id">
              {{ choice.label }}
            </option>
          </select>

          <textarea
            v-else-if="inputKind(view) === 'multiline'"
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

          <!-- The id under the name, in the same monospace face the key
               itself is rendered in. The name is what a human recognises;
               the id is what is stored, and it stays on screen so that the
               two are never in doubt. -->
          <template v-if="control(view) === 'channels'">
            <p class="mt-1.5 text-xs" :style="{ color: 'var(--text-muted)' }">
              <template v-if="channelsFor(view).selected.length === 0">
                {{ $t('admin.settings.noChannelsChosen') }}
              </template>
              <template v-else>
                {{ $t('admin.settings.chosenIds') }}
                <code
                  v-for="choice in channelsFor(view).selected"
                  :key="choice.id"
                  class="ml-1 inline-block break-all rounded bg-[var(--surface-raised)] px-1 font-mono"
                  >{{ choice.id }}</code
                >
              </template>
            </p>
            <!-- A chosen channel the mirror has no row for. Said out loud
                 rather than left as a tick beside a bare number: it is a
                 configuration problem, and this is the only page that can
                 show it. -->
            <p
              v-if="channelsFor(view).selected.some((choice) => !choice.resolved)"
              class="mt-1.5 text-xs"
              :style="{ color: 'var(--color-brand-yellow)' }"
            >
              {{ $t('admin.settings.unresolvedChannel') }}
            </p>
          </template>

          <template v-else-if="control(view) === 'role' || control(view) === 'collection'">
            <p v-if="currentFor(view)" class="mt-1.5 text-xs" :style="{ color: 'var(--text-muted)' }">
              <code class="break-all rounded bg-[var(--surface-raised)] px-1 font-mono">{{
                currentFor(view)?.id
              }}</code>
            </p>
            <p
              v-if="currentFor(view) && currentFor(view)?.resolved === false"
              class="mt-1.5 text-xs"
              :style="{ color: 'var(--color-brand-yellow)' }"
            >
              {{
                control(view) === 'collection'
                  ? $t('admin.settings.unresolvedCollection')
                  : $t('admin.settings.unresolvedRole')
              }}
            </p>
          </template>

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

          <!-- How fresh the list is, or why there is no list. A picker
               that silently offered a copy of last week is how somebody
               configures a channel that was deleted on Tuesday. -->
          <p
            v-for="note in mirrorNotes(view)"
            :key="note.key"
            class="mt-1.5 text-xs"
            :style="{ color: note.stale ? 'var(--color-brand-yellow)' : 'var(--text-muted)' }"
          >
            {{ $t(note.key, note.params) }}
          </p>

          <!-- The way past the picker. A mirror can be behind Discord, and
               an administrator who already knows the id must not be locked
               out by a list that has not caught up. -->
          <p v-if="pickerAvailable(view)" class="mt-1.5 text-xs">
            <button
              type="button"
              class="underline underline-offset-2 transition-colors hover:text-[var(--text)]"
              :style="{ color: 'var(--text-muted)' }"
              @click="manual[view.key] = !manual[view.key]"
            >
              {{
                manual[view.key]
                  ? $t('admin.settings.backToPicker')
                  : $t('admin.settings.enterIdManually')
              }}
            </button>
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
            <!-- No clear button for a key the API will not clear -- its
                 own `may_clear`, not "not required" guessed here. The two
                 differ for the deprecated voice_channel_id, which is
                 optional and still answers 409; an interface that offers
                 an action it knows will fail is worse than one that
                 explains why it cannot. -->
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
