<script setup lang="ts">
/**
 * Where a guild's protocols go.
 *
 * A guild has been able to publish to several destinations in several
 * formats since #144, and none of it was reachable from the interface:
 * `document_target` on Bot Settings still looked like the only answer, and
 * it is now the *fallback* for a guild that configures nothing here. This
 * page is what makes the difference visible, and it says which of the two
 * is actually in force rather than leaving them looking like rival
 * settings — see `fallbackNote`.
 *
 * Everything that is a *decision* lives in `~/utils/exportTargets` and is
 * tested there: which formats are offered and why two are absent, what a
 * format will accept as an address, which destination Discord announces,
 * what may be done to a credential, and what is wrong with a draft. What
 * is left in this file is layout, request plumbing and which panel is
 * open. The guild picker is not reimplemented either — it is the same
 * functions Bot Settings and the Queue use, remembered choice included, so
 * switching servers on one admin page carries to the others.
 *
 * Three things this page refuses to do, all three on purpose:
 *
 * - **It never renders a box for a credential beside a credential.** The
 *   destination form has no secret field at all; `ExportTargetSecret` is a
 *   separate control with its own request, and its input does not exist
 *   until somebody deliberately opens it. An empty password box saved with
 *   the rest of a form is how a working token gets wiped by somebody
 *   correcting a typo in a name.
 * - **It never decides for itself which formats exist.** `GET
 *   /api/export-formats` is read here and handed to the form, which is
 *   what lets a format this deployment does not build appear in the picker
 *   as a disabled row saying so instead of being silently absent. #150 had
 *   to be absent because there was no endpoint to ask; the argument for
 *   changing that answer, now that there is, is at the top of
 *   `~/utils/exportTargets`.
 * - **It never re-reads a guild's destinations under another guild's
 *   heading.** The answer travels with the guild it belongs to, the way
 *   the queue page's figures do: a list that lingers for a few hundred
 *   milliseconds is long enough to be acted on.
 */
import UiDisclosureList from '~/components/ui/UiDisclosureList.vue'
import UiPagination from '~/components/ui/UiPagination.vue'
import UiSelect from '~/components/ui/UiSelect.vue'
import { parseCollections } from '~/utils/directory'
import {
  type ExportTarget,
  type TargetDraft,
  FORMATS_PATH,
  describeTargetError,
  draftBody,
  draftOf,
  emptyDraft,
  enabledLabelKey,
  fallbackNote,
  orderTargets,
  parseFormats,
  parseTargets,
  primaryTarget,
  takenNames,
  targetPath,
  targetSecretPath,
  targetSummary,
  targetsPath,
} from '~/utils/exportTargets'
import type { Message } from '~/utils/message'
import { PAGE_SIZE } from '~/utils/paging'
import {
  chooseGuild,
  guildOptions,
  parseGuilds,
  readSelectedGuild,
  writeSelectedGuild,
} from '~/utils/settings'
import { paginationView } from '~/utils/uiPagination'

const { t } = useI18n()
const api = useApi()
const say = useSay()

useHead(() => ({ title: t('admin.destinations.title') }))

const { data: guildData, error: guildError } = await useAsyncData(
  'destinations-guilds',
  async () => parseGuilds(await api('/guilds')),
)

const guilds = computed(() => guildData.value ?? [])

// Server-side there is no browser and therefore no remembered choice, so
// the first render picks the first guild. The remembered one is applied
// after hydration -- the same trade the sidebar and the other admin pages
// make: a correct first paint for everybody, and one repaint for the
// person who has two guilds and last worked on the second.
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

// The guild the rows belong to travels *with* the rows rather than in a
// ref of its own. A ref set inside the fetcher would be null after
// hydration -- the server ran the fetch, the client did not.
const {
  data: targetData,
  error: targetError,
  status: targetStatus,
  refresh,
} = await useAsyncData(
  'destinations-targets',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, targets: [] as ExportTarget[] }
    return { guildId, targets: parseTargets(await api(targetsPath(guildId))) }
  },
  { watch: [selected] },
)

/** Nothing is shown while the answer on hand belongs to another guild. */
const targets = computed(() =>
  targetData.value && targetData.value.guildId === selected.value
    ? orderTargets(targetData.value.targets)
    : [],
)

/**
 * What this deployment can publish, asked once for the whole page.
 *
 * Not watched on `selected` and not keyed by guild: the catalogue is a
 * property of the binary answering, identical for every server, so a guild
 * switch must not refetch it and a stale answer from another guild is not
 * a thing that can happen here.
 *
 * Its failure is **not** treated like the collection list's. A collection
 * name is decoration and the field degrades to an id; the format is a
 * required value with a closed set, so an unreadable catalogue leaves this
 * page with nothing honest to offer for a *new* destination — and it says
 * so, rather than falling back to a list of guesses. Everything that does
 * not need it still works: the existing destinations render with their
 * stored formats, and switching one off, removing it or writing its
 * credential asks the catalogue nothing.
 */
const { data: formatData, error: formatError } = await useAsyncData(
  'export-formats',
  async () => parseFormats(await api(FORMATS_PATH)),
)

const formats = computed(() => formatData.value ?? [])

/**
 * Outline's collections, for the one format that addresses one.
 *
 * Decoration, exactly as on Bot Settings: if this refuses, the collection
 * field falls back to asking for an id and one sentence says why. A page
 * that could not be used because a name list is down would be worse than
 * one that asks for ids.
 */
const { data: collectionData, error: collectionError } = await useAsyncData(
  'destinations-collections',
  async () => parseCollections(await api('/outline/collections')),
)

const collections = computed(() => collectionData.value?.collections ?? [])

const primary = computed(() => primaryTarget(targets.value))

const page = ref(1)

const view = computed(() => paginationView(page.value, targets.value.length, PAGE_SIZE))
const shown = computed(() =>
  targets.value.slice((view.value.page - 1) * PAGE_SIZE, view.value.page * PAGE_SIZE),
)

/**
 * The list's rows.
 *
 * No `selected` is handed to `UiDisclosureList`, so no checkbox column is
 * rendered: this list has no bulk action. Removing several destinations at
 * once is not something anybody has asked for, and a column of ticks over
 * an action that does not exist is a control that reads as broken.
 *
 * The destination travels on the row rather than being looked back up by
 * index, which is what the list being generic in its row type is for — a
 * second array indexed in parallel is how one name ends up beside another
 * destination's controls the first time the two stop being the same
 * length.
 */
const rows = computed(() => shown.value.map((target) => ({ id: String(target.id), target })))

/* -------------------------------------------------------------------- */
/* Which panel is open, and what happened last                           */
/* -------------------------------------------------------------------- */

const adding = ref(false)
/** The destination whose form is open, by id. */
const editing = ref<number | null>(null)
/** The destination whose removal is being confirmed, by id. */
const removing = ref<number | null>(null)
/** The destination a request is running against, or `'new'`. One at a
 *  time: every write here re-reads the whole list afterwards, and two in
 *  flight would race over which answer wins. */
const busy = ref<number | 'new' | null>(null)
/** What the last write did, or why it did not. */
const outcome = ref<{ tone: 'good' | 'bad'; message: Message } | null>(null)

function closePanels() {
  adding.value = false
  editing.value = null
  removing.value = null
}

// A page number belongs to a list, and switching servers is a different
// list: page four of the previous guild's destinations is not page four of
// this one's. An open form belongs to a destination, and that destination
// is not on screen any more either.
watch(selected, () => {
  page.value = 1
  outcome.value = null
  closePanels()
})

/**
 * Runs one write, then re-reads the list.
 *
 * Re-read rather than patched in place: the API answers a write with the
 * stored row, and the *rest* of the list can have changed under a second
 * administrator in another tab. It is one request against a guild's own
 * destinations, of which there are a handful.
 */
async function write(
  who: number | 'new',
  request: () => Promise<unknown>,
  done: string,
): Promise<void> {
  if (busy.value !== null) return
  busy.value = who
  outcome.value = null
  try {
    await request()
    await refresh()
    closePanels()
    outcome.value = { tone: 'good', message: { key: done } }
  } catch (error) {
    outcome.value = { tone: 'bad', message: describeTargetError(error) }
  } finally {
    busy.value = null
  }
}

const guildId = computed(() => selected.value ?? '')

function create(draft: TargetDraft) {
  void write(
    'new',
    () => api(targetsPath(guildId.value), { method: 'POST', body: draftBody(draft) }),
    'admin.destinations.savedCreated',
  )
}

function update(target: ExportTarget, draft: TargetDraft) {
  void write(
    target.id,
    () =>
      api(targetPath(guildId.value, target.id), {
        method: 'PUT',
        // The stored `config` travels back out untouched: this page never
        // edits it, and sending `{}` would erase whatever a format that
        // needs one had put there.
        body: draftBody(draft, target.config),
      }),
    'admin.destinations.savedUpdated',
  )
}

/** Switching one on or off is the same `PUT` with one field flipped, so it
 *  goes through the same path rather than growing an endpoint of its own. */
function setEnabled(target: ExportTarget, enabled: boolean) {
  void write(
    target.id,
    () =>
      api(targetPath(guildId.value, target.id), {
        method: 'PUT',
        body: draftBody({ ...draftOf(target), enabled }, target.config),
      }),
    enabled ? 'admin.destinations.savedEnabled' : 'admin.destinations.savedDisabled',
  )
}

function remove(target: ExportTarget) {
  void write(
    target.id,
    () => api(targetPath(guildId.value, target.id), { method: 'DELETE' }),
    'admin.destinations.savedDeleted',
  )
}

function storeSecret(target: ExportTarget, secret: string) {
  void write(
    target.id,
    () =>
      api(targetSecretPath(guildId.value, target.id), { method: 'PUT', body: { secret } }),
    'admin.destinations.secretSaved',
  )
}

/** `null` is how the API is told to forget a credential, and it is a
 *  different request from writing one. See `ExportTargetSecret`. */
function clearSecret(target: ExportTarget) {
  void write(
    target.id,
    () =>
      api(targetSecretPath(guildId.value, target.id), {
        method: 'PUT',
        body: { secret: null },
      }),
    'admin.destinations.secretCleared',
  )
}

function startEditing(id: number) {
  outcome.value = null
  adding.value = false
  removing.value = null
  editing.value = editing.value === id ? null : id
}

function startAdding() {
  outcome.value = null
  editing.value = null
  removing.value = null
  adding.value = true
}
</script>

<template>
  <div class="mx-auto flex max-w-4xl flex-col gap-4">
    <header>
      <h1 class="text-2xl font-semibold">{{ $t('admin.destinations.title') }}</h1>
      <p class="mt-1 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.destinations.intro') }}
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
      <p class="text-sm font-medium">{{ $t('admin.destinations.serversFailed') }}</p>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ say(describeTargetError(guildError)) }}
      </p>
    </section>

    <!-- Nobody administers anything. The endpoints answer 404 to this
         person for every guild, so there is nothing to show and one
         paragraph saying how that changes. -->
    <section
      v-else-if="guilds.length === 0"
      class="rounded-2xl border p-6"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="text-base font-semibold">{{ $t('admin.destinations.noGuildsHeading') }}</h2>
      <p class="mt-2 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.destinations.noGuildsBody') }}
      </p>
      <i18n-t
        keypath="admin.destinations.noGuildsRole"
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
           it scopes and outside the list it changes. -->
      <section
        class="rounded-2xl border p-4"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <span class="block text-sm font-medium">{{ $t('admin.destinations.whichServer') }}</span>
        <div class="mt-1 max-w-md">
          <UiSelect
            :options="guildChoices"
            :model-value="selected"
            :label="$t('admin.destinations.whichServer')"
            @update:model-value="chooseGuildFromMenu"
          />
        </div>
      </section>

      <!-- What the last write did, good or bad, said once and near the
           list it changed. -->
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

      <div
        v-if="targetStatus === 'pending' && !targetError"
        aria-busy="true"
      >
        <p class="sr-only">{{ $t('admin.destinations.loading') }}</p>
        <div
          class="h-40 animate-pulse rounded-2xl border motion-reduce:animate-none"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        />
      </div>

      <section
        v-else-if="targetError"
        class="rounded-2xl border p-6"
        :style="{ borderColor: 'var(--danger)' }"
      >
        <p class="text-sm font-medium">{{ $t('admin.destinations.loadFailed') }}</p>
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ say(describeTargetError(targetError)) }}
        </p>
        <button
          type="button"
          class="mt-3 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-60"
          :style="{ color: 'var(--action)' }"
          :disabled="targetStatus === 'pending'"
          @click="refresh()"
        >
          {{ $t('error.retry') }}
        </button>
      </section>

      <template v-else>
        <section
          class="rounded-2xl border p-4"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <!-- Which one Discord announces, said above the list rather than
               left to be inferred from the order. It is the one fact on
               this page with a consequence outside it. -->
          <p
            v-if="primary"
            class="mb-3 text-xs"
            :style="{ color: 'var(--text-muted)' }"
          >
            {{ $t('admin.destinations.primaryNote') }}
          </p>

          <UiDisclosureList :rows="rows" :label="$t('admin.destinations.listLabel')">
            <template #row="{ row }">
              <span class="flex flex-wrap items-center gap-2">
                <span class="font-medium">{{ row.target.name }}</span>
                <span
                  class="rounded-full px-2 py-0.5 text-xs"
                  :style="row.target.enabled
                    ? { background: 'var(--positive)', color: 'var(--positive-contrast)' }
                    : { background: 'var(--surface-sunken)', color: 'var(--text-muted)' }"
                >{{ $t(enabledLabelKey(row.target)) }}</span>
                <span
                  v-if="primary && primary.id === row.target.id"
                  class="rounded-full px-2 py-0.5 text-xs"
                  :style="{ background: 'var(--surface-sunken)', color: 'var(--text)' }"
                >{{ $t('admin.destinations.primaryBadge') }}</span>
                <span
                  v-if="row.target.hasSecret"
                  class="rounded-full px-2 py-0.5 text-xs"
                  :style="{ background: 'var(--surface-sunken)', color: 'var(--text-muted)' }"
                >{{ $t('admin.destinations.secretHeading') }}</span>
                <span class="block w-full truncate text-xs" :style="{ color: 'var(--text-muted)' }">
                  {{ say(targetSummary(row.target, formats)) }}
                </span>
              </span>
            </template>

            <template #actions="{ row }">
              <div class="flex flex-col gap-3">
                <div class="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
                    :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
                    :disabled="busy !== null"
                    @click="startEditing(row.target.id)"
                  >
                    {{ $t('admin.destinations.edit') }}
                  </button>
                  <button
                    type="button"
                    class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
                    :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
                    :disabled="busy !== null"
                    @click="setEnabled(row.target, !row.target.enabled)"
                  >
                    {{ row.target.enabled
                      ? $t('admin.destinations.disable')
                      : $t('admin.destinations.enable') }}
                  </button>
                  <button
                    type="button"
                    class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
                    :style="{ borderColor: 'var(--danger)', color: 'var(--danger)' }"
                    :disabled="busy !== null"
                    @click="removing = removing === row.target.id ? null : row.target.id"
                  >
                    {{ $t('admin.destinations.delete') }}
                  </button>
                </div>

                <!-- Removing, confirmed, and told what it does not do.
                     Nothing already published is deleted, and somebody
                     about to press this should not have to guess. -->
                <div
                  v-if="removing === row.target.id"
                  class="rounded-lg border p-3"
                  :style="{ borderColor: 'var(--danger)' }"
                >
                  <p class="text-sm font-semibold">
                    {{ $t('admin.destinations.deleteConfirmHeading') }}
                  </p>
                  <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                    {{ $t('admin.destinations.deleteConfirmBody') }}
                  </p>
                  <div class="mt-2 flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-60"
                      :style="{ background: 'var(--danger)', color: 'var(--danger-contrast)' }"
                      :disabled="busy !== null"
                      @click="remove(row.target)"
                    >
                      {{ $t('admin.destinations.deleteConfirm') }}
                    </button>
                    <button
                      type="button"
                      class="rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-60"
                      :style="{ borderColor: 'var(--control-border)', color: 'var(--text)' }"
                      :disabled="busy !== null"
                      @click="removing = null"
                    >
                      {{ $t('admin.destinations.cancel') }}
                    </button>
                  </div>
                </div>

                <ExportTargetForm
                  v-if="editing === row.target.id"
                  mode="edit"
                  :initial="draftOf(row.target)"
                  :taken="takenNames(targets, row.target.id)"
                  :formats="formats"
                  :formats-failed="Boolean(formatError)"
                  :collections="collections"
                  :collections-failed="Boolean(collectionError)"
                  :busy="busy === row.target.id"
                  @submit="(draft) => update(row.target, draft)"
                  @cancel="editing = null"
                />

                <!-- Written, never read. Its own control and its own
                     request, so that saving the form above cannot touch
                     it. -->
                <ExportTargetSecret
                  :target="row.target"
                  :busy="busy === row.target.id"
                  @store="(secret) => storeSecret(row.target, secret)"
                  @clear="clearSecret(row.target)"
                />
              </div>
            </template>
          </UiDisclosureList>

          <!-- The empty list already says "there is nothing in this list";
               this says why that is not a fault and what happens instead. -->
          <div
            v-if="targets.length === 0"
            class="mt-3 rounded-lg border border-dashed p-3"
            :style="{ borderColor: 'var(--border)' }"
          >
            <p class="text-sm font-medium">{{ $t('admin.destinations.emptyHeading') }}</p>
            <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ $t('admin.destinations.emptyNote') }}
            </p>
          </div>

          <UiPagination
            v-if="view.count > 1"
            class="mt-4"
            :page="view.page"
            :total="targets.length"
            :label="$t('admin.destinations.pagerLabel')"
            @update:page="(next) => (page = next)"
          />

          <div class="mt-4">
            <ExportTargetForm
              v-if="adding"
              mode="add"
              :initial="emptyDraft(formats)"
              :taken="takenNames(targets)"
              :formats="formats"
              :formats-failed="Boolean(formatError)"
              :collections="collections"
              :collections-failed="Boolean(collectionError)"
              :busy="busy === 'new'"
              @submit="create"
              @cancel="adding = false"
            />
            <button
              v-else
              type="button"
              class="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-60"
              :style="{ background: 'var(--action)', color: 'var(--action-contrast)' }"
              :disabled="busy !== null"
              @click="startAdding"
            >
              {{ $t('admin.destinations.add') }}
            </button>
          </div>
        </section>

        <!-- The other half of the answer, on the page where the question
             is asked. `document_target` and these destinations are not
             rival settings: one replaces the other, and which of them is
             in force right now is what this says. -->
        <section
          class="rounded-2xl border p-4"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <h2 class="text-base font-semibold">{{ $t('admin.destinations.fallbackHeading') }}</h2>
          <p class="mt-1 max-w-3xl text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ say(fallbackNote(targets)) }}
          </p>
          <NuxtLink
            to="/admin/bot-settings"
            class="mt-2 inline-block text-sm font-medium transition-colors hover:underline"
            :style="{ color: 'var(--action)' }"
          >
            {{ $t('admin.destinations.fallbackLink') }}
          </NuxtLink>
        </section>
      </template>
    </template>
  </div>
</template>
