<script setup lang="ts">
/**
 * How one Discord server uses Sturnus: how much was recorded, how much of
 * it was written up, and how that changed month by month.
 *
 * Everything that is a *decision* -- what an absent average reads like,
 * which month comes first, whether a gap in the bar row is drawn, what the
 * speaking time is actually a total of, whether an empty report is a fault
 * -- lives in `~/utils/reporting` and is tested there. What is left in
 * this file is layout and request plumbing. The guild picker is not
 * reimplemented either: it is the same functions the Bot Settings, User
 * Settings and Queue pages use, down to the remembered choice, so
 * switching servers on one admin page carries to the others.
 *
 * The figures are laid out in the dashboard's vocabulary -- a big number,
 * a label, a line underneath -- rather than in a second one invented here.
 * Somebody who has read `/` already knows how to read this, and two
 * house styles for "a statistic on a card" is how a console starts looking
 * like two products.
 *
 * Three things this page refuses to do, all three on purpose:
 *
 * - **None of the figures above the ranking is about a person.** The report
 *   payload carries no ids and no names, and that is a decision rather than
 *   an omission. The attendance ranking at the foot of the page is the one
 *   thing here that names people, and everything about how it is built
 *   keeps it apart from the rest: its own endpoint, its own module
 *   (`~/utils/participation`), its own standing notes, and a request that
 *   goes out only when somebody presses the control that says what it will
 *   do. Reading it is written to the audit log, which is exactly why it is
 *   not loaded alongside the figures — somebody checking whether
 *   transcription is keeping up has not asked to look at a ranking of their
 *   colleagues, and their name should not end up in a log saying they did.
 * - **It draws its own bars.** A chart library for one row of rectangles
 *   would be a dependency for four glyphs, and the Content-Security-Policy
 *   this console is served under would not load it anyway. The bars are
 *   `div`s with a width, and every row carries its own sentence so the
 *   page reads the same to somebody who is listening to it.
 * - **It does not poll.** Nothing here changes minute to minute; a report
 *   left open overnight re-reading a whole server's history every few
 *   seconds would be a cost with no reader. The Refresh button is the
 *   whole of the refresh policy.
 */
import {
  PARTICIPATION_EMPTY_HEADING,
  PARTICIPATION_EMPTY_NOTE,
  PARTICIPATION_HEADING,
  PARTICIPATION_HIDE_LABEL,
  PARTICIPATION_HIDE_NOTE,
  PARTICIPATION_LOADING_NOTE,
  PARTICIPATION_REVEAL_BUSY_LABEL,
  PARTICIPATION_REVEAL_LABEL,
  PARTICIPATION_REVEAL_NOTE,
  type GuildParticipation,
  describeParticipationError,
  isParticipationEmpty,
  parseGuildParticipation,
  participationNotes,
  participationPath,
  participationRows,
  participationScopeLine,
} from '~/utils/participation'
import {
  REPORT_EMPTY_HEADING,
  REPORT_EMPTY_NOTE,
  REPORT_SCOPE_NOTE,
  type GuildReport,
  describeReportError,
  isReportEmpty,
  parseGuildReport,
  reportCaveats,
  reportHeadlineFigures,
  reportMonthRows,
  reportMonthsNote,
  reportPath,
  reportShapeFigures,
  reportSpanLine,
} from '~/utils/reporting'
import {
  chooseGuild,
  guildLabel,
  parseGuilds,
  readSelectedGuild,
  writeSelectedGuild,
} from '~/utils/settings'

useHead({ title: 'Reporting' })

const api = useApi()

const { data: guildData, error: guildError } = await useAsyncData('report-guilds', async () =>
  parseGuilds(await api('/guilds')),
)

const guilds = computed(() => guildData.value ?? [])

// Server-side there is no browser and therefore no remembered choice, so
// the first render picks the first guild. The remembered one is applied
// after hydration -- the same trade the sidebar and the other three admin
// pages make: a correct first paint for everybody, and one repaint for the
// person who has two guilds and last worked on the second.
const selected = ref<string | null>(chooseGuild(guilds.value, null))

onMounted(() => {
  selected.value = chooseGuild(guilds.value, readSelectedGuild(window.localStorage))
})

function selectGuild(guildId: string) {
  selected.value = guildId
  if (import.meta.client) writeSelectedGuild(window.localStorage, guildId)
}

// The guild the figures belong to travels *with* the figures rather than
// in a ref of its own. A ref set inside the fetcher would be null after
// hydration -- the server ran the fetch, the client did not -- and the
// figures would vanish on every first paint.
const {
  data: reportData,
  error: reportError,
  status: reportStatus,
  refresh,
} = await useAsyncData(
  'guild-report',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, report: null as GuildReport | null }
    return { guildId, report: parseGuildReport(await api(reportPath(guildId))) }
  },
  { watch: [selected] },
)

/** Nothing is shown while the answer on hand belongs to another guild.
 *  Reading one server's usage under another server's heading is the exact
 *  mistake the switcher exists to prevent, and a figure that lingers for a
 *  few hundred milliseconds is long enough to be written into a report
 *  somebody else will read. */
const report = computed(() =>
  reportData.value && reportData.value.guildId === selected.value ? reportData.value.report : null,
)

const currentGuild = computed(
  () => guilds.value.find((guild) => guild.id === selected.value) ?? null,
)

const headline = computed(() => (report.value ? reportHeadlineFigures(report.value) : []))
const shape = computed(() => (report.value ? reportShapeFigures(report.value) : []))
const caveats = computed(() => (report.value ? reportCaveats(report.value) : []))
const months = computed(() => (report.value ? reportMonthRows(report.value) : []))
const monthsNote = computed(() => (report.value ? reportMonthsNote(report.value) : ''))
const span = computed(() => (report.value ? reportSpanLine(report.value) : ''))
const empty = computed(() => Boolean(report.value && isReportEmpty(report.value)))

/** Whether a re-read is in flight. `useAsyncData`'s own status settles
 *  back to `success` while a background refresh runs, which is right for
 *  the page -- the figures on screen stay readable rather than being
 *  replaced by a skeleton -- and leaves the button with nothing to
 *  report. This ref is that report, and nothing else. */
const refreshing = ref(false)

async function refreshNow() {
  refreshing.value = true
  try {
    await refresh()
  }
  finally {
    refreshing.value = false
  }
}

/* -------------------------------------------------------------------- */
/* The attendance ranking, which is not fetched with anything else       */
/* -------------------------------------------------------------------- */

/**
 * Whether the reader has asked for the ranking.
 *
 * The whole reason this is a separate request rather than another field on
 * the report: reading the ranking is written to the audit log, and somebody
 * who opened this page to check whether transcription is keeping up has not
 * asked to look at a ranking of the people they work with. Loading it
 * alongside the figures would have put every one of them in that log,
 * without ever having been asked.
 *
 * `immediate: false` and no `watch`, deliberately. A watched fetcher would
 * re-issue the request -- and write another audit line -- every time
 * somebody flipped the guild picker, which is the opposite of asking.
 */
const revealed = ref(false)

/** Whether the request is out. Its own ref rather than `useAsyncData`'s
 *  status, for the same reason `refreshing` above is one: the status is
 *  about the data, and this is about the button the reader is looking at
 *  while they wait for an answer they deliberately asked for. */
const asking = ref(false)

const {
  data: rankingData,
  error: rankingError,
  execute: loadRanking,
  clear: clearRanking,
} = await useAsyncData(
  'guild-participation',
  async () => {
    const guildId = selected.value
    if (!guildId) {
      return { guildId: null as string | null, participation: null as GuildParticipation | null }
    }
    return { guildId, participation: parseGuildParticipation(await api(participationPath(guildId))) }
  },
  { immediate: false, default: () => null },
)

// The list appears only once the answer is in hand. The control stays put
// and says it is reading, rather than being replaced by a spinner where it
// stood: the reader pressed a button that named what it would do, and the
// thing that reports back should be that button.
async function revealRanking() {
  asking.value = true
  try {
    await loadRanking()
  }
  finally {
    asking.value = false
    revealed.value = true
  }
}

function hideRanking() {
  revealed.value = false
  clearRanking()
}

// Switching servers puts the ranking away rather than carrying it across.
// A list of named people left standing under a different server's heading
// is the worst version of the mistake the guild picker exists to prevent --
// and re-fetching it for the new server on the reader's behalf would be
// this page deciding to look at that server's people for them.
watch(selected, () => {
  revealed.value = false
  clearRanking()
})

/** Nothing is shown while the answer on hand belongs to another guild, for
 *  the same reason the report itself is guarded that way — with the
 *  difference that a stale row here carries somebody's name. */
const ranking = computed(() =>
  rankingData.value && rankingData.value.guildId === selected.value
    ? rankingData.value.participation
    : null,
)

const rankingNotes = participationNotes()
const rankingRows = computed(() => (ranking.value ? participationRows(ranking.value) : []))
const rankingScope = computed(() => (ranking.value ? participationScopeLine(ranking.value) : ''))
const rankingEmpty = computed(() => Boolean(ranking.value && isParticipationEmpty(ranking.value)))

/** An absence is not a small number, and must not be coloured like one. A
 *  muted em dash reads as "there is nothing here"; the same dash in the
 *  figure colour reads as a value the reader failed to parse. */
const TONE_COLOUR: Record<string, string> = {
  plain: 'var(--text)',
  absent: 'var(--text-muted)',
  watch: 'var(--color-brand-cyan)',
}
</script>

<template>
  <div class="mx-auto max-w-5xl">
    <h1 class="mb-1 text-2xl font-semibold">Reporting</h1>
    <p class="mb-6 text-sm" :style="{ color: 'var(--text-muted)' }">
      How one Discord server uses Sturnus: how many meetings it has recorded, how long they ran, how
      many of them were written up, and how that has changed month by month.
    </p>

    <p
      v-if="guildError"
      class="rounded-xl border p-4 text-sm"
      :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
    >
      {{ describeReportError(guildError) }}
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
        This section reports how one Discord server uses Sturnus, and it is open to the
        administrators of a server where Sturnus is running. You administer none of them right now.
      </p>
      <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
        Administrators are the members holding the Discord role that server names in its
        <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">admin_role_id</code>
        setting. Somebody who already has it can grant you that role — Sturnus mirrors the
        membership from Discord, so the change reaches this console on its own.
      </p>
    </section>

    <template v-else>
      <section
        class="mb-6 rounded-xl border p-4"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      >
        <!-- With more than one guild the switcher is the only thing
             standing between an administrator and reporting the wrong
             server's figures, so the current one is named here and its id
             repeated underneath. -->
        <label
          v-if="guilds.length > 1"
          class="mb-2 block text-xs font-medium tracking-wide uppercase"
          :style="{ color: 'var(--text-muted)' }"
          for="guild-switcher"
        >
          Which server
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
          <span :style="{ color: 'var(--text-muted)' }">Showing</span>
          <span class="ml-1 font-medium">{{ currentGuild ? guildLabel(currentGuild) : '—' }}</span>
        </p>
        <p v-if="currentGuild" class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
          Guild ID
          <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">{{ currentGuild.id }}</code>
        </p>
      </section>

      <p
        v-if="reportError"
        class="mb-6 rounded-xl border p-4 text-sm"
        :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
      >
        {{ describeReportError(reportError) }}
      </p>

      <template v-else>
        <p v-if="reportStatus === 'pending'" class="text-sm" :style="{ color: 'var(--text-muted)' }">
          Reading this server's figures…
        </p>

        <template v-else-if="report">
          <!-- What this page is about, said before the figures rather than
               after them. A reader who arrives looking for who spoke most
               should find the answer to that question here, not discover
               by exhaustion that the page does not have it. -->
          <p
            class="mb-6 rounded-xl border p-4 text-sm"
            :style="{
              borderColor: 'var(--border)',
              background: 'var(--surface-raised)',
              color: 'var(--text-muted)',
            }"
          >
            {{ REPORT_SCOPE_NOTE }}
          </p>

          <!-- A server that has recorded nothing gets a sentence. A grid
               of dashes and zeros would read as a page that failed to
               load, and is also a claim: that this server holds meetings
               of no length attended by nobody. -->
          <section
            v-if="empty"
            class="rounded-2xl border p-8 text-center"
            :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
          >
            <div class="mb-4 flex justify-center">
              <SturnusMark :size="48" />
            </div>
            <h2 class="mb-2 text-lg font-medium">{{ REPORT_EMPTY_HEADING }}</h2>
            <p class="mx-auto max-w-lg text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ REPORT_EMPTY_NOTE }}
            </p>
          </section>

          <template v-else>
            <div class="mb-4 flex flex-wrap items-baseline justify-between gap-3">
              <p class="text-xs" :style="{ color: 'var(--text-muted)' }">{{ span }}</p>
              <!-- The whole of the refresh policy. Nothing on this page
                   changes minute to minute, so it does not poll: a report
                   left open overnight must not re-read a server's whole
                   history every few seconds for a reader who is not
                   there. -->
              <button
                type="button"
                class="shrink-0 rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
                :style="{ borderColor: 'var(--border)' }"
                :disabled="refreshing"
                @click="refreshNow()"
              >
                {{ refreshing ? 'Reading…' : 'Refresh' }}
              </button>
            </div>

            <!-- The headline band: how much this server has used Sturnus
                 at all. The dashboard's card vocabulary, deliberately --
                 a big number, a label, the line that keeps it honest. -->
            <dl class="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
              <div
                v-for="figure in headline"
                :key="figure.key"
                class="rounded-2xl border p-5"
                :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
              >
                <dt class="text-sm font-medium" :style="{ color: 'var(--text-muted)' }">
                  {{ figure.label }}
                </dt>
                <dd
                  class="mt-2 text-3xl font-semibold tracking-tight tabular-nums"
                  :style="{ color: TONE_COLOUR[figure.tone] }"
                >
                  {{ figure.value }}
                </dd>
                <dd v-if="figure.note" class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
                  {{ figure.note }}
                </dd>
              </div>
            </dl>

            <!-- Kept visually apart from the band above, because these are
                 a different kind of number: those describe how much has
                 happened, these describe the shape of one meeting — and
                 four of the six are allowed to be missing entirely. -->
            <section
              class="mb-8 rounded-xl border p-4"
              :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
            >
              <h2 class="mb-1 text-sm font-semibold">What a meeting here looks like</h2>
              <p class="mb-4 text-xs" :style="{ color: 'var(--text-muted)' }">
                Averages over the meetings that have finished. A figure shown as an em dash is
                missing rather than zero, and says underneath why there is none.
              </p>
              <dl class="grid grid-cols-2 gap-4 sm:grid-cols-3">
                <div v-for="figure in shape" :key="figure.key">
                  <dt
                    class="text-xs font-medium tracking-wide uppercase"
                    :style="{ color: 'var(--text-muted)' }"
                  >
                    {{ figure.label }}
                  </dt>
                  <dd
                    class="text-2xl font-semibold tabular-nums"
                    :style="{ color: TONE_COLOUR[figure.tone] }"
                  >
                    {{ figure.value }}
                  </dd>
                  <dd v-if="figure.note" class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                    {{ figure.note }}
                  </dd>
                </div>
              </dl>
            </section>

            <!-- The by-month breakdown. A table, so it is navigable by
                 anything that navigates tables, with the bar drawn inside
                 the row rather than instead of it: the bar is a shape to
                 glance at, and the numbers beside it are what anybody
                 would actually quote. -->
            <section v-if="months.length" class="mb-8">
              <h2 class="mb-1 text-sm font-semibold">Month by month</h2>
              <p class="mb-3 text-xs" :style="{ color: 'var(--text-muted)' }">{{ monthsNote }}</p>

              <!-- Wide on a phone; the wrapper scrolls rather than the
                   page, so the rest of the page keeps its width. -->
              <div class="overflow-x-auto">
                <table class="w-full min-w-[34rem] border-collapse text-sm">
                  <caption class="sr-only">
                    Meetings recorded in this server by month, oldest first
                  </caption>
                  <thead>
                    <tr :style="{ color: 'var(--text-muted)' }">
                      <th scope="col" class="py-2 pr-4 text-left text-xs font-medium tracking-wide uppercase">
                        Month
                      </th>
                      <th scope="col" class="py-2 pr-4 text-left text-xs font-medium tracking-wide uppercase">
                        Meetings
                      </th>
                      <th scope="col" class="py-2 pr-4 text-right text-xs font-medium tracking-wide uppercase">
                        Recorded
                      </th>
                      <th scope="col" class="py-2 text-right text-xs font-medium tracking-wide uppercase">
                        Written up
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="row in months"
                      :key="row.month"
                      class="border-t"
                      :style="{ borderColor: 'var(--border)' }"
                    >
                      <th
                        scope="row"
                        class="py-2 pr-4 text-left font-medium whitespace-nowrap"
                        :style="{ color: row.silent ? 'var(--text-muted)' : 'var(--text)' }"
                      >
                        {{ row.label }}
                      </th>
                      <td class="py-2 pr-4">
                        <!-- The row said in full for a reader who is not
                             looking at the bar, and hidden from the eye
                             that is: the bar and the three columns beside
                             it already say the same thing. -->
                        <span class="sr-only">{{ row.detail }}</span>
                        <span aria-hidden="true" class="flex items-center gap-2">
                          <span
                            class="h-2 flex-1 overflow-hidden rounded-full"
                            :style="{ background: 'var(--surface-sunken)' }"
                          >
                            <span
                              class="block h-full rounded-full"
                              :style="{
                                width: `${row.extent * 100}%`,
                                background: row.silent
                                  ? 'transparent'
                                  : 'linear-gradient(90deg, var(--color-brand-blue), var(--color-brand-cyan))',
                              }"
                            />
                          </span>
                          <span
                            class="w-10 shrink-0 text-right tabular-nums"
                            :style="{ color: row.silent ? 'var(--text-muted)' : 'var(--text)' }"
                          >
                            {{ row.sessions }}
                          </span>
                        </span>
                      </td>
                      <td
                        class="py-2 pr-4 text-right tabular-nums"
                        :style="{ color: 'var(--text-muted)' }"
                      >
                        {{ row.recorded }}
                      </td>
                      <td class="py-2 text-right tabular-nums" :style="{ color: 'var(--text-muted)' }">
                        {{ row.documented }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </section>

            <!-- The two things a reader has to know before the figures
                 above mean what they appear to mean. In a panel of their
                 own rather than as footnotes: a footnote is read once, by
                 the person who was already being careful. -->
            <section
              class="rounded-xl border p-4"
              :style="{ borderColor: 'var(--color-brand-yellow)', background: 'var(--surface)' }"
            >
              <h2 class="mb-3 text-sm font-semibold">Before you quote any of this</h2>
              <dl class="flex flex-col gap-4">
                <div v-for="caveat in caveats" :key="caveat.key">
                  <dt class="text-sm font-medium">{{ caveat.label }}</dt>
                  <dd class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                    {{ caveat.text }}
                  </dd>
                </div>
              </dl>
            </section>

            <!-- The one section on this page that names people and puts
                 them in an order. Everything about how it is laid out is
                 the same argument the module makes in words: the standing
                 notes are above the control rather than above the list, so
                 the reader has them before they decide; the rows are drawn
                 identically, with no first-place styling, no bars and no
                 medals; and speaking time is a sentence underneath the
                 attendance rather than a column beside it, because a column
                 of durations is ranked by the eye whether or not anybody
                 sorted it. There is no sort control, and there is not going
                 to be one. -->
            <section class="mt-8">
              <h2 class="mb-1 text-sm font-semibold">{{ PARTICIPATION_HEADING }}</h2>

              <!-- Above the reveal, not above the list. A note that appears
                   only once the ranking is on screen arrives after the
                   decision it exists to inform. -->
              <dl
                class="mb-4 flex flex-col gap-4 rounded-xl border p-4"
                :style="{ borderColor: 'var(--color-brand-yellow)', background: 'var(--surface)' }"
              >
                <div v-for="note in rankingNotes" :key="note.key">
                  <dt class="text-sm font-medium">{{ note.label }}</dt>
                  <dd class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                    {{ note.text }}
                  </dd>
                </div>
              </dl>

              <!-- Nothing has been fetched at this point, and the control
                   says what pressing it will do rather than "show more". -->
              <div
                v-if="!revealed"
                class="rounded-xl border p-4"
                :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
              >
                <p class="mb-3 text-xs" :style="{ color: 'var(--text-muted)' }">
                  {{ asking ? PARTICIPATION_LOADING_NOTE : PARTICIPATION_REVEAL_NOTE }}
                </p>
                <button
                  type="button"
                  class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
                  :style="{ borderColor: 'var(--border)' }"
                  :disabled="asking"
                  @click="revealRanking()"
                >
                  {{ asking ? PARTICIPATION_REVEAL_BUSY_LABEL : PARTICIPATION_REVEAL_LABEL }}
                </button>
              </div>

              <template v-else>
                <p
                  v-if="rankingError"
                  class="rounded-xl border p-4 text-sm"
                  :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
                >
                  {{ describeParticipationError(rankingError) }}
                </p>

                <!-- No answer and no error: the guild changed under the
                     request, or the fetcher never ran. Nothing is shown,
                     because the only thing that could be shown here is
                     another server's people. -->
                <p v-else-if="!ranking" class="text-sm" :style="{ color: 'var(--text-muted)' }">
                  {{ PARTICIPATION_LOADING_NOTE }}
                </p>

                <!-- Nobody to list is a sentence, not an empty table. An
                     empty table reads as a section that failed to load, and
                     the obvious response to that is to press the button
                     again — which here costs another audit line. -->
                <section
                  v-else-if="rankingEmpty"
                  class="rounded-2xl border p-8 text-center"
                  :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
                >
                  <h3 class="mb-2 text-base font-medium">{{ PARTICIPATION_EMPTY_HEADING }}</h3>
                  <p class="mx-auto max-w-lg text-sm" :style="{ color: 'var(--text-muted)' }">
                    {{ PARTICIPATION_EMPTY_NOTE }}
                  </p>
                </section>

                <template v-else>
                  <p class="mb-3 text-xs" :style="{ color: 'var(--text-muted)' }">
                    {{ rankingScope }}
                  </p>

                  <!-- An ordered list, because that is what this is. Every
                       row carries the same border, the same weight and the
                       same colours: the difference between the first row
                       and the last is the number in it, and the page adds
                       nothing to that. -->
                  <ol class="flex flex-col gap-3">
                    <li
                      v-for="row in rankingRows"
                      :key="row.key"
                      class="rounded-xl border p-4"
                      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
                    >
                      <!-- The row said in full for a reader who is
                           listening to the page, and hidden from the eye
                           that is reading the lines below it. -->
                      <span class="sr-only">{{ row.detail }}</span>

                      <div aria-hidden="true">
                        <div class="flex items-baseline gap-3">
                          <span
                            class="w-8 shrink-0 text-right text-sm tabular-nums"
                            :style="{ color: 'var(--text-muted)' }"
                          >{{ row.rank }}</span>
                          <h3 class="text-sm font-medium break-all">{{ row.name }}</h3>
                          <!-- A shared place is said rather than left to be
                               inferred from two identical numbers. -->
                          <span v-if="row.tied" class="text-xs" :style="{ color: 'var(--text-muted)' }">
                            shared place
                          </span>
                        </div>

                        <div class="mt-1 pl-11">
                          <p class="text-sm">{{ row.attendance }}</p>
                          <!-- Speaking time is a sentence in muted text,
                               under the attendance and never beside it. An
                               unmeasured one is coloured as the absence it
                               is rather than as a small figure. -->
                          <p
                            class="mt-1 text-xs"
                            :style="{
                              color: 'var(--text-muted)',
                              fontStyle: row.speechAbsent ? 'italic' : 'normal',
                            }"
                          >
                            {{ row.speech }}
                          </p>
                          <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                            {{ row.seen }}
                          </p>
                          <p
                            v-if="row.identity"
                            class="mt-1 text-xs"
                            :style="{ color: 'var(--text-muted)' }"
                          >
                            {{ row.identity }}
                          </p>
                        </div>
                      </div>
                    </li>
                  </ol>

                  <!-- Putting it away does not unsay it, and the note
                       beside the button says so. -->
                  <div class="mt-4 flex flex-wrap items-baseline gap-3">
                    <button
                      type="button"
                      class="shrink-0 rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)]"
                      :style="{ borderColor: 'var(--border)' }"
                      @click="hideRanking()"
                    >
                      {{ PARTICIPATION_HIDE_LABEL }}
                    </button>
                    <p class="text-xs" :style="{ color: 'var(--text-muted)' }">
                      {{ PARTICIPATION_HIDE_NOTE }}
                    </p>
                  </div>
                </template>
              </template>
            </section>
          </template>
        </template>
      </template>
    </template>
  </div>
</template>
