/**
 * Whether a decision made in a pure module actually reaches a reader as
 * their own language.
 *
 * The modules under `app/utils` are tested without rendering anything, and
 * that is the point of them: they return a key and its values, and a spec
 * can pin the decision without pinning the wording. What no such spec can
 * see is the other half -- that the key exists, that the values land in the
 * holes the sentence has, that a count picks the right side of a plural,
 * that a quantity is grouped the way the reader groups quantities and a
 * date is ordered the way they order dates.
 *
 * Every one of those can be wrong while every pure spec passes, and each of
 * them is invisible to whoever wrote it: a German reader sees `48,213`
 * where they expect `48.213`, or `calendar.cellRecorded` where they expect
 * a sentence. So this renders, in both languages, against the locale files
 * that actually ship.
 *
 * **The locale is pinned, and the zone is not the machine's.** Both are
 * deliberate: a test whose result depends on where it runs is a test that
 * fails in CI for a reason nobody can reproduce. Everything asserted below
 * is formatted through a datetime shape that names UTC -- see
 * `i18n/i18n.config.ts`, which explains why the one shape that does not is
 * the one thing this spec stays away from.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { mount } from '@vue/test-utils'
import { computed, defineComponent, h, ref } from 'vue'
import { createI18n, useI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useSay } from '../app/composables/useSay'
import { durationMessage } from '../app/utils/duration'
import { describeFailure, figureMoment, summaryFigures } from '../app/utils/format'
import { describeCell, buildYearGrid } from '../app/utils/heatmap'
import { NOT_MEASURED } from '../app/utils/message'
import { reportMonthRows, parseGuildReport } from '../app/utils/reporting'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

/**
 * Which language tag stands behind each locale code, read out of
 * `nuxt.config.ts` rather than restated here.
 *
 * The distinction is the whole reason `useSay` formats with a tag: the code
 * is `en`, the language is `en-GB`, and `Intl` writes `August 21, 2026` for
 * the first and `21 August 2026` for the second. A fixture that named the
 * tags itself would keep passing after somebody changed the real ones,
 * which is the failure this file exists to catch.
 */
function configuredLocales(): { code: string, language: string }[] {
  const config = readFileSync(resolve(process.cwd(), 'nuxt.config.ts'), 'utf8')
  return [...config.matchAll(/\{\s*code:\s*'([^']+)',\s*language:\s*'([^']+)'/g)].map((found) => ({
    code: found[1]!,
    language: found[2]!,
  }))
}

/**
 * `say`, as a component in this locale would have it.
 *
 * `useSay` reaches for `useI18n` the way every composable in this project
 * does -- through Nuxt's auto-import, which vitest does not provide. The
 * stub is vue-i18n's own, widened with the `locales` list that
 * `@nuxtjs/i18n` adds to the composer and that `useSay` reads the language
 * tag out of.
 */
function sayIn(locale: 'en' | 'de') {
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: { en: load('en'), de: load('de') },
    datetimeFormats: DATETIME_FORMATS,
    numberFormats: NUMBER_FORMATS,
  })

  vi.stubGlobal('computed', computed)
  vi.stubGlobal('ref', ref)
  vi.stubGlobal('useI18n', () => ({
    ...useI18n(),
    locales: computed(() => configuredLocales()),
  }))

  let say!: ReturnType<typeof useSay>
  const probe = defineComponent({
    setup() {
      say = useSay()
      return () => h('span')
    },
  })
  // Deliberately not unmounted: `say` closes over a computed, and a
  // computed whose effect scope has been disposed is a different thing to
  // read from than the one a live page holds.
  mount(probe, { global: { plugins: [i18n] } })
  return say
}

/**
 * The shapes `i18n/i18n.config.ts` declares.
 *
 * Restated here because that file is a Nuxt module wrapped in
 * `defineI18nConfig`, which vitest cannot import. What matters for these
 * assertions is that both spellings of each locale carry the table --
 * `useSay` formats with the tag, and a tag with nothing registered under it
 * falls back to the code's and quietly writes American dates.
 */
const SHAPES = {
  fullDate: { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' },
  longDate: { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' },
  monthYear: { year: 'numeric', month: 'long', timeZone: 'UTC' },
  shortMonth: { month: 'short', timeZone: 'UTC' },
  weekday: { weekday: 'long', timeZone: 'UTC' },
  weekdayShort: { weekday: 'short', timeZone: 'UTC' },
  utcMoment: {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: 'UTC',
    timeZoneName: 'short',
  },
  clock: { hour: '2-digit', minute: '2-digit', hour12: false },
} as const

const DATETIME_FORMATS = {
  'en': SHAPES,
  'en-GB': SHAPES,
  'de': SHAPES,
  'de-DE': SHAPES,
} as never

const NUMBER_FORMATS = {
  'en': { percent: { style: 'percent', maximumFractionDigits: 0 } },
  'en-GB': { percent: { style: 'percent', maximumFractionDigits: 0 } },
  'de': { percent: { style: 'percent', maximumFractionDigits: 0 } },
  'de-DE': { percent: { style: 'percent', maximumFractionDigits: 0 } },
} as never

afterEach(() => vi.unstubAllGlobals())

/** A day with something on it, for the heatmap sentence. */
const BUSY_DAY = {
  date: '2026-08-21',
  sessions: 3,
  totalDurationSeconds: 4320,
  participants: 5,
  intensity: 2,
}

describe('a figure that is not a sentence', () => {
  it('writes a quantity the way its reader writes quantities', () => {
    // The module hands over 48213 and nothing else. Grouping it here rather
    // than in the module is what makes the same number readable to both:
    // `48,213` to an English reader is `48.213` to a German one, and a
    // German reader shown the English form reads forty-eight point two.
    expect(sayIn('en')(48213)).toBe('48,213')
    expect(sayIn('de')(48213)).toBe('48.213')
  })

  it('writes an absence as the same em dash in both languages', () => {
    // A glyph, not a word. There is nothing here for a translator to
    // decide, and a locale file entry for it would be an invitation to
    // change one of them and not the other.
    for (const locale of ['en', 'de'] as const) {
      expect(sayIn(locale)(null)).toBe(NOT_MEASURED)
      expect(sayIn(locale)(undefined)).toBe(NOT_MEASURED)
    }
  })

  it('leaves a string alone', () => {
    // A channel's own name, a person's display name, an IANA zone. None of
    // them is ours to translate and none of them is a quantity.
    expect(sayIn('de')('#retro')).toBe('#retro')
  })
})

describe('a duration', () => {
  it('reads in the units each language abbreviates with', () => {
    // The module says "an hour and twelve minutes" by returning a key and
    // two numbers. That German writes `Std.` where English writes `h` is a
    // fact about German, and it lives in the locale file.
    expect(sayIn('en')(durationMessage(4320))).toBe('1 h 12 min')
    expect(sayIn('de')(durationMessage(4320))).toBe('1 Std. 12 Min.')
  })

  it('says a length was never recorded rather than calling it zero', () => {
    expect(sayIn('en')(durationMessage(null))).toBe('length unknown')
    expect(sayIn('de')(durationMessage(null))).toBe('Länge unbekannt')
  })
})

describe('an instant', () => {
  it('is written in the order its reader reads dates in', () => {
    // This is why `useSay` formats with the language tag rather than the
    // locale code: `Intl` under a bare `en` writes the American order, and
    // this console's English is British.
    const at = figureMoment('2026-08-21T14:30:00Z')
    expect(sayIn('en')(at)).toBe('21 Aug 2026, 14:30 UTC')
    expect(sayIn('de')(at)).toBe('21. Aug. 2026, 14:30 UTC')
  })

  it('names the zone it is in, in both languages', () => {
    // The server render has no idea what zone the reader is in, so every
    // instant on these pages is UTC. Saying so is the honest half of that.
    for (const locale of ['en', 'de'] as const) {
      expect(sayIn(locale)(figureMoment('2026-01-01T00:00:00Z'))).toContain('UTC')
    }
  })
})

describe('a count inside a sentence', () => {
  it('picks the singular and the plural in each language', () => {
    // Written as plural forms in the locale file rather than as an `if` in
    // a module: German and English do not always agree about where the
    // boundary is, and a module that decided would be deciding it in
    // English for both.
    const one = { key: 'calendar.sessionCount', params: { count: 1 } }
    const many = { key: 'calendar.sessionCount', params: { count: 4 } }
    expect(sayIn('en')(one)).toBe('1 session')
    expect(sayIn('en')(many)).toBe('4 sessions')
    expect(sayIn('de')(one)).toBe('1 Besprechung')
    expect(sayIn('de')(many)).toBe('4 Besprechungen')
  })

  it('still groups the count it pluralised by', () => {
    // The count is handed over twice on purpose -- once as a number, to
    // choose the form, and once as text, to be read. A single pass would
    // have to give up one of the two.
    expect(sayIn('de')({ key: 'calendar.sessionCount', params: { count: 4210 } })).toBe(
      '4.210 Besprechungen',
    )
  })
})

describe('a sentence built out of other sentences', () => {
  it('resolves every message nested inside it', () => {
    // The heatmap cell is the deepest of these: a date, a count of
    // meetings, a length, a count of people and a word for how busy the
    // day was, four of which are decisions of their own.
    const said = sayIn('en')(describeCell(BUSY_DAY))
    expect(said).toBe(
      'Friday, 21 August 2026 (UTC): 3 sessions, 1 h 12 min, 5 people. Activity: moderate (2 of 4).',
    )
  })

  it('resolves them in German too, including the words Intl supplies', () => {
    const said = sayIn('de')(describeCell(BUSY_DAY))
    expect(said).toBe(
      'Freitag, 21. August 2026 (UTC): 3 Besprechungen, 1 Std. 12 Min., 5 Personen. '
      + 'Aktivität: mittel (2 von 4).',
    )
  })

  it('leaves no key showing anywhere in either language', () => {
    // The failure that survives every structural check: a key spelled one
    // way in a module and another in the locale file renders as itself, and
    // reads as a label until somebody looks at it.
    const report = parseGuildReport({
      guild_id: '1',
      sessions: 3,
      documented: 1,
      open_sessions: 1,
      recorded_seconds: 9000,
      speech_seconds: 3000,
      unmeasured_tracks: 2,
      tracks: 6,
      distinct_participants: 4,
      average_participants: 2.5,
      largest_meeting: 4,
      average_duration_seconds: 3000,
      longest_duration_seconds: 4000,
      first_session_at: '2026-01-02T09:00:00Z',
      last_session_at: '2026-08-21T14:30:00Z',
      timezone: 'Europe/Berlin',
      months: [{ month: '2026-01', sessions: 2, recorded_seconds: 6000, documented: 1 }],
    })

    const said: string[] = []
    for (const locale of ['en', 'de'] as const) {
      const say = sayIn(locale)
      for (const figure of summaryFigures({
        total_speech_seconds: 4320,
        unmeasured_tracks: 2,
        sessions_attended: 3,
        sessions_with_protocol: 1,
        people_spoken_with: 4,
        words_transcribed: 48213,
        longest_session: null,
        most_recent_session: null,
        first_session: null,
      })) {
        said.push(say(figure.value), figure.note ? say(figure.note) : '')
      }
      for (const row of reportMonthRows(report)) said.push(say(row.detail))
      for (const week of buildYearGrid(2026, [])) {
        for (const cell of week) said.push(say(describeCell(cell)))
      }
      said.push(say(describeFailure({ statusCode: 503 })))
    }

    for (const one of said) {
      expect(one, `"${one}" reads as a key rather than as a sentence`).not.toMatch(
        /\b(?:common|dashboard|recordings|calendar|admin)\.[a-zA-Z.]+/,
      )
    }
  })
})

describe('a number that is not a quantity', () => {
  it('leaves a year ungrouped', () => {
    // `2,026` is not a year. Anything that is a number without being a
    // quantity is passed as a string for exactly this reason, and a month
    // travels as an instant rather than as a pair of numbers.
    const report = parseGuildReport({
      months: [{ month: '2026-01', sessions: 2, recorded_seconds: 6000, documented: 1 }],
    })
    const row = reportMonthRows(report)[0]!
    expect(sayIn('en')(row.detail)).toContain('January 2026')
    expect(sayIn('de')(row.detail)).toContain('Januar 2026')
    for (const locale of ['en', 'de'] as const) {
      expect(sayIn(locale)(row.detail)).not.toMatch(/2[,.]026/)
    }
  })
})
