<script setup lang="ts">
/**
 * The link a guild hands out, and the one page that must tell nobody
 * anything.
 *
 * `domain/oauth_clients.py` has named this address since #147 — "the
 * console publishes a guild's link at `/g/{slug}/sign-in`" — and nothing
 * has ever served it. Without it the only link an administrator could
 * distribute was `/api/auth/login?guild=acme`, which is a redirect with no
 * page: somebody following it while the registration is half-finished meets
 * a raw JSON body, and somebody following it while it is finished never
 * sees this deployment at all.
 *
 * **This page performs no lookup, and that is its entire security
 * property.** It does not ask whether the slug is registered, whether the
 * guild has a secret stored, or whether the name is spelled like a slug at
 * all. It cannot: `/api/auth/login?guild=…` answers the same 404 with the
 * same body to a name nobody holds, a name that is not a name, a guild
 * whose secret was never supplied, a guild registered against a provider
 * this deployment cannot exchange with, and a guild whose secret is wrapped
 * by a master key this process no longer holds — and it does that so that
 * an attacker walking a list of organisation names cannot tell "no such
 * organisation here" from "one, half-configured". A page that rendered
 * differently for a name it recognised would put that oracle back, in
 * HTML, in front of anyone with a browser and no session at all.
 *
 * So every slug gets this page, byte for byte, and the answer arrives from
 * the API when the button is pressed. The slug is echoed because it came
 * out of the address bar and the reader already has it; nothing else about
 * the guild is on screen, because nothing else is knowable from here
 * without asking a question this deployment refuses to answer.
 *
 * The button is a plain anchor rather than a fetch, for the reason
 * `pages/sign-in.vue` gives for its own: the OAuth flow is a browser
 * navigation to another origin and back, and an XHR cannot follow it.
 */
import { loginUrl } from '~/utils/oauthClient'

definePageMeta({ layout: 'anonymous' })

const { t } = useI18n()
const route = useRoute()

/** Whatever is in the address bar, unexamined. `[slug]` matches one
 *  segment, so this is a string; it is never tested, trimmed or
 *  lowercased here — the endpoint behind the button is the one thing that
 *  decides, and it decides the same way for every value. */
const slug = computed(() => String(route.params.slug ?? ''))

useHead(() => ({ title: t('auth.guildSignIn') }))
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

    <!-- The name out of the address bar, and nothing else about the guild.
         Rendered as an unstyled string in a `<code>` rather than as a
         heading: it is a word this page was handed, not a fact this page
         looked up, and dressing it as an organisation's name would imply a
         lookup happened. -->
    <i18n-t
      keypath="auth.guildTagline"
      tag="p"
      class="mb-8 text-sm"
      :style="{ color: 'var(--text-muted)' }"
    >
      <template #slug>
        <code class="rounded bg-[var(--surface-raised)] px-1 break-all">{{ slug }}</code>
      </template>
    </i18n-t>

    <a
      :href="loginUrl(slug)"
      class="inline-flex w-full items-center justify-center rounded-lg px-4 py-2.5 font-medium text-white transition-opacity hover:opacity-90"
      :style="{
        background: 'linear-gradient(120deg, var(--color-brand-blue), var(--color-brand-magenta))',
      }"
    >
      {{ $t('auth.signInWithOutline') }}
    </a>

    <!-- What happens when this link does not work, said in advance and
         without saying which of the several reasons applies -- because this
         deployment does not know from here and would not answer if it did.
         The remedy is the same for all of them and it is not on this
         screen: ask whoever sent the link. -->
    <p class="mt-4 text-xs" :style="{ color: 'var(--text-muted)' }">
      {{ $t('auth.guildUnknownNote') }}
    </p>

    <!-- The way back to the deployment's own sign-in, for somebody who
         followed a link they were not the intended reader of. -->
    <NuxtLink
      to="/sign-in"
      class="mt-4 inline-block text-xs font-medium transition-colors hover:underline"
      :style="{ color: 'var(--action)' }"
    >
      {{ $t('auth.guildUseCentral') }}
    </NuxtLink>
  </div>
</template>
