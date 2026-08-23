<script setup lang="ts">
/**
 * Who has consented to being recorded in one guild, and the one thing an
 * administrator can do about it from here: withdraw a consent.
 *
 * Everything that is a *decision* -- what a state badge says, whether a
 * revoke is offered at all, how a refusal reads as a sentence, how a moment
 * is written, which name goes in front of a snowflake, what a batch would
 * apply to -- lives in `~/utils/consents` and `~/utils/consentRoster` and
 * is tested there. What is left in this file is layout, request plumbing
 * and per-row state. The guild picker is not reimplemented either: it is
 * the same functions the Bot Settings page uses, down to the remembered
 * choice, so switching servers on one admin page carries to the other.
 *
 * Two things this page refuses to do, both on purpose:
 *
 * - **It never lets a withdrawal read as more than it is.** The Discord
 *   role stays, and the recordings stay. Both limits are said in the
 *   confirmation, before the act, and again in the panel afterwards -- not
 *   only in a footnote nobody reads twice. The batch confirmation says them
 *   too, in the plural.
 * - **It never offers an action it knows will fail.** A consent already
 *   withdrawn has no button at all, only the sentence saying when it went,
 *   and its checkbox is disabled so that "select this page" cannot promise
 *   a withdrawal the API answers `already_revoked` to.
 *
 * ## What this page stopped doing
 *
 * It used to render an `<article>` per person, fetch **every** consent
 * record a guild ever had in one request, and sort them in the browser. A
 * guild with four hundred participants sent four hundred records to draw
 * the first ten, and withdrawing consent for four people was four separate
 * confirmations.
 *
 * Now it asks for one page, the page number is in the address so a page is
 * a place somebody can link to, and the order is SQL's. **`orderConsents`
 * is deliberately not called here any more**: a second ordering applied on
 * top of the server's would reshuffle a page whose neighbours the reader
 * cannot see, which is a list that disagrees with its own pager.
 *
 * ## The names
 *
 * A consent record carries a display name only if Sturnus has seen the
 * person in a recorded session. `GET .../directory` holds one for everybody
 * in the guild, and the roster consults it. Somebody neither can name still
 * renders -- as their bare id, with a sentence saying which of the two
 * reasons applies -- because a person quietly missing from a consent roster
 * is a roster that is wrong about who may be recorded.
 *
 * ## The effective instant
 *
 * `UiDatePicker` emits ISO-8601 with a real offset, and it does so by
 * calling `~/utils/effectiveInstant` -- the module that worked out that the
 * offset belongs to the *chosen* moment rather than to today. This page
 * used to hand-roll a `datetime-local` inside a `<details>` and attach the
 * offset itself. It no longer does: three implementations of one piece of
 * daylight-saving arithmetic is two too many, and they would disagree first
 * on the page that matters most.
 *
 * **This file mixes two languages of string, and the mix is deliberate.**
 * Everything already here is English prose, because the whole
 * administrative area is and converting it belongs to the sweep that
 * converts all four admin pages together. Everything *new* goes through
 * `$t` under `admin.consents.*`, the namespace `i18n/README.md` already
 * reserves for this page, so the sweep has less to do rather than more.
 */
import UiDatePicker from '~/components/ui/UiDatePicker.vue'
import UiDisclosureList from '~/components/ui/UiDisclosureList.vue'
import UiPagination from '~/components/ui/UiPagination.vue'
import UiSelect from '~/components/ui/UiSelect.vue'
import {
  type BulkRevokeResult,
  type RosterEntry,
  type RosterPerson,
  batchVerdict,
  bulkConfirmation,
  bulkOutcomeRows,
  bulkRevokeBody,
  bulkTally,
  chosenPeople,
  grantFloor,
  latestGrant,
  nameNote,
  parseBulkRevoke,
  parseConsentPage,
  rememberPeople,
  rosterCount,
  rosterEntries,
  rosterInForce,
  rosterSummary,
} from '~/utils/consentRoster'
import {
  AUDIT_LOG_NOTE,
  ROLE_STAYS_NOTE,
  type ConsentRow,
  type RevokeOutcome,
  consentBadge,
  describeConsentError,
  grantedLine,
  isStaleRow,
  parseRevokeResult,
  policyLine,
  recordingsLine,
  revocability,
  revokeConfirmation,
  revokeOutcome,
  scopeLineKey,
  withdrawnLine,
} from '~/utils/consents'
import { type NamedRow, parseDirectory } from '~/utils/directory'
import { effectiveConsequence, effectiveKind, effectiveOutcome, validateEffectiveAt } from '~/utils/effectiveInstant'
import type { Line } from '~/utils/myConsents'
import { PAGE_SIZE, isPastTheEnd, offsetForPage, pageFromQuery } from '~/utils/paging'
import {
  chooseGuild,
  guildLabel,
  guildOptions,
  parseGuilds,
  readSelectedGuild,
  writeSelectedGuild,
} from '~/utils/settings'

useHead({ title: 'Consents' })

const api = useApi()
const route = useRoute()
const router = useRouter()
const say = useSay()

const { data: guildData, error: guildError } = await useAsyncData('consent-guilds', async () =>
  parseGuilds(await api('/guilds')),
)

const guilds = computed(() => guildData.value ?? [])

// Server-side there is no browser and therefore no remembered choice, so
// the first render picks the first guild. The remembered one is applied
// after hydration -- the same trade the sidebar and the Bot Settings page
// make: a correct first paint for everybody, and one repaint for the person
// who has two guilds and last worked on the second.
const selected = ref<string | null>(chooseGuild(guilds.value, null))

onMounted(() => {
  selected.value = chooseGuild(guilds.value, readSelectedGuild(window.localStorage))
})

function selectGuild(guildId: string) {
  selected.value = guildId
  if (import.meta.client) writeSelectedGuild(window.localStorage, guildId)
}

/** The dropdown's model is nullable so that a page resetting a filter has
 *  a way to say so. This page has no such state: there is always a guild
 *  whose roster is being read, and clearing it would leave the list below
 *  belonging to nothing. */
function chooseGuildFromMenu(guildId: string | null) {
  if (guildId !== null) selectGuild(guildId)
}

const guildChoices = computed(() => guildOptions(guilds.value))
const currentGuild = computed(() => guilds.value.find((guild) => guild.id === selected.value) ?? null)

/* -------------------------------------------------------------------- */
/* One page of the roster                                               */
/* -------------------------------------------------------------------- */

/** The page number lives in the address, so that a page of the roster is a
 *  place: the back button lands where the reader left it, and "the third
 *  page of this server's consents" is something one person can send
 *  another. */
const page = computed(() => pageFromQuery(route.query.page))

function goToPage(next: number) {
  // Page one carries no `?page=1`. The first page is the address of the
  // list itself, and two URLs for one page is two entries in somebody's
  // history that render identically.
  router.push({ path: route.path, query: next > 1 ? { page: String(next) } : {} })
}

// The guild the rows belong to travels *with* the rows rather than in a ref
// of its own. A ref set inside the fetcher would be null after hydration --
// the server ran the fetch, the client did not -- and the list would vanish
// on every first paint.
const {
  data: consentData,
  error: consentError,
  status: consentStatus,
  refresh,
} = await useAsyncData(
  'consent-people',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, page: parseConsentPage(null, PAGE_SIZE) }
    const offset = offsetForPage(page.value, PAGE_SIZE)
    return {
      guildId,
      page: parseConsentPage(
        await api(`/guilds/${guildId}/consents?limit=${PAGE_SIZE}&offset=${offset}`),
        PAGE_SIZE,
      ),
    }
  },
  { watch: [selected, page] },
)

/**
 * The guild's own names for its members.
 *
 * Fetched beside the roster rather than inside it, and failing quietly: a
 * directory that cannot be read is a roster with fewer names on it, never a
 * roster that refuses to render. The people it would have named still
 * appear, as their ids, with the sentence that says the directory was not
 * readable -- which is a different sentence from the one for a member the
 * directory genuinely does not hold.
 */
const { data: directoryData } = await useAsyncData(
  'consent-directory',
  async () => {
    const guildId = selected.value
    if (!guildId) return { guildId: null as string | null, members: null as NamedRow[] | null }
    try {
      return { guildId, members: parseDirectory(await api(`/guilds/${guildId}/directory`)).members }
    } catch {
      return { guildId, members: null as NamedRow[] | null }
    }
  },
  { watch: [selected] },
)

/** Nothing is shown while the answer on hand belongs to another guild.
 *  Withdrawing somebody's consent from a list loaded for a different server
 *  is the exact mistake the switcher exists to prevent, and a list that
 *  lingers for a few hundred milliseconds under the new heading is long
 *  enough to click. */
const held = computed(() =>
  consentData.value && consentData.value.guildId === selected.value ? consentData.value.page : null,
)
const rows = computed<ConsentRow[]>(() => held.value?.rows ?? [])
const total = computed(() => held.value?.total ?? 0)
const offset = computed(() => held.value?.offset ?? 0)

const members = computed<NamedRow[] | null>(() =>
  directoryData.value && directoryData.value.guildId === selected.value
    ? directoryData.value.members
    : null,
)

const entries = computed<RosterEntry[]>(() => rosterEntries(rows.value, members.value))
const inForce = computed(() => rows.value.filter((row) => row.active).length)
const summary = computed(() => rosterSummary(total.value, offset.value, rows.value.length))
/** A link to a page the roster no longer has. Not the same as "nobody has
 *  consented", and telling somebody that instead would be a console
 *  reporting a stale bookmark as a fact about their server. */
const beyondTheEnd = computed(() => isPastTheEnd(total.value, rows.value.length, page.value))

/* -------------------------------------------------------------------- */
/* The selection                                                        */
/* -------------------------------------------------------------------- */

const picked = ref<readonly string[]>([])

/**
 * Everybody the reader has seen, kept by id.
 *
 * A selection outlives the page it was made on -- that is what
 * `selectionSummary` announces -- so the confirmation has to name people
 * whose rows are no longer in hand. Remembering them as they go past is the
 * only way to state exactly who a batch applies to without a second
 * request, and a confirmation that could not name them would be a
 * confirmation of a number.
 */
const known = ref<Record<string, RosterPerson>>({})

watch(
  entries,
  (seen) => {
    known.value = rememberPeople(
      known.value,
      seen.map((entry) => entry.person),
    )
  },
  { immediate: true },
)

const chosen = computed(() => chosenPeople(known.value, picked.value))
const verdict = computed(() => batchVerdict(picked.value))
const confirmation = computed(() => bulkConfirmation(chosen.value))

/* -------------------------------------------------------------------- */
/* Per-row state                                                        */
/* -------------------------------------------------------------------- */

/** The person whose confirmation panel is open. One at a time: two open
 *  panels with the same red button on each is how the wrong one is
 *  clicked. */
const confirming = ref<string | null>(null)
const busy = ref<Record<string, boolean>>({})
const failures = ref<Record<string, string>>({})
const outcomes = ref<Record<string, RevokeOutcome | null>>({})
/** What the API said it actually did with the instant, as keyed lines.
 *  Kept beside the English outcome rather than folded into it: the outcome
 *  is `~/utils/consents`' prose and the instant is new, translated
 *  material. */
const instantOutcomes = ref<Record<string, Line[]>>({})

/**
 * The instant the open confirmation would take effect, as an ISO-8601
 * string with its offset already attached -- `UiDatePicker` emits nothing
 * else.
 *
 * Null means *now*, and null is the default. One value rather than one per
 * row because only one confirmation is ever open: two remembered instants
 * would be one instant attached to the wrong person the moment a panel is
 * closed and another opened.
 */
const effectiveAt = ref<string | null>(null)

// A moment chosen for one person must never survive into another's
// confirmation. It is the single most dangerous piece of state on this
// page: a withdrawal is destructive, back-dating it is more so, and an
// instant that quietly carried over would be applied to somebody nobody
// chose it for.
watch(confirming, () => {
  effectiveAt.value = null
})

/** Why the chosen instant cannot be sent, or null when it can. Null while
 *  the control is untouched: "now" is always legal. */
function instantProblem(row: ConsentRow): Line | null {
  if (!effectiveAt.value) return null
  const verdictFor = validateEffectiveAt(effectiveAt.value, row.granted_at)
  return verdictFor.ok ? null : verdictFor.problem
}

/**
 * What the confirmation gains because of the instant that was chosen.
 *
 * Nothing at all for "now", which is what keeps the unchanged path
 * unchanged. `new Date()` is read inside `effectiveKind` rather than held
 * in a ref because this only ever runs in a browser -- the panel exists
 * after a click -- and because a clock captured once would call a future
 * instant "past" for anybody who left the tab open.
 */
function instantConsequence(row: ConsentRow): Line[] {
  if (!effectiveAt.value || instantProblem(row)) return []
  return effectiveConsequence(
    effectiveKind(effectiveAt.value),
    effectiveAt.value,
    row.recordings_with_audio,
  )
}

/** The earliest day the picker will offer, in the reader's own zone. A
 *  bound they can see beats one they discover by tripping over it, and the
 *  offset is read from the grant instant itself so a date in another
 *  daylight-saving period is not a day out. */
function floorOf(iso: string | null): string | null {
  if (!iso) return null
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? null : grantFloor(iso, at.getTimezoneOffset())
}

/** Why a withdrawal is not offered. Empty when it is. Written out here
 *  rather than inline in the template because a discriminated union does
 *  not narrow across two separate calls, and calling it twice in one
 *  expression is how the second call ends up asking a different question
 *  than the first. */
function blockedReason(row: ConsentRow): string {
  const verdictFor = revocability(row)
  return verdictFor.revocable ? '' : verdictFor.reason
}

/* -------------------------------------------------------------------- */
/* The batch                                                            */
/* -------------------------------------------------------------------- */

const bulkOpen = ref(false)
const bulkBusy = ref(false)
const bulkFailure = ref('')
const bulkResult = ref<BulkRevokeResult | null>(null)
const bulkEffectiveAt = ref<string | null>(null)

/** The floor for a batch is the *latest* grant among the people chosen. An
 *  instant legal for nine and not the tenth would earn nine withdrawals and
 *  one `effective_before_grant`, which is a half-applied act explained
 *  afterwards rather than a whole one refused before. */
const bulkFloor = computed(() => latestGrant(chosen.value))

const bulkProblem = computed<Line | null>(() => {
  if (!bulkEffectiveAt.value) return null
  const verdictFor = validateEffectiveAt(bulkEffectiveAt.value, bulkFloor.value)
  return verdictFor.ok ? null : verdictFor.problem
})

const bulkConsequence = computed<Line[]>(() => {
  if (!bulkEffectiveAt.value || bulkProblem.value) return []
  // No count of recordings here. The figure `effectiveConsequence` names
  // belongs to one person, and summing it over a selection whose off-page
  // members this page holds no counts for would be a number invented to
  // fill a sentence. The per-row panel still names it for each of them.
  return effectiveConsequence(effectiveKind(bulkEffectiveAt.value), bulkEffectiveAt.value, 0)
})

const outcomeRows = computed(() =>
  bulkResult.value ? bulkOutcomeRows(bulkResult.value, known.value) : [],
)
const tally = computed(() => (bulkResult.value ? bulkTally(bulkResult.value) : null))

function openBulk() {
  bulkResult.value = null
  bulkFailure.value = ''
  bulkEffectiveAt.value = null
  bulkOpen.value = true
}

async function commitBulk() {
  if (!verdict.value.ok || bulkProblem.value) return
  const guildId = selected.value
  const body = bulkRevokeBody(picked.value, bulkEffectiveAt.value)
  bulkOpen.value = false
  bulkFailure.value = ''
  bulkResult.value = null
  bulkBusy.value = true
  try {
    const answer = await api<unknown>(`/guilds/${guildId}/consents/revoke`, {
      method: 'POST',
      body,
    })
    // The endpoint's own answer decides what is reported, never the fact
    // that the call did not throw. It answers 200 for a mixed result on
    // purpose, so every person in the body gets their own sentence below.
    bulkResult.value = parseBulkRevoke(answer)
    // Cleared on the way out rather than kept: the rows below have just
    // changed state, and a selection still standing over them is a second
    // press away from a batch that is now entirely refusals. `known` stays,
    // because the outcome list underneath still has to name these people.
    picked.value = []
    await refresh()
  } catch (error) {
    bulkFailure.value = describeConsentError(error)
    await refresh()
  } finally {
    bulkBusy.value = false
  }
}

/* -------------------------------------------------------------------- */
/* One person at a time                                                 */
/* -------------------------------------------------------------------- */

async function commit(entry: RosterEntry) {
  const row = entry.row
  if (instantProblem(row)) return
  const instant = effectiveAt.value
  confirming.value = null
  failures.value[row.discord_user_id] = ''
  outcomes.value[row.discord_user_id] = null
  instantOutcomes.value[row.discord_user_id] = []
  busy.value[row.discord_user_id] = true
  const guildId = selected.value
  try {
    const answer = await api<unknown>(
      `/guilds/${guildId}/consents/${row.discord_user_id}/revoke`,
      // No instant, no body. An administrator who never opened the control
      // sends byte for byte the request this page has always sent, which is
      // what makes "the default is now" a fact about the wire rather than a
      // claim about the interface.
      instant ? { method: 'POST', body: { effective_at: instant } } : { method: 'POST' },
    )
    // The endpoint's own answer decides what is reported, never the fact
    // that the call did not throw. A body this console cannot read counts
    // as a refusal: the only person who would find out otherwise is the one
    // still being recorded.
    const result = parseRevokeResult(answer)
    outcomes.value[row.discord_user_id] = revokeOutcome(row, result, entry.person.label)
    // Only when the write went through. Repeating "it takes effect on
    // Tuesday" under a refusal would describe a withdrawal that does not
    // exist.
    instantOutcomes.value[row.discord_user_id] = result.revoked
      ? effectiveOutcome(result.effective_at, result.recordings_from_effective_at)
      : []
    await refresh()
  } catch (error) {
    failures.value[row.discord_user_id] = describeConsentError(error)
    // A refusal always means this row was out of date -- the endpoint says
    // no exactly when there is no consent left to withdraw -- so the list is
    // reloaded rather than left showing the state that was already wrong
    // when it was clicked.
    if (isStaleRow(error)) await refresh()
  } finally {
    busy.value[row.discord_user_id] = false
  }
}

// Every guild has its own people, and a panel, a selection or an outcome
// left over from the previous one would sit on whichever row happened to
// land in the same position -- attached to a person it says nothing true
// about.
watch(selected, () => {
  confirming.value = null
  busy.value = {}
  failures.value = {}
  outcomes.value = {}
  instantOutcomes.value = {}
  picked.value = []
  known.value = {}
  bulkOpen.value = false
  bulkResult.value = null
  bulkFailure.value = ''
  if (page.value !== 1) goToPage(1)
})

/** Three states, three colours. Rendering "withdrawn" and "the policy
 *  version they agreed to has moved on" in the same grey would hide which
 *  of the two happened, and only one of them was the person's own
 *  decision. */
const TONE_COLOUR: Record<string, string> = {
  active: 'var(--color-brand-green)',
  superseded: 'var(--color-brand-yellow)',
  withdrawn: 'var(--color-brand-magenta)',
  done: 'var(--color-brand-green)',
  refused: 'var(--color-brand-yellow)',
}
</script>

<template>
  <div class="max-w-3xl">
    <h1 class="mb-1 text-2xl font-semibold">Consents</h1>
    <p class="mb-6 text-sm" :style="{ color: 'var(--text-muted)' }">
      Everybody who has ever given consent to be recorded in one server, whether that consent still
      counts, and how much of them Sturnus still holds. Consent is given by the person, in Discord,
      with <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">/consent grant</code> —
      the only thing that can be done from here is to withdraw one.
    </p>

    <!-- The three limits of a withdrawal, said before anybody opens a
         confirmation as well as inside it. A footnote alone would be read
         once, by the person who was already careful. -->
    <section
      class="mb-8 rounded-xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="mb-2 text-sm font-semibold">What withdrawing a consent here does, and does not</h2>
      <ul class="flex flex-col gap-2 text-sm" :style="{ color: 'var(--text-muted)' }">
        <li>{{ ROLE_STAYS_NOTE }}</li>
        <li>
          Nothing already recorded is deleted. Every recording that already contains somebody's
          audio stays exactly where it is; erasing them is a separate act,
          <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">/audio purge</code> in
          Discord.
        </li>
        <li>{{ AUDIT_LOG_NOTE }}</li>
      </ul>
    </section>

    <p
      v-if="guildError"
      class="rounded-xl border p-4 text-sm"
      :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
    >
      {{ describeConsentError(guildError) }}
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
        This section lists the consents of one Discord server, and it is open to the administrators
        of a server where Sturnus is running. You administer none of them right now.
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
             standing between an administrator and withdrawing a consent in
             the wrong server. The server is named by its *name*, and its id
             rides along as the row's subtext — which is what `detail` is
             for, and why the id is not repeated in a paragraph underneath. -->
        <template v-if="guilds.length > 1">
          <!-- A caption rather than a `<label for>`: the control is a
               button wearing `role="combobox"` and it carries its own
               accessible name. Two names for one control is how a screen
               reader ends up reading the caption twice. -->
          <p
            class="mb-2 text-xs font-medium uppercase tracking-wide"
            :style="{ color: 'var(--text-muted)' }"
          >
            {{ $t('admin.consents.roster.server') }}
          </p>
          <UiSelect
            :model-value="selected"
            :options="guildChoices"
            :label="$t('admin.consents.roster.server')"
            @update:model-value="chooseGuildFromMenu"
          />
        </template>
        <template v-else>
          <p class="text-sm">
            <span :style="{ color: 'var(--text-muted)' }">Showing</span>
            <span class="ml-1 font-medium">{{ currentGuild ? guildLabel(currentGuild) : '—' }}</span>
          </p>
          <p v-if="currentGuild" class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
            Guild ID
            <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">{{
              currentGuild.id
            }}</code>
          </p>
        </template>
      </section>

      <p
        v-if="consentError"
        class="mb-6 rounded-xl border p-4 text-sm"
        :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
      >
        {{ describeConsentError(consentError) }}
      </p>

      <template v-else>
        <p
          v-if="consentStatus === 'pending' && held === null"
          class="text-sm"
          :style="{ color: 'var(--text-muted)' }"
        >
          Reading this server's consents…
        </p>

        <!-- A link to a page that has since emptied. Saying "nobody has
             consented" here would be a stale bookmark reported as a fact
             about the server. -->
        <section
          v-else-if="beyondTheEnd"
          class="rounded-xl border p-6"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ $t('admin.consents.roster.beyondEnd') }}
          </p>
          <button
            type="button"
            class="rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)]"
            :style="{ borderColor: 'var(--border)' }"
            @click="goToPage(1)"
          >
            {{ $t('admin.consents.roster.backToFirst') }}
          </button>
        </section>

        <!-- A guild with no consent records at all is not a blank page. It
             is a server where nobody has run /consent grant yet, which is
             worth saying outright: an administrator wondering why a meeting
             recorded nothing usually ends up here. -->
        <section
          v-else-if="total === 0"
          class="rounded-xl border p-6"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
        >
          <h2 class="mb-2 text-base font-semibold">Nobody has consented in this server yet</h2>
          <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
            Sturnus holds no consent record for anybody here, so a session in this server records
            no audio at all — it can still note who was present, and it captures nobody's voice.
          </p>
          <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
            Each member gives consent themselves, with
            <code class="rounded bg-[var(--surface-raised)] px-1 font-mono">/consent grant</code>
            in Discord. Nobody, including an administrator, can give it on their behalf, which is
            why this page can only ever take one away.
          </p>
        </section>

        <template v-else>
          <p class="mb-1 text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ say(rosterCount(total)) }}
          </p>
          <!-- Two sentences rather than one. The total is the API's and
               describes the whole server; "in force" can only be counted
               over the rows in hand, and one sentence carrying both would
               silently say "3 of 400" while counting 3 of 20. -->
          <p class="mb-4 text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ say(rosterInForce(inForce, rows.length)) }}
          </p>

          <!-- The batch confirmation sits above the list, next to the bar
               that opened it. A destructive panel that appears somewhere
               the reader is not looking is a panel that gets confirmed
               without being read. -->
          <section
            v-if="bulkOpen"
            class="mb-4 rounded-xl border p-4"
            :style="{
              borderColor: 'var(--color-brand-red)',
              background: 'var(--surface-raised)',
            }"
          >
            <h2 class="mb-2 text-sm font-semibold">{{ say(confirmation.title) }}</h2>

            <p
              v-if="!verdict.ok"
              class="mb-3 rounded-lg border p-2 text-sm"
              :style="{ borderColor: 'var(--danger)', color: 'var(--danger)' }"
            >
              {{ say(verdict.problem) }}
            </p>

            <template v-else>
              <!-- Exactly who, by name, before anything happens to
                   anybody. The reader may be on a different page from the
                   rows they ticked, so a confirmation naming a count and
                   not the people would be a confirmation they cannot
                   check. -->
              <p class="mb-1 text-sm" :style="{ color: 'var(--text-muted)' }">
                {{ say(confirmation.lead) }}
              </p>
              <ul class="mb-3 flex flex-col gap-1">
                <li
                  v-for="who in confirmation.people"
                  :key="who.id"
                  class="text-sm"
                >
                  <span class="font-medium">{{ who.label }}</span>
                  <span
                    v-if="who.source !== 'record' && who.source !== 'directory'"
                    class="ml-2 text-xs"
                    :style="{ color: 'var(--text-muted)' }"
                  >{{ $t('admin.consents.roster.userId') }}</span>
                </li>
              </ul>

              <!-- Three sentences, kept as three, for the same reason the
                   single-row confirmation keeps its three: a paragraph
                   carrying all of them is skimmed exactly where the reader
                   most needs to notice that the roles and the recordings
                   are not part of this. -->
              <p
                v-for="consequence in confirmation.consequences"
                :key="consequence.key"
                class="mb-2 text-sm"
                :style="{ color: 'var(--text-muted)' }"
              >
                {{ say(consequence) }}
              </p>

              <div class="mt-3 rounded-lg border p-3" :style="{ borderColor: 'var(--border)' }">
                <p class="mb-1 text-xs font-medium uppercase tracking-wide" :style="{ color: 'var(--text-muted)' }">
                  {{ $t('admin.consents.bulk.effectiveLegend') }}
                </p>
                <p class="mb-2 text-xs" :style="{ color: 'var(--text-muted)' }">
                  {{ $t('admin.consents.bulk.effectiveNote') }}
                </p>
                <!-- The picker emits ISO-8601 with the offset of the
                     *chosen* moment, because it calls the module that
                     worked that out. Nothing here attaches an offset. -->
                <UiDatePicker
                  v-model="bulkEffectiveAt"
                  :label="$t('admin.consents.bulk.effectiveLegend')"
                  :min="floorOf(bulkFloor)"
                  :invalid="Boolean(bulkProblem)"
                />
                <button
                  v-if="bulkEffectiveAt"
                  type="button"
                  class="mt-2 rounded-lg border px-3 py-1.5 text-xs transition-colors hover:bg-[var(--surface)]"
                  :style="{ borderColor: 'var(--border)' }"
                  @click="bulkEffectiveAt = null"
                >
                  {{ $t('admin.consents.effective.reset') }}
                </button>
              </div>

              <p
                v-for="line in bulkConsequence"
                :key="line.key"
                class="mt-2 text-sm"
                :style="{ color: 'var(--text-muted)' }"
              >
                {{ $t(line.key, line.values ?? {}) }}
              </p>

              <p
                v-if="bulkProblem"
                class="mt-2 rounded-lg border p-2 text-sm"
                :style="{ borderColor: 'var(--danger)', color: 'var(--danger)' }"
              >
                {{ $t(bulkProblem.key, bulkProblem.values ?? {}) }}
              </p>
            </template>

            <div class="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                class="rounded-lg px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
                :style="{ background: 'var(--color-brand-red)' }"
                :disabled="!verdict.ok || Boolean(bulkProblem) || bulkBusy"
                @click="commitBulk"
              >
                {{ bulkBusy ? $t('admin.consents.bulk.working') : say(confirmation.confirmLabel) }}
              </button>
              <button
                type="button"
                class="rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface)]"
                :style="{ borderColor: 'var(--border)' }"
                @click="bulkOpen = false"
              >
                {{ $t('admin.consents.bulk.cancel') }}
              </button>
            </div>
          </section>

          <!-- The mixed answer, person by person. This is the whole reason
               the endpoint answers 200 for a partial success: "some were
               refused" is an administrator who has to withdraw all ten
               again one at a time to find out which. -->
          <section
            v-if="bulkResult"
            class="mb-4 rounded-xl border p-4"
            :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
          >
            <p v-if="tally" class="text-sm font-semibold">{{ say(tally) }}</p>
            <p class="mt-1 mb-2 text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ $t('admin.consents.bulk.perPerson') }}
            </p>
            <ul class="flex flex-col gap-2">
              <li
                v-for="result in outcomeRows"
                :key="result.person.id"
                class="rounded-lg border-l-2 pl-3"
                :style="{ borderColor: TONE_COLOUR[result.tone] }"
              >
                <p class="text-sm font-medium">{{ result.person.label }}</p>
                <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
                  {{ say(result.sentence) }}
                </p>
                <p
                  v-for="line in result.detail"
                  :key="line.key"
                  class="text-sm"
                  :style="{ color: 'var(--text-muted)' }"
                >
                  {{ $t(line.key, line.values ?? {}) }}
                </p>
              </li>
            </ul>
            <button
              type="button"
              class="mt-3 rounded-lg border px-3 py-1.5 text-xs transition-colors hover:bg-[var(--surface-raised)]"
              :style="{ borderColor: 'var(--border)' }"
              @click="bulkResult = null"
            >
              {{ $t('admin.consents.bulk.dismiss') }}
            </button>
          </section>

          <p
            v-if="bulkFailure"
            class="mb-4 rounded-xl border p-4 text-sm"
            :style="{ borderColor: 'var(--color-brand-red)' }"
          >
            {{ bulkFailure }}
          </p>

          <UiDisclosureList
            v-model:selected="picked"
            :rows="entries"
            :bulk-action="$t('admin.consents.bulk.action')"
            :label="$t('admin.consents.roster.list')"
            @bulk="openBulk"
          >
            <!-- Collapsed: who, and where they stand. Nothing else — an
                 administrator scans this list for a person, and a row that
                 also carried the policy version and two dates would be a
                 row nobody scans. -->
            <template #row="{ row: entry }">
              <span class="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
                <span class="truncate font-medium">{{ entry.person.label }}</span>
                <!-- The bare id is still a person, and the marker says so
                     at a glance. The sentence explaining which of the two
                     reasons applies is inside the row, where there is
                     space for it. -->
                <span
                  v-if="entry.person.source !== 'record' && entry.person.source !== 'directory'"
                  class="shrink-0 text-xs"
                  :style="{ color: 'var(--text-muted)' }"
                >{{ $t('admin.consents.roster.userId') }}</span>
                <span
                  class="shrink-0 rounded-full border px-2 py-0.5 text-xs font-medium"
                  :style="{
                    borderColor: TONE_COLOUR[consentBadge(entry.row).tone],
                    color: TONE_COLOUR[consentBadge(entry.row).tone],
                  }"
                >
                  {{ consentBadge(entry.row).label }}
                </span>
              </span>
            </template>

            <template #actions="{ row: entry }">
              <!-- The badge's long form is here rather than in a tooltip:
                   the difference between "withdrawn" and "the policy
                   version moved on" decides what to do next, and nobody
                   hovers to find that out. -->
              <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
                {{ consentBadge(entry.row).detail }}
              </p>

              <p
                v-if="nameNote(entry.person)"
                class="mt-2 text-xs"
                :style="{ color: 'var(--text-muted)' }"
              >
                {{ say(nameNote(entry.person)) }}
              </p>

              <dl class="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
                <div>
                  <dt class="inline font-medium">Policy version ·</dt>
                  <dd class="inline" :style="{ color: 'var(--text-muted)' }">
                    {{ policyLine(entry.row) }}
                  </dd>
                </div>
                <div>
                  <dt class="inline font-medium">Granted ·</dt>
                  <dd class="inline" :style="{ color: 'var(--text-muted)' }">
                    {{ grantedLine(entry.row) }}
                  </dd>
                </div>
                <!-- On the roster so that "who agreed to video" is a
                     question an administrator can answer by reading rather
                     than by opening anything. It is not editable here:
                     narrowing somebody else's scope stops short of stopping
                     the recording, and widening it would be granting
                     consent on their behalf. -->
                <div>
                  <dt class="inline font-medium">{{ $t('admin.consents.scope.label') }} ·</dt>
                  <dd class="inline" :style="{ color: 'var(--text-muted)' }">
                    {{ $t(scopeLineKey(entry.row)) }}
                  </dd>
                </div>
                <div v-if="withdrawnLine(entry.row)">
                  <dt class="inline font-medium">Withdrawn ·</dt>
                  <dd class="inline" :style="{ color: 'var(--text-muted)' }">
                    {{ withdrawnLine(entry.row) }}
                  </dd>
                </div>
                <!-- Inside the row as well as in the confirmation, because
                     it answers the question an administrator is usually
                     really asking. Somebody who came to erase a person's
                     audio has come to the wrong page, and finds that out
                     here. -->
                <div>
                  <dt class="inline font-medium">Recordings held ·</dt>
                  <dd class="inline" :style="{ color: 'var(--text-muted)' }">
                    {{ recordingsLine(entry.row) }}
                  </dd>
                </div>
                <!-- The id is secondary everywhere else on this page, and
                     this is the one place it is still worth having: it is
                     what an administrator quotes in a ticket. -->
                <div>
                  <dt class="inline font-medium">{{ $t('admin.consents.roster.userId') }} ·</dt>
                  <dd class="inline">
                    <code class="font-mono" :style="{ color: 'var(--text-muted)' }">{{
                      entry.row.discord_user_id
                    }}</code>
                  </dd>
                </div>
              </dl>

              <div class="mt-3 flex flex-wrap items-center gap-2">
                <button
                  v-if="revocability(entry.row).revocable"
                  type="button"
                  class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
                  :style="{ borderColor: 'var(--color-brand-red)', color: 'var(--color-brand-red)' }"
                  :disabled="busy[entry.row.discord_user_id]"
                  @click="confirming = entry.row.discord_user_id"
                >
                  {{ busy[entry.row.discord_user_id] ? 'Withdrawing…' : 'Withdraw consent' }}
                </button>
                <!-- No button for a consent already withdrawn. The API
                     answers 409, and an interface that offers an action it
                     knows will fail is worse than one that explains why it
                     cannot. -->
                <span v-else class="text-xs" :style="{ color: 'var(--text-muted)' }">
                  {{ blockedReason(entry.row) }}
                </span>
              </div>

              <div
                v-if="confirming === entry.row.discord_user_id"
                class="mt-3 rounded-lg border p-3"
                :style="{
                  borderColor: 'var(--color-brand-red)',
                  background: 'var(--surface-raised)',
                }"
              >
                <p class="mb-2 text-sm font-semibold">
                  {{ revokeConfirmation(entry.row, entry.person.label).title }}
                </p>
                <!-- Three sentences, kept as three. A single paragraph
                     carrying all of them is one that gets skimmed, exactly
                     where the reader most needs to notice that the role and
                     the recordings are not part of this. -->
                <p
                  v-for="consequence in revokeConfirmation(entry.row, entry.person.label).consequences"
                  :key="consequence"
                  class="mb-2 text-sm"
                  :style="{ color: 'var(--text-muted)' }"
                >
                  {{ consequence }}
                </p>

                <!-- The effective instant. Left alone, this confirmation
                     sends the request it has always sent; used, it is where
                     a withdrawal becomes a statement about a moment rather
                     than about now. -->
                <div class="mt-3 rounded-lg border p-3" :style="{ borderColor: 'var(--border)' }">
                  <p
                    class="mb-1 text-xs font-medium uppercase tracking-wide"
                    :style="{ color: 'var(--text-muted)' }"
                  >
                    {{ $t('admin.consents.effective.legend') }}
                  </p>
                  <p class="text-xs" :style="{ color: 'var(--text-muted)' }">
                    {{ $t('admin.consents.effective.nowNote') }}
                  </p>
                  <p class="mt-1 mb-2 text-xs" :style="{ color: 'var(--text-muted)' }">
                    {{ $t('admin.consents.effective.help') }}
                  </p>
                  <UiDatePicker
                    v-model="effectiveAt"
                    :label="$t('admin.consents.effective.legend')"
                    :min="floorOf(entry.row.granted_at)"
                    :invalid="Boolean(instantProblem(entry.row))"
                  />
                  <button
                    v-if="effectiveAt"
                    type="button"
                    class="mt-2 rounded-lg border px-3 py-1.5 text-xs transition-colors hover:bg-[var(--surface)]"
                    :style="{ borderColor: 'var(--border)' }"
                    @click="effectiveAt = null"
                  >
                    {{ $t('admin.consents.effective.reset') }}
                  </button>
                </div>

                <!-- Said before the confirmation is pressed, never after.
                     Choosing a past instant and being shown nothing would
                     let somebody believe they had erased something. -->
                <p
                  v-for="line in instantConsequence(entry.row)"
                  :key="line.key"
                  class="mt-2 text-sm"
                  :style="{ color: 'var(--text-muted)' }"
                >
                  {{ $t(line.key, line.values ?? {}) }}
                </p>

                <p
                  v-if="instantProblem(entry.row)"
                  class="mt-2 rounded-lg border p-2 text-sm"
                  :style="{ borderColor: 'var(--danger)', color: 'var(--danger)' }"
                >
                  {{ $t(instantProblem(entry.row)!.key, instantProblem(entry.row)!.values ?? {}) }}
                </p>

                <div class="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    class="rounded-lg px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
                    :style="{ background: 'var(--color-brand-red)' }"
                    :disabled="Boolean(instantProblem(entry.row))"
                    @click="commit(entry)"
                  >
                    {{ revokeConfirmation(entry.row, entry.person.label).confirmLabel }}
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
                v-if="outcomes[entry.row.discord_user_id]"
                class="mt-3 rounded-lg border p-3"
                :style="{
                  borderColor: TONE_COLOUR[outcomes[entry.row.discord_user_id]!.tone],
                  background: 'var(--surface-raised)',
                }"
              >
                <p class="text-sm font-semibold">
                  {{ outcomes[entry.row.discord_user_id]!.headline }}
                </p>
                <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
                  {{ outcomes[entry.row.discord_user_id]!.detail }}
                </p>
                <!-- What the API says it did with the instant, from its own
                     answer rather than from the request: the console's
                     arithmetic and the API's are two arithmetics, and only
                     one of them has the recordings table. -->
                <p
                  v-for="line in instantOutcomes[entry.row.discord_user_id] ?? []"
                  :key="line.key"
                  class="mt-1 text-sm"
                  :style="{ color: 'var(--text-muted)' }"
                >
                  {{ $t(line.key, line.values ?? {}) }}
                </p>
              </div>

              <p
                v-if="failures[entry.row.discord_user_id]"
                class="mt-3 rounded-lg border p-3 text-sm"
                :style="{ borderColor: 'var(--color-brand-red)' }"
              >
                {{ failures[entry.row.discord_user_id] }}
              </p>
            </template>
          </UiDisclosureList>

          <div class="mt-4 flex flex-col items-center gap-2">
            <UiPagination
              :page="page"
              :total="total"
              :size="PAGE_SIZE"
              @update:page="goToPage"
            />
            <p v-if="summary" class="text-sm" :style="{ color: 'var(--text-muted)' }">
              {{ say(summary) }}
            </p>
          </div>
        </template>
      </template>
    </template>
  </div>
</template>
