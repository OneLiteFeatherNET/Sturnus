<script setup lang="ts">
/**
 * The one page that exists before a session does.
 *
 * The sign-in link is a plain anchor rather than a fetch: the OAuth flow is
 * a browser navigation to another origin and back, and an XHR cannot follow
 * it.
 */
definePageMeta({ layout: 'anonymous' })
useHead({ title: 'Sign in' })

const route = useRoute()

// The API answers a failed sign-in with a specific reason, and one of them
// is not a refusal at all: somebody who authenticated but has never run
// `/link` is not being turned away, they are missing a step. Saying which
// is the difference between an instruction and a dead end.
const notLinked = computed(() => route.query.error === 'not-linked')
</script>

<template>
  <div
    class="w-full max-w-md rounded-2xl border p-8 text-center"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
  >
    <div class="mb-6 flex justify-center">
      <SturnusMark :size="56" />
    </div>
    <h1 class="mb-2 text-xl font-semibold">Sturnus</h1>
    <p class="mb-8 text-sm" :style="{ color: 'var(--text-muted)' }">
      Meeting protocols, and the recordings behind them.
    </p>

    <div
      v-if="notLinked"
      class="mb-6 rounded-lg border p-4 text-left text-sm"
      :style="{ borderColor: 'var(--color-brand-yellow)' }"
    >
      <p class="mb-1 font-medium">Your account is not linked yet.</p>
      <p :style="{ color: 'var(--text-muted)' }">
        Run <code class="rounded bg-[var(--surface-raised)] px-1">/link</code> in Discord, then
        sign in again. The console finds your recordings by your Discord account, and the link is
        what connects the two.
      </p>
    </div>

    <a
      href="/api/auth/login"
      class="inline-flex w-full items-center justify-center rounded-lg px-4 py-2.5 font-medium text-white transition-opacity hover:opacity-90"
      :style="{
        background: 'linear-gradient(120deg, var(--color-brand-blue), var(--color-brand-magenta))',
      }"
    >
      Sign in with Outline
    </a>
  </div>
</template>
