<script setup lang="ts">
/**
 * The page that belongs to the person reading it.
 *
 * `/settings` used to be a permanent redirect to the bot's configuration --
 * the console's *administrative* settings, which is what "settings" meant
 * back when a person could decide nothing about themselves here. That
 * redirect is gone and this address is a page again, which is the one
 * breaking change in this pull request.
 *
 * Two preferences, and both of them are a person's rather than a server's:
 * which theme the console is in, and which language it speaks. Each is kept
 * in a cookie, and each has exactly one writer -- `useThemePreference` for
 * the theme, `setLocale` for the language, by way of `useLocalePreference`.
 * A page that wrote either cookie itself would be a second writer of one
 * value, and the two would eventually disagree about the path or the expiry.
 *
 * **The Consent section is the third, and it is the one this page exists
 * for.** Theme and language are conveniences; consent is a right somebody
 * exercises. Every decision it makes -- which sentence a state produces,
 * whether the video option exists at all, what a refusal reads like -- is
 * in `~/utils/myConsents` and tested without rendering anything. What is
 * left here is layout, request plumbing and per-guild state.
 *
 * Three things this section refuses to do, all three on purpose:
 *
 * - **It never renders an absence as an answer.** `GET /api/me/consents`
 *   answers 404 until the API that serves it is deployed, and this console
 *   ships as a separate image. A 404 drawn as an empty list would say "you
 *   have consented nowhere", which is a false statement about somebody's
 *   own data and the worst thing this page could say. So the failure is
 *   named and nothing else is shown.
 * - **It never offers a video option a server has no policy for.** When
 *   `video_consent_offered` is false the option is absent -- not disabled,
 *   not greyed -- and one sentence says the server records audio only.
 * - **It never lets "withdrawn" read as "erased".** The Discord role stays,
 *   the recordings stay, and both are said before the act and again after
 *   it.
 *
 * The Security section says a fourth thing, and can, because it is honest:
 * those two features are not built, the rows say exactly that, and they are
 * the same two rows the account menu shows -- read from the same list, so
 * the two cannot drift into promising different things.
 */
import {
  AUDIO_ONLY_KEY,
  type ConsentScope,
  type Line,
  type MyConsent,
  type Outcome,
  consentBadgeKey,
  consentNarrative,
  consentTone,
  describeMyConsentError,
  isConsentServiceMissing,
  mayChangeScope,
  orderMyConsents,
  parseMyConsents,
  parseMyRevokeResult,
  parseScopeResult,
  scopeChoices,
  scopeLabelKey,
  scopeOutcome,
  withdrawConfirmation,
  withdrawOutcome,
  withdrawability,
} from '~/utils/myConsents'
import { COMING_SOON_KEY, UNAVAILABLE_ITEMS } from '~/utils/profileMenu'
import { themeLabelKey } from '~/utils/theme'

const { t } = useI18n()
useHead({ title: t('settings.title') })

const api = useApi()

/**
 * The person's own consent records.
 *
 * `useAsyncData` so the first paint already carries them: this section is
 * the reason most people open this page, and a spinner where a fact belongs
 * is a page that makes somebody wait to find out whether they are being
 * recorded.
 */
const {
  data: consentData,
  error: consentError,
  status: consentStatus,
  refresh: refreshConsents,
} = await useAsyncData('my-consents', async () =>
  orderMyConsents(parseMyConsents(await api('/me/consents'))),
)

const consents = computed(() => consentData.value ?? [])
/** The one failure with a shape of its own. See the module comment. */
const serviceMissing = computed(() => isConsentServiceMissing(consentError.value))

/** The guild whose withdrawal confirmation is open. One at a time: two open
 *  panels with the same red button on each is how the wrong one is
 *  pressed. */
const confirming = ref<string | null>(null)
const busy = ref<Record<string, boolean>>({})
const scopeOutcomes = ref<Record<string, Outcome | null>>({})
const withdrawOutcomes = ref<Record<string, Outcome | null>>({})
const failures = ref<Record<string, Line | null>>({})

function clear(guildId: string) {
  failures.value[guildId] = null
  scopeOutcomes.value[guildId] = null
  withdrawOutcomes.value[guildId] = null
}

/**
 * Narrow or widen what may be recorded.
 *
 * Narrowing takes effect immediately and needs nothing. Widening inserts a
 * new consent record carrying the guild's current policy version, which is
 * the API's business and not this page's -- the console asks for a scope
 * and reports what it is told, and never computes a policy version of its
 * own.
 */
async function chooseScope(row: MyConsent, scope: ConsentScope) {
  if (scope === row.scope || busy.value[row.guild_id]) return
  clear(row.guild_id)
  busy.value[row.guild_id] = true
  try {
    const answer = await api<unknown>(`/me/consents/${row.guild_id}/scope`, {
      method: 'PUT',
      body: { scope },
    })
    // The endpoint's own answer decides what is reported, never the fact
    // that the call did not throw.
    scopeOutcomes.value[row.guild_id] = scopeOutcome(parseScopeResult(answer))
    await refreshConsents()
  } catch (error) {
    failures.value[row.guild_id] = describeMyConsentError(error)
    // A refusal always means the record on screen was out of date, so the
    // list is reloaded rather than left showing the state that was already
    // wrong when it was clicked.
    await refreshConsents()
  } finally {
    busy.value[row.guild_id] = false
  }
}

async function withdraw(row: MyConsent) {
  confirming.value = null
  clear(row.guild_id)
  busy.value[row.guild_id] = true
  try {
    const answer = await api<unknown>(`/me/consents/${row.guild_id}/revoke`, { method: 'POST' })
    withdrawOutcomes.value[row.guild_id] = withdrawOutcome(parseMyRevokeResult(answer))
    await refreshConsents()
  } catch (error) {
    failures.value[row.guild_id] = describeMyConsentError(error)
    await refreshConsents()
  } finally {
    busy.value[row.guild_id] = false
  }
}

/** Why no withdrawal is offered, or null when one is. Written out here
 *  rather than inline in the template because a discriminated union does not
 *  narrow across two separate calls, and calling it twice in one expression
 *  is how the second call ends up asking a different question than the
 *  first. */
function withdrawBlocked(row: MyConsent): Line | null {
  const verdict = withdrawability(row)
  return verdict.may ? null : verdict.reason
}

/** Four states, four colours. Rendering "withdrawn" and "the policy version
 *  moved on" in one grey would hide which of the two happened, and only one
 *  of them was this person's own decision. */
const TONE_COLOUR: Record<string, string> = {
  active: 'var(--color-brand-green)',
  scheduled: 'var(--color-brand-yellow)',
  withdrawn: 'var(--color-brand-magenta)',
  superseded: 'var(--color-brand-yellow)',
  done: 'var(--color-brand-green)',
  refused: 'var(--color-brand-yellow)',
}

// Destructured so the template reads `currentTheme` rather than
// `theme.current.value`: a ref returned at the top level of `setup` is
// unwrapped in the template, one nested inside an object is not.
const { available: themes, current: currentTheme, choose: chooseTheme } = useThemePreference()
const {
  available: languages,
  current: currentLanguage,
  choose: chooseLanguage,
} = useLocalePreference()

/** A chip that is the current answer, against one that is merely on offer. */
function chipStyle(selected: boolean) {
  return selected
    ? {
        background: 'var(--action)',
        color: 'var(--action-contrast)',
        borderColor: 'var(--action)',
      }
    : { borderColor: 'var(--control-border)', color: 'var(--text)' }
}
</script>

<template>
  <div class="max-w-3xl">
    <h1 class="mb-1 text-2xl font-semibold">{{ $t('settings.title') }}</h1>
    <p class="mb-6 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('settings.intro') }}
    </p>

    <!-- `fieldset` and `legend` rather than a heading over a row of buttons:
         a group of mutually exclusive choices is what a radio group is, and
         the native one comes with the arrow keys, the grouping a screen
         reader announces, and the "one of these is chosen" that a row of
         toggle buttons has to reimplement badly. The input is visually
         hidden and its label is what is painted, so the focus ring is put
         back on the label with `peer-focus-visible`. -->
    <fieldset
      class="mb-6 rounded-xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <legend class="px-1 text-base font-semibold">{{ $t('settings.appearance.title') }}</legend>
      <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('settings.appearance.note') }}
      </p>
      <div class="flex flex-wrap gap-2">
        <label
          v-for="choice in themes"
          :key="choice"
          class="cursor-pointer"
        >
          <input
            class="peer sr-only"
            type="radio"
            name="theme"
            :value="choice"
            :checked="currentTheme === choice"
            @change="chooseTheme(choice)"
          >
          <span
            class="block rounded-lg border px-3 py-2 text-sm transition-colors peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2"
            :style="chipStyle(currentTheme === choice)"
          >{{ $t(themeLabelKey(choice)) }}</span>
        </label>
      </div>
    </fieldset>

    <fieldset
      class="mb-6 rounded-xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <legend class="px-1 text-base font-semibold">{{ $t('settings.language.title') }}</legend>
      <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('settings.language.note') }}
      </p>
      <div class="flex flex-wrap gap-2">
        <label
          v-for="locale in languages"
          :key="locale.code"
          class="cursor-pointer"
        >
          <input
            class="peer sr-only"
            type="radio"
            name="locale"
            :value="locale.code"
            :checked="currentLanguage === locale.code"
            @change="chooseLanguage(locale.code)"
          >
          <!-- The language's own name, never a translation of it: somebody
               looking for German is looking for "Deutsch". -->
          <span
            class="block rounded-lg border px-3 py-2 text-sm transition-colors peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2"
            :style="chipStyle(currentLanguage === locale.code)"
          >{{ locale.name }}</span>
        </label>
      </div>
    </fieldset>

    <section
      class="mb-6 rounded-xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="text-base font-semibold">{{ $t('settings.consent.title') }}</h2>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('settings.consent.intro') }}
      </p>

      <p
        v-if="consentStatus === 'pending'"
        class="mt-3 text-sm"
        :style="{ color: 'var(--text-muted)' }"
      >
        {{ $t('settings.consent.loading') }}
      </p>

      <!-- **A 404 shows this and nothing else.** The API that serves these
           records may not be deployed yet, and an empty list drawn in its
           place would tell somebody they have consented nowhere — a false
           statement about their own data, and the one failure on this page
           that is worth a shape of its own. -->
      <div
        v-else-if="consentError"
        class="mt-3 rounded-lg border p-3"
        :style="{
          borderColor: serviceMissing ? 'var(--border)' : 'var(--danger)',
          background: 'var(--surface-raised)',
        }"
      >
        <p class="text-sm font-semibold">
          {{
            serviceMissing
              ? $t('settings.consent.unavailableHeading')
              : $t('settings.consent.errorHeading')
          }}
        </p>
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{
            $t(
              describeMyConsentError(consentError).key,
              describeMyConsentError(consentError).values ?? {},
            )
          }}
        </p>
      </div>

      <!-- Not a blank space. Somebody with no consent anywhere is being
           recorded nowhere, which is a fact worth stating, and the way to
           change it is in Discord rather than here. -->
      <div
        v-else-if="consents.length === 0"
        class="mt-3 rounded-lg border p-3"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface-raised)' }"
      >
        <p class="text-sm font-semibold">{{ $t('settings.consent.emptyHeading') }}</p>
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ $t('settings.consent.emptyNote') }}
        </p>
      </div>

      <template v-else>
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('settings.consent.serverIdNote') }}
        </p>

        <article
          v-for="row in consents"
          :key="row.guild_id"
          class="mt-4 rounded-lg border p-3"
          :style="{ borderColor: 'var(--border)', background: 'var(--surface-raised)' }"
        >
          <header class="mb-2 flex flex-wrap items-start justify-between gap-2">
            <h3 class="text-sm font-semibold">
              {{ $t('settings.consent.server', { guild: row.guild_id }) }}
            </h3>
            <span
              class="shrink-0 rounded-full border px-2.5 py-1 text-xs font-medium"
              :style="{
                borderColor: TONE_COLOUR[consentTone(row)],
                color: TONE_COLOUR[consentTone(row)],
              }"
            >
              {{ $t(consentBadgeKey(row)) }}
            </span>
          </header>

          <!-- Sentences rather than a table of raw fields: what is being
               recorded of somebody right now, under which policy version,
               since when, and — for a scheduled stop — that it is coming
               and when. A person whose consent runs out on Friday should
               see that on Tuesday. -->
          <p
            v-for="line in consentNarrative(row)"
            :key="line.key"
            class="mb-2 text-sm"
            :style="{ color: 'var(--text-muted)' }"
          >
            {{ $t(line.key, line.values ?? {}) }}
          </p>

          <!-- `fieldset` and `legend` rather than a heading over a row of
               buttons, for the same reason the theme chooser uses them: a
               group of mutually exclusive choices is what a radio group
               is, and the native one comes with the arrow keys and the
               grouping a screen reader announces. -->
          <fieldset v-if="mayChangeScope(row)" class="mt-3">
            <legend class="text-xs font-medium uppercase tracking-wide" :style="{ color: 'var(--text-muted)' }">
              {{ $t('settings.consent.scope.title') }}
            </legend>
            <div class="mt-2 flex flex-wrap gap-2">
              <!-- **When the guild does not offer video consent there is
                   no video option in this list at all.** Not disabled, not
                   greyed, not behind a tooltip: a consent record naming
                   video under a policy that describes only audio is not
                   consent, and this interface must not offer what it
                   cannot honour. -->
              <label v-for="choice in scopeChoices(row)" :key="choice" class="cursor-pointer">
                <input
                  class="peer sr-only"
                  type="radio"
                  :name="`scope-${row.guild_id}`"
                  :value="choice"
                  :checked="row.scope === choice"
                  :disabled="busy[row.guild_id]"
                  @change="chooseScope(row, choice)"
                >
                <span
                  class="block rounded-lg border px-3 py-2 text-sm transition-colors peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-disabled:opacity-40"
                  :style="chipStyle(row.scope === choice)"
                >{{ $t(scopeLabelKey(choice)) }}</span>
              </label>
            </div>
            <!-- One sentence in place of the option, so its absence reads
                 as a fact about this server rather than as a control that
                 failed to render. -->
            <p
              v-if="!row.video_consent_offered"
              class="mt-2 text-xs"
              :style="{ color: 'var(--text-muted)' }"
            >
              {{ $t(AUDIO_ONLY_KEY) }}
            </p>
            <p class="mt-2 text-xs" :style="{ color: 'var(--text-muted)' }">
              {{ $t('settings.consent.scope.note') }}
            </p>
          </fieldset>
          <p v-else class="mt-3 text-xs" :style="{ color: 'var(--text-muted)' }">
            {{ $t('settings.consent.scope.locked') }}
          </p>

          <div
            v-if="scopeOutcomes[row.guild_id]"
            class="mt-3 rounded-lg border p-3"
            :style="{
              borderColor: TONE_COLOUR[scopeOutcomes[row.guild_id]!.tone],
              background: 'var(--surface)',
            }"
          >
            <p class="text-sm font-semibold">
              {{
                $t(
                  scopeOutcomes[row.guild_id]!.headline.key,
                  scopeOutcomes[row.guild_id]!.headline.values ?? {},
                )
              }}
            </p>
            <p
              v-for="line in scopeOutcomes[row.guild_id]!.detail"
              :key="line.key"
              class="mt-1 text-sm"
              :style="{ color: 'var(--text-muted)' }"
            >
              {{ $t(line.key, line.values ?? {}) }}
            </p>
          </div>

          <div class="mt-3 flex flex-wrap items-center gap-2">
            <button
              v-if="withdrawability(row).may"
              type="button"
              class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface)] disabled:opacity-40"
              :style="{ borderColor: 'var(--danger)', color: 'var(--danger)' }"
              :disabled="busy[row.guild_id]"
              @click="confirming = row.guild_id"
            >
              {{
                busy[row.guild_id]
                  ? $t('settings.consent.withdraw.busy')
                  : $t('settings.consent.withdraw.button')
              }}
            </button>
            <!-- No button on a consent already withdrawn. The API answers
                 409, and an interface that offers an action it knows will
                 fail is worse than one that explains why it cannot. -->
            <span v-else-if="withdrawBlocked(row)" class="text-xs" :style="{ color: 'var(--text-muted)' }">
              {{ $t(withdrawBlocked(row)!.key, withdrawBlocked(row)!.values ?? {}) }}
            </span>
          </div>

          <div
            v-if="confirming === row.guild_id"
            class="mt-3 rounded-lg border p-3"
            :style="{ borderColor: 'var(--danger)', background: 'var(--surface)' }"
          >
            <p class="mb-2 text-sm font-semibold">
              {{ $t(withdrawConfirmation(row).titleKey) }}
            </p>
            <!-- Kept as separate sentences. One paragraph carrying all of
                 them is a paragraph that gets skimmed, exactly where the
                 reader most needs to notice that the Discord role and the
                 recordings are not part of this. -->
            <p
              v-for="line in withdrawConfirmation(row).consequences"
              :key="line.key"
              class="mb-2 text-sm"
              :style="{ color: 'var(--text-muted)' }"
            >
              {{ $t(line.key, line.values ?? {}) }}
            </p>
            <div class="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                class="rounded-lg px-3 py-1.5 text-sm font-medium transition-opacity hover:opacity-90"
                :style="{ background: 'var(--danger)', color: 'var(--danger-contrast)' }"
                @click="withdraw(row)"
              >
                {{ $t(withdrawConfirmation(row).confirmKey) }}
              </button>
              <button
                type="button"
                class="rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)]"
                :style="{ borderColor: 'var(--border)' }"
                @click="confirming = null"
              >
                {{ $t('settings.consent.withdraw.cancel') }}
              </button>
            </div>
          </div>

          <div
            v-if="withdrawOutcomes[row.guild_id]"
            class="mt-3 rounded-lg border p-3"
            :style="{
              borderColor: TONE_COLOUR[withdrawOutcomes[row.guild_id]!.tone],
              background: 'var(--surface)',
            }"
          >
            <p class="text-sm font-semibold">
              {{
                $t(
                  withdrawOutcomes[row.guild_id]!.headline.key,
                  withdrawOutcomes[row.guild_id]!.headline.values ?? {},
                )
              }}
            </p>
            <p
              v-for="line in withdrawOutcomes[row.guild_id]!.detail"
              :key="line.key"
              class="mt-1 text-sm"
              :style="{ color: 'var(--text-muted)' }"
            >
              {{ $t(line.key, line.values ?? {}) }}
            </p>
          </div>

          <p
            v-if="failures[row.guild_id]"
            class="mt-3 rounded-lg border p-3 text-sm"
            :style="{ borderColor: 'var(--danger)' }"
          >
            {{
              $t(failures[row.guild_id]!.key, failures[row.guild_id]!.values ?? {})
            }}
          </p>
        </article>
      </template>
    </section>

    <section
      class="rounded-xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="text-base font-semibold">{{ $t('settings.security.title') }}</h2>
      <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('settings.security.note') }}
      </p>
      <ul class="flex flex-col gap-2">
        <li
          v-for="item in UNAVAILABLE_ITEMS"
          :key="item.id"
          class="flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm"
          :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
        >
          <span>{{ $t(item.labelKey) }}</span>
          <span
            class="shrink-0 rounded-full border px-2 py-0.5 text-[0.65rem] uppercase tracking-wide"
            :style="{ borderColor: 'var(--control-border)' }"
          >{{ $t(item.noteKey ?? COMING_SOON_KEY) }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>
