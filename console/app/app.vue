<script setup lang="ts">
/**
 * The root, whose only job beyond the layout is to say what language this
 * document is in.
 *
 * `<html lang>` is not decoration. It is what tells a screen reader which
 * voice to read the page in -- a German sentence read by an English
 * synthesiser is not accented, it is unintelligible -- and what tells a
 * browser which dictionary to hyphenate and spell-check against. The
 * console had no `lang` at all before it had a second language, which was
 * survivable while every word was English and is not now.
 *
 * The region-qualified tag rather than the bare code: `de-DE` and `en-GB`
 * are what the locales are declared as in `nuxt.config.ts`, and `en-GB` is
 * a claim this console can actually make good on -- it is written in
 * British English throughout.
 */
const { locale, locales } = useI18n()

const language = computed(
  () => locales.value.find((entry) => entry.code === locale.value)?.language ?? locale.value,
)

useHead({ htmlAttrs: { lang: language } })
</script>

<template>
  <!-- The bar that says a navigation is still happening.
       `throttle` is Nuxt's default 200 ms and is left alone deliberately: a
       bar that appears for every navigation, including the ones that finish
       in forty milliseconds, is a flicker at the top of the window rather
       than information. It appears when a wait becomes a wait.

       Coloured from `--action` rather than Nuxt's own green-to-blue
       gradient, so the console has one colour that means "a control, or the
       thing a control is doing" in both themes.

       The `!` is load-bearing: the component writes its transition into an
       inline `style`, and only an important declaration can overrule that.
       Under `prefers-reduced-motion` the bar still reports progress -- it
       jumps to each figure instead of gliding to it. -->
  <NuxtLoadingIndicator color="var(--action)" class="motion-reduce:transition-none!" />
  <NuxtLayout>
    <NuxtPage />
  </NuxtLayout>
</template>
