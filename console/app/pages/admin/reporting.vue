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
 * - **It never names or ranks a person, and never hints that it might.**
 *   The payload carries no ids and no names, and that is a decision rather
 *   than an omission: a per-person readout of who attended which meetings
 *   and who spoke for how long is a means of monitoring conduct and
 *   performance at work, which is a works-council matter and not a console
 *   feature. `REPORT_SCOPE_NOTE` says so on the page, because the reader
 *   who was about to go looking for the breakdown deserves the answer
 *   rather than a dead end.
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
  REPORT_EMPTY_HEADING_KEY,
  REPORT_EMPTY_NOTE_KEY,
  REPORT_SCOPE_NOTE_KEY,
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
import type { Message } from '~/utils/message'

const { t } = useI18n()
const say = useSay()

useHead({ title: () => t('nav.reporting') })

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
const monthsNote = computed(() => (report.value ? reportMonthsNote(report.value) : []))
const span = computed(() => (report.value ? reportSpanLine(report.value) : null))

/** A run of decided sentences, spoken. They arrive as a list rather than as
 *  one string so that nothing in `~/utils/reporting` has to decide what
 *  order two sentences go in; here they are simply read out in turn. */
function sentences(said: readonly Message[]): string {
  return said.map((one) => say(one)).join(' ')
}
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
    <h1 class="mb-1 text-2xl font-semibold">{{ $t('nav.reporting') }}</h1>
    <p class="mb-6 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('admin.reporting.intro') }}
    </p>

    <p
      v-if="guildError"
      class="rounded-xl border p-4 text-sm"
      :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
    >
      {{ say(describeReportError(guildError)) }}
    </p>

    <!-- Somebody who administers nothing gets the reason and the way in,
         not an empty page. -->
    <section
      v-else-if="guilds.length === 0"
      class="rounded-xl border p-6"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="mb-2 text-base font-semibold">{{ $t('admin.reporting.noGuildsHeading') }}</h2>
      <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.reporting.noGuildsBody') }}
      </p>
      <!-- `i18n-t` rather than the sentence split around the `<code>`:
           where in the sentence the setting's name sits is a property of
           the language, and splitting here would fix it at English. -->
      <i18n-t
        keypath="admin.reporting.noGuildsRole"
        tag="p"
        class="text-sm"
        :style="{ color: 'var(--text-muted)' }"
      >
        <template #setting>
          <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">admin_role_id</code>
        </template>
      </i18n-t>
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
          {{ $t('admin.reporting.whichServer') }}
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
          <span :style="{ color: 'var(--text-muted)' }">{{ $t('admin.reporting.showing') }}</span>
          <span class="ml-1 font-medium">
            {{ currentGuild ? guildLabel(currentGuild) : NOT_MEASURED }}
          </span>
        </p>
        <p v-if="currentGuild" class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('admin.reporting.guildId') }}
          <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">{{ currentGuild.id }}</code>
        </p>
      </section>

      <p
        v-if="reportError"
        class="mb-6 rounded-xl border p-4 text-sm"
        :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
      >
        {{ say(describeReportError(reportError)) }}
      </p>

      <template v-else>
        <p v-if="reportStatus === 'pending'" class="text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ $t('admin.reporting.loadingFigures') }}
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
            {{ $t(REPORT_SCOPE_NOTE_KEY) }}
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
            <h2 class="mb-2 text-lg font-medium">{{ $t(REPORT_EMPTY_HEADING_KEY) }}</h2>
            <p class="mx-auto max-w-lg text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ $t(REPORT_EMPTY_NOTE_KEY) }}
            </p>
          </section>

          <template v-else>
            <div class="mb-4 flex flex-wrap items-baseline justify-between gap-3">
              <p class="text-xs" :style="{ color: 'var(--text-muted)' }">{{ say(span) }}</p>
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
                {{ refreshing ? $t('admin.reporting.reading') : $t('admin.reporting.refresh') }}
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
                  {{ $t(figure.labelKey) }}
                </dt>
                <dd
                  class="mt-2 text-3xl font-semibold tracking-tight tabular-nums"
                  :style="{ color: TONE_COLOUR[figure.tone] }"
                >
                  {{ say(figure.value) }}
                </dd>
                <dd
                  v-if="figure.note.length"
                  class="mt-2 text-xs"
                  :style="{ color: 'var(--text-muted)' }"
                >
                  {{ sentences(figure.note) }}
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
              <h2 class="mb-1 text-sm font-semibold">{{ $t('admin.reporting.shapeHeading') }}</h2>
              <p class="mb-4 text-xs" :style="{ color: 'var(--text-muted)' }">
                {{ $t('admin.reporting.shapeNote') }}
              </p>
              <dl class="grid grid-cols-2 gap-4 sm:grid-cols-3">
                <div v-for="figure in shape" :key="figure.key">
                  <dt
                    class="text-xs font-medium tracking-wide uppercase"
                    :style="{ color: 'var(--text-muted)' }"
                  >
                    {{ $t(figure.labelKey) }}
                  </dt>
                  <dd
                    class="text-2xl font-semibold tabular-nums"
                    :style="{ color: TONE_COLOUR[figure.tone] }"
                  >
                    {{ say(figure.value) }}
                  </dd>
                  <dd
                    v-if="figure.note.length"
                    class="mt-1 text-xs"
                    :style="{ color: 'var(--text-muted)' }"
                  >
                    {{ sentences(figure.note) }}
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
              <h2 class="mb-1 text-sm font-semibold">{{ $t('admin.reporting.monthsHeading') }}</h2>
              <p class="mb-3 text-xs" :style="{ color: 'var(--text-muted)' }">
                {{ sentences(monthsNote) }}
              </p>

              <!-- Wide on a phone; the wrapper scrolls rather than the
                   page, so the rest of the page keeps its width. -->
              <div class="overflow-x-auto">
                <table class="w-full min-w-[34rem] border-collapse text-sm">
                  <caption class="sr-only">
                    {{ $t('admin.reporting.tableCaption') }}
                  </caption>
                  <thead>
                    <tr :style="{ color: 'var(--text-muted)' }">
                      <th scope="col" class="py-2 pr-4 text-left text-xs font-medium tracking-wide uppercase">
                        {{ $t('admin.reporting.columnMonth') }}
                      </th>
                      <th scope="col" class="py-2 pr-4 text-left text-xs font-medium tracking-wide uppercase">
                        {{ $t('admin.reporting.columnMeetings') }}
                      </th>
                      <th scope="col" class="py-2 pr-4 text-right text-xs font-medium tracking-wide uppercase">
                        {{ $t('admin.reporting.columnRecorded') }}
                      </th>
                      <th scope="col" class="py-2 text-right text-xs font-medium tracking-wide uppercase">
                        {{ $t('admin.reporting.columnDocumented') }}
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
                        {{ $d(row.at, 'monthYear') }}
                      </th>
                      <td class="py-2 pr-4">
                        <!-- The row said in full for a reader who is not
                             looking at the bar, and hidden from the eye
                             that is: the bar and the three columns beside
                             it already say the same thing. -->
                        <span class="sr-only">{{ say(row.detail) }}</span>
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
                            {{ $n(row.sessions) }}
                          </span>
                        </span>
                      </td>
                      <td
                        class="py-2 pr-4 text-right tabular-nums"
                        :style="{ color: 'var(--text-muted)' }"
                      >
                        {{ say(row.recorded) }}
                      </td>
                      <td class="py-2 text-right tabular-nums" :style="{ color: 'var(--text-muted)' }">
                        {{ $n(row.documented) }}
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
              <h2 class="mb-3 text-sm font-semibold">{{ $t('admin.reporting.caveatsHeading') }}</h2>
              <dl class="flex flex-col gap-4">
                <div v-for="caveat in caveats" :key="caveat.key">
                  <dt class="text-sm font-medium">{{ $t(caveat.labelKey) }}</dt>
                  <dd class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
                    {{ sentences(caveat.text) }}
                  </dd>
                </div>
              </dl>
            </section>
          </template>
        </template>
      </template>
    </template>
  </div>
</template>
