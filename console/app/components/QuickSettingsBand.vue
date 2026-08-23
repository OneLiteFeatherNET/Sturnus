<script setup lang="ts">
/**
 * The band of controls the dashboard grows, chosen by what the reader is.
 *
 * Everyone gets their consent and the one control that changes it.
 * Administrators additionally get the handful of settings a guild changes
 * often. Somebody who is neither gets **nothing at all** -- no heading, no
 * separator, no space where the band would have been -- which is what
 * `bandIsEmpty` decides and what `test/quickSettings.spec.ts` pins.
 *
 * Both reads happen here rather than in the halves below, for one reason:
 * the band cannot know whether it exists until it knows how many consent
 * records and how many administered guilds there are. A half that fetched
 * for itself could only report that after it had already rendered a heading.
 *
 * Both are `lazy`. The dashboard's figures are what somebody came to read,
 * they render first, and the band appears underneath them when it is ready.
 * That ordering is not cosmetic: a band of unknowable height above the
 * figures would move them after the page had settled, and for a participant
 * with no consent anywhere it would move them and then vanish.
 */
import { parseGuilds } from '~/utils/settings'
import { bandIsEmpty, dashboardBand } from '~/utils/quickSettings'

const api = useApi()
const session = useSession()

const {
  data: consentData,
  error: consentError,
  status: consentStatus,
  refresh: refreshConsents,
} = useMyConsentRecords({ lazy: true })

/**
 * The guilds this person administers.
 *
 * Asked for only when `/api/me` already said they administer something
 * somewhere -- the same flag the sidebar uses to offer the admin section --
 * so a participant's dashboard does not make a request whose answer is known
 * to be `[]`. An empty list from the endpoint is a real answer too, and the
 * administrator half is then simply absent rather than empty.
 */
const {
  data: guildData,
  status: guildStatus,
} = useAsyncData(
  'quick-guilds',
  async () => (session.value?.is_admin ? parseGuilds(await api('/guilds')) : []),
  { lazy: true, watch: [session] },
)

const records = computed(() => consentData.value ?? [])
const guilds = computed(() => guildData.value ?? [])

const settled = (status: string) => status !== 'pending' && status !== 'idle'
const loading = computed(() => !settled(consentStatus.value) || !settled(guildStatus.value))

const band = computed(() =>
  dashboardBand({
    consentFailed: Boolean(consentError.value),
    consentRecords: records.value.length,
    administeredGuilds: guilds.value.length,
  }),
)

/** A failure reading `/api/guilds` is deliberately not rendered here.
 *  Somebody who cannot be told which servers they administer is told that on
 *  the page that configures them; on the dashboard it would be a red box
 *  about a section they may not even have. */
</script>

<template>
  <!-- A skeleton of the shape that is coming rather than a spinner. It sits
       below the figures, so nothing above it moves when the band lands. -->
  <div v-if="loading" class="mt-10" aria-busy="true">
    <p class="sr-only">{{ $t('dashboard.quick.loading') }}</p>
    <div
      class="mb-3 h-5 w-40 animate-pulse rounded motion-reduce:animate-none"
      :style="{ background: 'var(--surface-raised)' }"
    />
    <div
      class="h-32 animate-pulse rounded-xl border motion-reduce:animate-none"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    />
  </div>

  <div v-else-if="!bandIsEmpty(band)" class="mt-10 flex flex-col gap-10">
    <QuickConsent :records="records" :failure="consentError" @changed="refreshConsents()" />
    <QuickGuildSettings v-if="band.guildSettings" :guilds="guilds" />
  </div>

  <!-- Somebody with neither a consent record nor a server to administer gets
       no band whatsoever. Saying nothing is better than saying something
       false about their own data. -->
</template>
