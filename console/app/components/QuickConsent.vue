<script setup lang="ts">
/**
 * Consent, on the page a person lands on.
 *
 * §7.3 of the personalisation spec: consent is the thing a participant is
 * most likely to have come to do, and making them find it two clicks deep is
 * the current design's mistake repeated in a new place. So the dashboard
 * shows the same records `/settings` shows, read through the same composable
 * and rendered by the same `ConsentCard` -- there is exactly one component
 * in this console that writes a consent, and this half of the band mounts it
 * rather than reimplementing it.
 *
 * Which of the three shapes to take is `~/utils/quickSettings`' decision
 * rather than a chain of `v-if`s here, and the one worth naming is
 * `silent`: **somebody with no consent record anywhere gets nothing.** Not
 * an empty box, not "you have consented nowhere". `/settings` does say that,
 * and should -- somebody who opened a page called Consent asked the
 * question. Nobody asked it by opening the dashboard.
 *
 * A failure is the opposite case and is never silent. `GET /api/me/consents`
 * answers 404 until the API that serves it is deployed, and this console
 * ships as a separate image; an empty list drawn in its place would be a
 * false statement about somebody's own data.
 *
 * The records arrive as a prop rather than being fetched here, because the
 * band above has to know how many there are before it can decide whether it
 * exists at all.
 */
import { type MyConsent, describeMyConsentError, isConsentServiceMissing } from '~/utils/myConsents'
import { OWN_SETTINGS_PATH, consentBand } from '~/utils/quickSettings'

const props = defineProps<{
  records: readonly MyConsent[]
  /** Whatever `/me/consents` threw, or null. */
  failure: unknown
}>()

const emit = defineEmits<{ changed: [] }>()

const band = computed(() =>
  consentBand({ failed: Boolean(props.failure), records: props.records.length }),
)
/** Whether the endpoint simply is not deployed, which reads differently from
 *  a server that answered and failed. */
const serviceMissing = computed(() => isConsentServiceMissing(props.failure))

/** One withdrawal confirmation open at a time, across the whole band. Two
 *  open panels with the same red button on each is how the wrong one is
 *  pressed. */
const confirming = ref<string | null>(null)
</script>

<template>
  <!-- Never an empty list. See the component comment. -->
  <section
    v-if="band === 'unavailable'"
    class="rounded-xl border p-4"
    :style="{
      borderColor: serviceMissing ? 'var(--border)' : 'var(--danger)',
      background: 'var(--surface)',
    }"
  >
    <h2 class="text-base font-semibold">
      {{
        serviceMissing
          ? $t('settings.consent.unavailableHeading')
          : $t('settings.consent.errorHeading')
      }}
    </h2>
    <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t(describeMyConsentError(failure).key, describeMyConsentError(failure).values ?? {}) }}
    </p>
  </section>

  <section v-else-if="band === 'records'">
    <div class="mb-1 flex flex-wrap items-baseline justify-between gap-2">
      <h2 class="text-base font-semibold">{{ $t('dashboard.quick.consent.title') }}</h2>
      <NuxtLink
        :to="OWN_SETTINGS_PATH"
        class="text-sm underline underline-offset-2"
        :style="{ color: 'var(--action)' }"
      >
        {{ $t('dashboard.quick.consent.more') }}
      </NuxtLink>
    </div>
    <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('dashboard.quick.consent.intro') }}
    </p>
    <div class="flex flex-col gap-4">
      <ConsentCard
        v-for="row in records"
        :key="row.guild_id"
        v-model:confirming="confirming"
        :row="row"
        @changed="emit('changed')"
      />
    </div>
  </section>

  <!-- `silent` renders nothing at all, deliberately. -->
</template>
