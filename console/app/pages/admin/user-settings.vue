<script setup lang="ts">
/**
 * Who has consented to being recorded in one guild, and the one thing an
 * administrator can do about it from here: withdraw a consent.
 *
 * Everything that is a *decision* -- which row comes first, what a state
 * badge says, whether a revoke is offered at all, how a refusal reads as a
 * sentence, how a moment is written -- lives in `~/utils/consents` and is
 * tested there. What is left in this file is layout, request plumbing and
 * per-row state. The guild picker is not reimplemented either: it is the
 * same functions the Bot Settings page uses, down to the remembered choice,
 * so switching servers on one admin page carries to the other.
 *
 * Two things this page refuses to do, both on purpose:
 *
 * - **It never lets a withdrawal read as more than it is.** The Discord
 *   role stays, and the recordings stay. Both limits are said in the
 *   confirmation, before the act, and again in the panel afterwards -- not
 *   only in a footnote nobody reads twice.
 * - **It never offers an action it knows will fail.** A consent already
 *   withdrawn has no button at all, only the sentence saying when it went.
 */
import {
  AUDIT_LOG_NOTE,
  ROLE_STAYS_NOTE,
  type ConsentRow,
  type RevokeOutcome,
  activeCount,
  consentBadge,
  describeConsentError,
  grantedLine,
  identityNote,
  isStaleRow,
  orderConsents,
  parseConsents,
  parseRevokeResult,
  personLabel,
  policyLine,
  recordingsLine,
  revocability,
  revokeConfirmation,
  revokeOutcome,
  withdrawnLine,
} from '~/utils/consents'
import {
  chooseGuild,
  guildLabel,
  parseGuilds,
  readSelectedGuild,
  writeSelectedGuild,
} from '~/utils/settings'

useHead({ title: 'User Settings' })

const api = useApi()

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
    if (!guildId) return { guildId: null as string | null, rows: [] as ConsentRow[] }
    return {
      guildId,
      rows: orderConsents(parseConsents(await api(`/guilds/${guildId}/consents`))),
    }
  },
  { watch: [selected] },
)

/** Nothing is shown while the answer on hand belongs to another guild.
 *  Withdrawing somebody's consent from a list loaded for a different server
 *  is the exact mistake the switcher exists to prevent, and a list that
 *  lingers for a few hundred milliseconds under the new heading is long
 *  enough to click. */
const rows = computed(() =>
  consentData.value && consentData.value.guildId === selected.value ? consentData.value.rows : [],
)
const inForce = computed(() => activeCount(rows.value))
const currentGuild = computed(() => guilds.value.find((guild) => guild.id === selected.value) ?? null)

/** The person whose confirmation panel is open. One at a time: two open
 *  panels with the same red button on each is how the wrong one is
 *  clicked. */
const confirming = ref<string | null>(null)
const busy = ref<Record<string, boolean>>({})
const failures = ref<Record<string, string>>({})
const outcomes = ref<Record<string, RevokeOutcome | null>>({})

// Every guild has its own people, and a panel or an outcome left over from
// the previous selection would sit on whichever row happened to land in the
// same position -- attached to a person it says nothing true about.
watch(selected, () => {
  confirming.value = null
  busy.value = {}
  failures.value = {}
  outcomes.value = {}
})

async function commit(row: ConsentRow) {
  confirming.value = null
  failures.value[row.discord_user_id] = ''
  outcomes.value[row.discord_user_id] = null
  busy.value[row.discord_user_id] = true
  const guildId = selected.value
  try {
    const answer = await api<unknown>(
      `/guilds/${guildId}/consents/${row.discord_user_id}/revoke`,
      { method: 'POST' },
    )
    // The endpoint's own answer decides what is reported, never the fact
    // that the call did not throw. A body this console cannot read counts
    // as a refusal: the only person who would find out otherwise is the one
    // still being recorded.
    outcomes.value[row.discord_user_id] = revokeOutcome(row, parseRevokeResult(answer))
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

/** Why a withdrawal is not offered. Empty when it is. Written out here
 *  rather than inline in the template because a discriminated union does
 *  not narrow across two separate calls, and calling it twice in one
 *  expression is how the second call ends up asking a different question
 *  than the first. */
function blockedReason(row: ConsentRow): string {
  const verdict = revocability(row)
  return verdict.revocable ? '' : verdict.reason
}

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
    <h1 class="mb-1 text-2xl font-semibold">User Settings</h1>
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
             the wrong server, so the current one is named here and its id
             repeated underneath. -->
        <label
          v-if="guilds.length > 1"
          class="mb-2 block text-xs font-medium uppercase tracking-wide"
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
        v-if="consentError"
        class="mb-6 rounded-xl border p-4 text-sm"
        :style="{ borderColor: 'var(--color-brand-red)', background: 'var(--surface)' }"
      >
        {{ describeConsentError(consentError) }}
      </p>

      <template v-else>
        <p
          v-if="consentStatus === 'pending'"
          class="text-sm"
          :style="{ color: 'var(--text-muted)' }"
        >
          Reading this server's consents…
        </p>

        <!-- A guild with no consent records at all is not a blank page. It
             is a server where nobody has run /consent grant yet, which is
             worth saying outright: an administrator wondering why a meeting
             recorded nothing usually ends up here. -->
        <section
          v-else-if="rows.length === 0"
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
          <p class="mb-4 text-sm" :style="{ color: 'var(--text-muted)' }">
            {{ rows.length }} {{ rows.length === 1 ? 'person has' : 'people have' }} a consent
            record here; {{ inForce }} of them
            {{ inForce === 1 ? 'is' : 'are' }} in force right now.
          </p>

          <div class="flex flex-col gap-4">
            <article
              v-for="row in rows"
              :key="row.discord_user_id"
              class="rounded-xl border p-4"
              :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
            >
              <header class="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                <div>
                  <h2 class="text-sm font-semibold">{{ personLabel(row) }}</h2>
                  <code class="font-mono text-xs" :style="{ color: 'var(--text-muted)' }">{{
                    row.discord_user_id
                  }}</code>
                </div>
                <span
                  class="shrink-0 self-start rounded-full border px-2.5 py-1 text-xs font-medium"
                  :style="{
                    borderColor: TONE_COLOUR[consentBadge(row).tone],
                    color: TONE_COLOUR[consentBadge(row).tone],
                  }"
                >
                  {{ consentBadge(row).label }}
                </span>
              </header>

              <!-- The badge's long form is on the row rather than in a
                   tooltip: the difference between "withdrawn" and "the
                   policy version moved on" decides what to do next, and
                   nobody hovers to find that out. -->
              <p class="text-sm" :style="{ color: 'var(--text-muted)' }">
                {{ consentBadge(row).detail }}
              </p>

              <p
                v-if="identityNote(row)"
                class="mt-2 text-xs"
                :style="{ color: 'var(--text-muted)' }"
              >
                {{ identityNote(row) }}
              </p>

              <dl class="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
                <div>
                  <dt class="inline font-medium">Policy version ·</dt>
                  <dd class="inline" :style="{ color: 'var(--text-muted)' }">
                    {{ policyLine(row) }}
                  </dd>
                </div>
                <div>
                  <dt class="inline font-medium">Granted ·</dt>
                  <dd class="inline" :style="{ color: 'var(--text-muted)' }">
                    {{ grantedLine(row) }}
                  </dd>
                </div>
                <div v-if="withdrawnLine(row)">
                  <dt class="inline font-medium">Withdrawn ·</dt>
                  <dd class="inline" :style="{ color: 'var(--text-muted)' }">
                    {{ withdrawnLine(row) }}
                  </dd>
                </div>
                <!-- On the row as well as in the confirmation, because it
                     answers the question an administrator is usually really
                     asking. Somebody who came to erase a person's audio has
                     come to the wrong page, and finds that out here. -->
                <div>
                  <dt class="inline font-medium">Recordings held ·</dt>
                  <dd class="inline" :style="{ color: 'var(--text-muted)' }">
                    {{ recordingsLine(row) }}
                  </dd>
                </div>
              </dl>

              <div class="mt-3 flex flex-wrap items-center gap-2">
                <button
                  v-if="revocability(row).revocable"
                  type="button"
                  class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
                  :style="{ borderColor: 'var(--color-brand-red)', color: 'var(--color-brand-red)' }"
                  :disabled="busy[row.discord_user_id]"
                  @click="confirming = row.discord_user_id"
                >
                  {{ busy[row.discord_user_id] ? 'Withdrawing…' : 'Withdraw consent' }}
                </button>
                <!-- No button for a consent already withdrawn. The API
                     answers 409, and an interface that offers an action it
                     knows will fail is worse than one that explains why it
                     cannot. -->
                <span v-else class="text-xs" :style="{ color: 'var(--text-muted)' }">
                  {{ blockedReason(row) }}
                </span>
              </div>

              <div
                v-if="confirming === row.discord_user_id"
                class="mt-3 rounded-lg border p-3"
                :style="{
                  borderColor: 'var(--color-brand-red)',
                  background: 'var(--surface-raised)',
                }"
              >
                <p class="mb-2 text-sm font-semibold">{{ revokeConfirmation(row).title }}</p>
                <!-- Three sentences, kept as three. A single paragraph
                     carrying all of them is one that gets skimmed, exactly
                     where the reader most needs to notice that the role and
                     the recordings are not part of this. -->
                <p
                  v-for="consequence in revokeConfirmation(row).consequences"
                  :key="consequence"
                  class="mb-2 text-sm"
                  :style="{ color: 'var(--text-muted)' }"
                >
                  {{ consequence }}
                </p>
                <div class="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    class="rounded-lg px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90"
                    :style="{ background: 'var(--color-brand-red)' }"
                    @click="commit(row)"
                  >
                    {{ revokeConfirmation(row).confirmLabel }}
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
                v-if="outcomes[row.discord_user_id]"
                class="mt-3 rounded-lg border p-3"
                :style="{
                  borderColor: TONE_COLOUR[outcomes[row.discord_user_id]!.tone],
                  background: 'var(--surface-raised)',
                }"
              >
                <p class="text-sm font-semibold">{{ outcomes[row.discord_user_id]!.headline }}</p>
                <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
                  {{ outcomes[row.discord_user_id]!.detail }}
                </p>
              </div>

              <p
                v-if="failures[row.discord_user_id]"
                class="mt-3 rounded-lg border p-3 text-sm"
                :style="{ borderColor: 'var(--color-brand-red)' }"
              >
                {{ failures[row.discord_user_id] }}
              </p>
            </article>
          </div>
        </template>
      </template>
    </template>
  </div>
</template>
