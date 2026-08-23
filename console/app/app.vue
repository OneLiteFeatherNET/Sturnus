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
  <NuxtLayout>
    <NuxtPage />
  </NuxtLayout>
</template>
