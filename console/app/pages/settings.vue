<script setup lang="ts">
/**
 * The page that belongs to the person reading it.
 *
 * `/settings` used to be a permanent redirect to the bot's configuration --
 * the console's *administrative* settings, which is what "settings" meant
 * back when a person could decide nothing about themselves here. That
 * redirect is gone and this address is a page again, which is the one
 * breaking change in this pull request.
 *
 * Two settings, and both of them are a person's rather than a server's:
 * which theme the console is in, and which language it speaks. Each is kept
 * in a cookie, and each has exactly one writer -- `useThemePreference` for
 * the theme, `setLocale` for the language, by way of `useLocalePreference`.
 * A page that wrote either cookie itself would be a second writer of one
 * value, and the two would eventually disagree about the path or the expiry.
 *
 * **There is no Consent section here yet, and no empty box promising one.**
 * A person's own consent belongs on this page -- it is the thing they are
 * most likely to have come for -- but the endpoint it needs is being built
 * in a different pull request. A placeholder that cannot say anything true
 * about somebody's consent would be worse than its absence: it would invite
 * a reader to conclude they have none.
 *
 * The Security section says the opposite thing, and can, because it is
 * honest: those two features are not built, the rows say exactly that, and
 * they are the same two rows the account menu shows -- read from the same
 * list, so the two cannot drift into promising different things.
 */
import { COMING_SOON_KEY, UNAVAILABLE_ITEMS } from '~/utils/profileMenu'
import { themeLabelKey } from '~/utils/theme'

const { t } = useI18n()
useHead({ title: t('settings.title') })

// Destructured so the template reads `currentTheme` rather than
// `theme.current.value`: a ref returned at the top level of `setup` is
// unwrapped in the template, one nested inside an object is not.
const { available: themes, current: currentTheme, choose: chooseTheme } = useThemePreference()
const {
  available: languages,
  current: currentLanguage,
  choose: chooseLanguage,
} = useLocalePreference()

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
  <div class="max-w-3xl">
    <h1 class="mb-1 text-2xl font-semibold">{{ $t('settings.title') }}</h1>
    <p class="mb-6 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('settings.intro') }}
    </p>

    <!-- `fieldset` and `legend` rather than a heading over a row of buttons:
         a group of mutually exclusive choices is what a radio group is, and
         the native one comes with the arrow keys, the grouping a screen
         reader announces, and the "one of these is chosen" that a row of
         toggle buttons has to reimplement badly. The input is visually
         hidden and its label is what is painted, so the focus ring is put
         back on the label with `peer-focus-visible`. -->
    <fieldset
      class="mb-6 rounded-xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <legend class="px-1 text-base font-semibold">{{ $t('settings.appearance.title') }}</legend>
      <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('settings.appearance.note') }}
      </p>
      <div class="flex flex-wrap gap-2">
        <label
          v-for="choice in themes"
          :key="choice"
          class="cursor-pointer"
        >
          <input
            class="peer sr-only"
            type="radio"
            name="theme"
            :value="choice"
            :checked="currentTheme === choice"
            @change="chooseTheme(choice)"
          >
          <span
            class="block rounded-lg border px-3 py-2 text-sm transition-colors peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2"
            :style="chipStyle(currentTheme === choice)"
          >{{ $t(themeLabelKey(choice)) }}</span>
        </label>
      </div>
    </fieldset>

    <fieldset
      class="mb-6 rounded-xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <legend class="px-1 text-base font-semibold">{{ $t('settings.language.title') }}</legend>
      <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('settings.language.note') }}
      </p>
      <div class="flex flex-wrap gap-2">
        <label
          v-for="locale in languages"
          :key="locale.code"
          class="cursor-pointer"
        >
          <input
            class="peer sr-only"
            type="radio"
            name="locale"
            :value="locale.code"
            :checked="currentLanguage === locale.code"
            @change="chooseLanguage(locale.code)"
          >
          <!-- The language's own name, never a translation of it: somebody
               looking for German is looking for "Deutsch". -->
          <span
            class="block rounded-lg border px-3 py-2 text-sm transition-colors peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2"
            :style="chipStyle(currentLanguage === locale.code)"
          >{{ locale.name }}</span>
        </label>
      </div>
    </fieldset>

    <section
      class="rounded-xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="text-base font-semibold">{{ $t('settings.security.title') }}</h2>
      <p class="mb-3 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('settings.security.note') }}
      </p>
      <ul class="flex flex-col gap-2">
        <li
          v-for="item in UNAVAILABLE_ITEMS"
          :key="item.id"
          class="flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2 text-sm"
          :style="{ borderColor: 'var(--border)', color: 'var(--text-muted)' }"
        >
          <span>{{ $t(item.labelKey) }}</span>
          <span
            class="shrink-0 rounded-full border px-2 py-0.5 text-[0.65rem] uppercase tracking-wide"
            :style="{ borderColor: 'var(--control-border)' }"
          >{{ $t(item.noteKey ?? COMING_SOON_KEY) }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>
