<script setup lang="ts">
/**
 * The one page that exists before a session does.
 *
 * The sign-in link is a plain anchor rather than a fetch: the OAuth flow is
 * a browser navigation to another origin and back, and an XHR cannot follow
 * it.
 */
definePageMeta({ layout: 'anonymous' })

const { t } = useI18n()
useHead({ title: () => t('auth.signIn') })

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
    <h1 class="mb-2 text-xl font-semibold">{{ $t('common.brand') }}</h1>
    <p class="mb-8 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('auth.tagline') }}
    </p>

    <div
      v-if="notLinked"
      class="mb-6 rounded-lg border p-4 text-left text-sm"
      :style="{ borderColor: 'var(--color-brand-yellow)' }"
    >
      <p class="mb-1 font-medium">{{ $t('auth.notLinkedHeading') }}</p>
      <!-- `i18n-t` rather than three concatenated fragments: the command has
           to sit inside a sentence, and where in the sentence it sits is a
           property of the language. German puts it second and English puts
           it second only by coincidence -- splitting the sentence around the
           `<code>` would have hard-coded the English word order into every
           translation of it. -->
      <i18n-t
        keypath="auth.notLinkedBody"
        tag="p"
        :style="{ color: 'var(--text-muted)' }"
      >
        <template #command>
          <code class="rounded bg-[var(--surface-raised)] px-1">/link</code>
        </template>
      </i18n-t>
    </div>

    <a
      href="/api/auth/login"
      class="inline-flex w-full items-center justify-center rounded-lg px-4 py-2.5 font-medium text-white transition-opacity hover:opacity-90"
      :style="{
        background: 'linear-gradient(120deg, var(--color-brand-blue), var(--color-brand-magenta))',
      }"
    >
      {{ $t('auth.signInWithOutline') }}
    </a>
  </div>
</template>
