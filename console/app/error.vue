<script setup lang="ts">
/**
 * What the console shows when a page could not be rendered at all.
 *
 * Without this file Nuxt answers with raw JSON -- `{"error":true,
 * "statusCode":500,...}` -- which is a fine thing for a machine to read
 * and nothing at all for the person in front of the browser.
 *
 * The distinction it exists to draw: **an unreachable API is not a
 * sign-in problem.** `loadSession` deliberately treats only a 401 as
 * "nobody is signed in" and rethrows everything else, because swallowing
 * a 500 would bounce a signed-in person to the sign-in page, where signing
 * in works perfectly and the whole thing reads as a random logout. The
 * cost of that correctness is that a console whose API is down renders an
 * error rather than a login form -- so the error had better say which,
 * and offer the thing that might actually help.
 */
import type { NuxtError } from '#app'

const props = defineProps<{ error: NuxtError }>()

useHead({ title: 'Something went wrong' })

// `ApiError` sets status 0 when the request never got a response: a
// network failure, a DNS failure, a tunnel that is down. Distinguishable
// from every real status, which is the point -- "could not reach the API"
// and "the API said no" need different words.
const unreachable = computed(() => String(props.error?.message ?? '').includes('status 0'))
const notFound = computed(() => props.error?.statusCode === 404)

const heading = computed(() => {
  if (notFound.value) return 'That page does not exist'
  if (unreachable.value) return 'Sturnus is not answering'
  return 'Something went wrong'
})

const detail = computed(() => {
  if (notFound.value) {
    return 'The address is wrong, or the page has moved.'
  }
  if (unreachable.value) {
    return 'The console reached the browser but could not reach the service behind it. '
      + 'This is not a sign-in problem — signing in again will not help. It usually means '
      + 'the service is restarting, and usually resolves within a minute.'
  }
  return 'The console could not load this page. Trying again is worth one attempt; '
    + 'if it keeps happening, the service is likely unwell rather than the page.'
})
</script>

<template>
  <div class="flex min-h-screen items-center justify-center p-6">
    <div
      class="w-full max-w-md rounded-2xl border p-8 text-center"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <div class="mb-6 flex justify-center">
        <SturnusMark :size="48" />
      </div>
      <h1 class="mb-3 text-xl font-semibold">
        {{ heading }}
      </h1>
      <p class="mb-8 text-sm leading-relaxed" :style="{ color: 'var(--text-muted)' }">
        {{ detail }}
      </p>

      <div class="flex flex-col gap-2">
        <button
          type="button"
          class="rounded-lg px-4 py-2.5 font-medium text-white transition-opacity hover:opacity-90"
          :style="{
            background: 'linear-gradient(120deg, var(--color-brand-blue), var(--color-brand-magenta))',
          }"
          @click="clearError({ redirect: '/' })"
        >
          Try again
        </button>
        <!-- Offered second, and only as a link: on an unreachable API this
             will not help, and a page that leads with it would be telling
             somebody to fix a problem that is not theirs. -->
        <NuxtLink
          to="/sign-in"
          class="rounded-lg px-4 py-2 text-sm transition-colors hover:bg-[var(--surface-raised)]"
          :style="{ color: 'var(--text-muted)' }"
        >
          Go to sign-in
        </NuxtLink>
      </div>

      <p v-if="error?.statusCode" class="mt-6 text-xs" :style="{ color: 'var(--text-muted)' }">
        Status {{ error.statusCode }}
      </p>
    </div>
  </div>
</template>
