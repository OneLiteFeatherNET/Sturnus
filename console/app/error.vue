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
 *
 * **This is the one page that does not simply call `$t`.** Every other
 * page in the console can assume its locale file loaded, because a page
 * whose assets did not load is a page nobody is looking at -- they are
 * looking at this one. Locale messages are lazily fetched, so the failure
 * that brings somebody here can be the same failure that leaves this page
 * with no German and no English: `$t('error.unreachableHeading')` would
 * then render the string `error.unreachableHeading` at somebody who is
 * already confused. So each sentence carries its English text here in the
 * source and treats the translation as an improvement on it. See
 * `~/utils/i18nFallback`.
 */
import type { NuxtError } from '#app'
import { translateOr, type Translate } from '~/utils/i18nFallback'

const props = defineProps<{ error: NuxtError }>()

// Guarded, because `useI18n` throws rather than returns null when the i18n
// plugin is not installed at all -- and "the plugin did not install" is
// precisely one of the disasters this page renders for.
let translate: Translate | null = null
try {
  translate = useI18n().t as Translate
} catch {
  translate = null
}

// `ApiError` sets status 0 when the request never got a response: a
// network failure, a DNS failure, a tunnel that is down. Distinguishable
// from every real status, which is the point -- "could not reach the API"
// and "the API said no" need different words.
const unreachable = computed(() => String(props.error?.message ?? '').includes('status 0'))
const notFound = computed(() => props.error?.statusCode === 404)

const heading = computed(() => {
  if (notFound.value) {
    return translateOr(translate, 'error.notFoundHeading', 'That page does not exist')
  }
  if (unreachable.value) {
    return translateOr(translate, 'error.unreachableHeading', 'Sturnus is not answering')
  }
  return translateOr(translate, 'error.genericHeading', 'Something went wrong')
})

const detail = computed(() => {
  if (notFound.value) {
    return translateOr(
      translate,
      'error.notFoundDetail',
      'The address is wrong, or the page has moved.',
    )
  }
  if (unreachable.value) {
    return translateOr(
      translate,
      'error.unreachableDetail',
      'The console reached the browser but could not reach the service behind it. '
      + 'This is not a sign-in problem — signing in again will not help. It usually means '
      + 'the service is restarting, and usually resolves within a minute.',
    )
  }
  return translateOr(
    translate,
    'error.genericDetail',
    'The console could not load this page. Trying again is worth one attempt; '
    + 'if it keeps happening, the service is likely unwell rather than the page.',
  )
})

const retry = computed(() => translateOr(translate, 'error.retry', 'Try again'))
const goToSignIn = computed(() => translateOr(translate, 'error.goToSignIn', 'Go to sign-in'))
const status = computed(() =>
  translateOr(translate, 'error.status', `Status ${props.error?.statusCode}`, {
    code: props.error?.statusCode,
  }),
)

// The tab says which failure this is, rather than always saying the generic
// one: somebody with four tabs open looking for the broken one should not
// have to click through four identical titles.
useHead({ title: heading })
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
          {{ retry }}
        </button>
        <!-- Offered second, and only as a link: on an unreachable API this
             will not help, and a page that leads with it would be telling
             somebody to fix a problem that is not theirs. -->
        <NuxtLink
          to="/sign-in"
          class="rounded-lg px-4 py-2 text-sm transition-colors hover:bg-[var(--surface-raised)]"
          :style="{ color: 'var(--text-muted)' }"
        >
          {{ goToSignIn }}
        </NuxtLink>
      </div>

      <p v-if="error?.statusCode" class="mt-6 text-xs" :style="{ color: 'var(--text-muted)' }">
        {{ status }}
      </p>
    </div>
  </div>
</template>
