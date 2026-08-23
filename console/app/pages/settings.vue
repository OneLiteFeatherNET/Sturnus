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
 * Two preferences, and both of them are a person's rather than a server's:
 * which theme the console is in, and which language it speaks. Each is kept
 * in a cookie, and each has exactly one writer -- `useThemePreference` for
 * the theme, `setLocale` for the language, by way of `useLocalePreference`.
 * A page that wrote either cookie itself would be a second writer of one
 * value, and the two would eventually disagree about the path or the expiry.
 *
 * **The Consent section is the third, and it is the one this page exists
 * for.** Theme and language are conveniences; consent is a right somebody
 * exercises. Every decision it makes -- which sentence a state produces,
 * whether the video option exists at all, what a refusal reads like -- is
 * in `~/utils/myConsents` and tested without rendering anything. One guild's
 * block, and the two writes it can make, are in `ConsentCard`, which the
 * dashboard band mounts as well: there is exactly one component in this
 * console that writes a consent, because a second write path would be a
 * second set of consent bugs. What is left here is the section around it.
 *
 * Three things this section refuses to do, all three on purpose:
 *
 * - **It never renders an absence as an answer.** `GET /api/me/consents`
 *   answers 404 until the API that serves it is deployed, and this console
 *   ships as a separate image. A 404 drawn as an empty list would say "you
 *   have consented nowhere", which is a false statement about somebody's
 *   own data and the worst thing this page could say. So the failure is
 *   named and nothing else is shown.
 * - **It never offers a video option a server has no policy for.** When
 *   `video_consent_offered` is false the option is absent -- not disabled,
 *   not greyed -- and one sentence says the server records audio only.
 * - **It never lets "withdrawn" read as "erased".** The Discord role stays,
 *   the recordings stay, and both are said before the act and again after
 *   it.
 *
 * The Security section says a fourth thing, and can, because it is honest:
 * those two features are not built, the rows say exactly that, and they are
 * the same two rows the account menu shows -- read from the same list, so
 * the two cannot drift into promising different things.
 */
import { describeMyConsentError, isConsentServiceMissing } from '~/utils/myConsents'
import { COMING_SOON_KEY, UNAVAILABLE_ITEMS } from '~/utils/profileMenu'
import { themeLabelKey } from '~/utils/theme'

const { t } = useI18n()
useHead({ title: t('settings.title') })

/**
 * The person's own consent records.
 *
 * Not `lazy`, so the first paint already carries them: this section is the
 * reason most people open this page, and a spinner where a fact belongs is a
 * page that makes somebody wait to find out whether they are being recorded.
 * The dashboard reads the same records through the same composable and does
 * ask for `lazy`, because there the band is not what held the page up.
 */
const {
  data: consentData,
  error: consentError,
  status: consentStatus,
  refresh: refreshConsents,
} = await useMyConsentRecords()

const consents = computed(() => consentData.value ?? [])
/** The one failure with a shape of its own. See the module comment. */
const serviceMissing = computed(() => isConsentServiceMissing(consentError.value))

/**
 * The guild whose withdrawal confirmation is open. One at a time: two open
 * panels with the same red button on each is how the wrong one is pressed.
 *
 * It stays here rather than inside `ConsentCard` precisely because it is a
 * property of the list and not of a card -- a card holding its own flag
 * could not know that another one is already open.
 */
const confirming = ref<string | null>(null)

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
      class="mb-6 rounded-xl border p-4"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
    >
      <h2 class="text-base font-semibold">{{ $t('settings.consent.title') }}</h2>
      <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
        {{ $t('settings.consent.intro') }}
      </p>

      <p
        v-if="consentStatus === 'pending'"
        class="mt-3 text-sm"
        :style="{ color: 'var(--text-muted)' }"
      >
        {{ $t('settings.consent.loading') }}
      </p>

      <!-- **A 404 shows this and nothing else.** The API that serves these
           records may not be deployed yet, and an empty list drawn in its
           place would tell somebody they have consented nowhere — a false
           statement about their own data, and the one failure on this page
           that is worth a shape of its own. -->
      <div
        v-else-if="consentError"
        class="mt-3 rounded-lg border p-3"
        :style="{
          borderColor: serviceMissing ? 'var(--border)' : 'var(--danger)',
          background: 'var(--surface-raised)',
        }"
      >
        <p class="text-sm font-semibold">
          {{
            serviceMissing
              ? $t('settings.consent.unavailableHeading')
              : $t('settings.consent.errorHeading')
          }}
        </p>
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{
            $t(
              describeMyConsentError(consentError).key,
              describeMyConsentError(consentError).values ?? {},
            )
          }}
        </p>
      </div>

      <!-- Not a blank space. Somebody with no consent anywhere is being
           recorded nowhere, which is a fact worth stating, and the way to
           change it is in Discord rather than here. -->
      <div
        v-else-if="consents.length === 0"
        class="mt-3 rounded-lg border p-3"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface-raised)' }"
      >
        <p class="text-sm font-semibold">{{ $t('settings.consent.emptyHeading') }}</p>
        <p class="mt-1 text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ $t('settings.consent.emptyNote') }}
        </p>
      </div>

      <template v-else>
        <p class="mt-1 text-xs" :style="{ color: 'var(--text-muted)' }">
          {{ $t('settings.consent.serverIdNote') }}
        </p>

        <div class="mt-4 flex flex-col gap-4">
          <!-- The one component in this console that writes a consent. The
               dashboard band mounts the same one, so there is a single
               implementation of narrowing, widening and withdrawing rather
               than two that can drift. `confirming` is held here because
               only one confirmation may be open across the whole list. -->
          <ConsentCard
            v-for="row in consents"
            :key="row.guild_id"
            v-model:confirming="confirming"
            :row="row"
            @changed="refreshConsents()"
          />
        </div>
      </template>
    </section>

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
