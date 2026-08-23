<script setup lang="ts">
/**
 * One server's consent, and the controls that change it.
 *
 * This is the block `/settings` grew, lifted out of that page unchanged so
 * that the dashboard can show the same thing without a second copy of it.
 * That is not tidiness. **Consent is the one area of this console where a
 * second implementation is not an acceptable trade**: a second write path is
 * a second set of consent bugs, and the person who finds one is the person
 * who believed they had said no.
 *
 * So there is exactly one component that writes `PUT
 * /me/consents/{guild}/scope` and `POST /me/consents/{guild}/revoke`, and
 * both pages mount it. Every decision it renders -- which sentence a state
 * produces, whether the video option exists at all, what a refusal reads
 * like -- is still in `~/utils/myConsents` and still tested without
 * rendering anything. What is here is layout, request plumbing and this
 * card's own state.
 *
 * `confirming` is a prop rather than local state on purpose. Only one
 * withdrawal confirmation may be open across a whole page: two open panels
 * with the same red button on each is how the wrong one is pressed, and that
 * invariant belongs to whoever renders the list rather than to one card in
 * it.
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
  mayChangeScope,
  parseMyRevokeResult,
  parseScopeResult,
  scopeChoices,
  scopeLabelKey,
  scopeOutcome,
  withdrawConfirmation,
  withdrawOutcome,
  withdrawability,
} from '~/utils/myConsents'

const props = defineProps<{
  row: MyConsent
  /** The guild whose withdrawal confirmation is open anywhere on the page,
   *  or null. */
  confirming: string | null
}>()

const emit = defineEmits<{
  'update:confirming': [guildId: string | null]
  /** Raised after every write, successful or refused. A refusal always
   *  means the record on screen was already out of date, so the list is
   *  reloaded rather than left showing a state that was wrong when it was
   *  clicked. */
  'changed': []
}>()

const api = useApi()

const busy = ref(false)
const scopeResult = ref<Outcome | null>(null)
const withdrawResult = ref<Outcome | null>(null)
const failure = ref<Line | null>(null)

const open = computed(() => props.confirming === props.row.guild_id)

function clear() {
  failure.value = null
  scopeResult.value = null
  withdrawResult.value = null
}

/**
 * Narrow or widen what may be recorded.
 *
 * Narrowing takes effect immediately and needs nothing. Widening inserts a
 * new consent record carrying the guild's current policy version, which is
 * the API's business and not this component's -- it asks for a scope and
 * reports what it is told, and never computes a policy version of its own.
 */
async function chooseScope(scope: ConsentScope) {
  if (scope === props.row.scope || busy.value) return
  clear()
  busy.value = true
  try {
    const answer = await api<unknown>(`/me/consents/${props.row.guild_id}/scope`, {
      method: 'PUT',
      body: { scope },
    })
    // The endpoint's own answer decides what is reported, never the fact
    // that the call did not throw.
    scopeResult.value = scopeOutcome(parseScopeResult(answer))
  }
  catch (error) {
    failure.value = describeMyConsentError(error)
  }
  finally {
    busy.value = false
    emit('changed')
  }
}

async function withdraw() {
  emit('update:confirming', null)
  clear()
  busy.value = true
  try {
    const answer = await api<unknown>(`/me/consents/${props.row.guild_id}/revoke`, {
      method: 'POST',
    })
    withdrawResult.value = withdrawOutcome(parseMyRevokeResult(answer))
  }
  catch (error) {
    failure.value = describeMyConsentError(error)
  }
  finally {
    busy.value = false
    emit('changed')
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
  <article
    class="rounded-lg border p-3"
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

    <!-- Sentences rather than a table of raw fields: what is being recorded
         of somebody right now, under which policy version, since when, and
         — for a scheduled stop — that it is coming and when. A person whose
         consent runs out on Friday should see that on Tuesday. -->
    <p
      v-for="line in consentNarrative(row)"
      :key="line.key"
      class="mb-2 text-sm"
      :style="{ color: 'var(--text-muted)' }"
    >
      {{ $t(line.key, line.values ?? {}) }}
    </p>

    <!-- `fieldset` and `legend` rather than a heading over a row of buttons:
         a group of mutually exclusive choices is what a radio group is, and
         the native one comes with the arrow keys and the grouping a screen
         reader announces. -->
    <fieldset v-if="mayChangeScope(row)" class="mt-3">
      <legend
        class="text-xs font-medium tracking-wide uppercase"
        :style="{ color: 'var(--text-muted)' }"
      >
        {{ $t('settings.consent.scope.title') }}
      </legend>
      <div class="mt-2 flex flex-wrap gap-2">
        <!-- **When the guild does not offer video consent there is no video
             option in this list at all.** Not disabled, not greyed, not
             behind a tooltip: a consent record naming video under a policy
             that describes only audio is not consent, and this interface
             must not offer what it cannot honour. -->
        <label v-for="choice in scopeChoices(row)" :key="choice" class="cursor-pointer">
          <input
            class="peer sr-only"
            type="radio"
            :name="`scope-${row.guild_id}`"
            :value="choice"
            :checked="row.scope === choice"
            :disabled="busy"
            @change="chooseScope(choice)"
          >
          <span
            class="block rounded-lg border px-3 py-2 text-sm transition-colors peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-disabled:opacity-40 motion-reduce:transition-none"
            :style="chipStyle(row.scope === choice)"
          >{{ $t(scopeLabelKey(choice)) }}</span>
        </label>
      </div>
      <!-- One sentence in place of the option, so its absence reads as a
           fact about this server rather than as a control that failed to
           render. -->
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
      v-if="scopeResult"
      class="mt-3 rounded-lg border p-3"
      :style="{ borderColor: TONE_COLOUR[scopeResult.tone], background: 'var(--surface)' }"
    >
      <p class="text-sm font-semibold">
        {{ $t(scopeResult.headline.key, scopeResult.headline.values ?? {}) }}
      </p>
      <p
        v-for="line in scopeResult.detail"
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
        class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface)] disabled:opacity-40 motion-reduce:transition-none"
        :style="{ borderColor: 'var(--danger)', color: 'var(--danger)' }"
        :disabled="busy"
        @click="emit('update:confirming', row.guild_id)"
      >
        {{
          busy ? $t('settings.consent.withdraw.busy') : $t('settings.consent.withdraw.button')
        }}
      </button>
      <!-- No button on a consent already withdrawn. The API answers 409, and
           an interface that offers an action it knows will fail is worse
           than one that explains why it cannot. -->
      <span
        v-else-if="withdrawBlocked(row)"
        class="text-xs"
        :style="{ color: 'var(--text-muted)' }"
      >
        {{ $t(withdrawBlocked(row)!.key, withdrawBlocked(row)!.values ?? {}) }}
      </span>
    </div>

    <div
      v-if="open"
      class="mt-3 rounded-lg border p-3"
      :style="{ borderColor: 'var(--danger)', background: 'var(--surface)' }"
    >
      <p class="mb-2 text-sm font-semibold">
        {{ $t(withdrawConfirmation(row).titleKey) }}
      </p>
      <!-- Kept as separate sentences. One paragraph carrying all of them is
           a paragraph that gets skimmed, exactly where the reader most needs
           to notice that the Discord role and the recordings are not part of
           this. -->
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
          class="rounded-lg px-3 py-1.5 text-sm font-medium transition-opacity hover:opacity-90 motion-reduce:transition-none"
          :style="{ background: 'var(--danger)', color: 'var(--danger-contrast)' }"
          @click="withdraw()"
        >
          {{ $t(withdrawConfirmation(row).confirmKey) }}
        </button>
        <button
          type="button"
          class="rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)] motion-reduce:transition-none"
          :style="{ borderColor: 'var(--border)' }"
          @click="emit('update:confirming', null)"
        >
          {{ $t('settings.consent.withdraw.cancel') }}
        </button>
      </div>
    </div>

    <div
      v-if="withdrawResult"
      class="mt-3 rounded-lg border p-3"
      :style="{ borderColor: TONE_COLOUR[withdrawResult.tone], background: 'var(--surface)' }"
    >
      <p class="text-sm font-semibold">
        {{ $t(withdrawResult.headline.key, withdrawResult.headline.values ?? {}) }}
      </p>
      <p
        v-for="line in withdrawResult.detail"
        :key="line.key"
        class="mt-1 text-sm"
        :style="{ color: 'var(--text-muted)' }"
      >
        {{ $t(line.key, line.values ?? {}) }}
      </p>
    </div>

    <p
      v-if="failure"
      class="mt-3 rounded-lg border p-3 text-sm"
      :style="{ borderColor: 'var(--danger)' }"
    >
      {{ $t(failure.key, failure.values ?? {}) }}
    </p>
  </article>
</template>
