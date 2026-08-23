/**
 * Whether the console actually speaks the second language it claims to.
 *
 * Two failures are worth a build breaking over, and neither is visible to
 * anybody who reads English.
 *
 * The first is a key added to one file and not the other. It is the normal
 * outcome of a hurried afternoon: the English sentence is what you were
 * writing, the German one is what you meant to come back to. Nothing warns
 * you, the page renders perfectly in the language you were testing in, and
 * the hole only opens under a reader who chose German.
 *
 * The second is worse because it looks finished. A `de.json` produced by
 * copying `en.json` and translating as far as the eye got is a file that
 * passes every structural check ever written -- same keys, same shape,
 * nothing empty -- and is still English. So a German value byte-identical
 * to its English one fails here unless it is on a list somebody had to add
 * it to.
 *
 * The files are read from disk rather than imported, the way
 * `palette.spec.ts` reads the stylesheet: what ships is the file, so the
 * file is what is checked.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import { translateOr } from '../app/utils/i18nFallback'

// Resolved from the working directory rather than from `import.meta.url`:
// the tests run under happy-dom, where `import.meta.url` is not a `file:`
// URL and `fileURLToPath` refuses it.
function load(locale: string): unknown {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

/** Every leaf, as `namespace.name` -> the sentence. */
function flatten(node: unknown, prefix = ''): Record<string, string> {
  const flat: Record<string, string> = {}
  for (const [name, value] of Object.entries(node as Record<string, unknown>)) {
    const key = prefix ? `${prefix}.${name}` : name
    if (value !== null && typeof value === 'object') Object.assign(flat, flatten(value, key))
    else flat[key] = String(value)
  }
  return flat
}

const EN = flatten(load('en'))
const DE = flatten(load('de'))

/**
 * Keys whose German is allowed to be exactly its English.
 *
 * It lives here, in the test, rather than in a data file next to the
 * translations -- so that growing it is a diff a reviewer reads, and every
 * entry has had to survive somebody asking "is that really the same word?".
 * Two answers are legitimate: a proper noun, and a word German borrowed
 * unchanged.
 */
const IDENTICAL_BY_RIGHT: Record<string, string> = {
  'common.brand': 'a product name, and not ours to translate',
  'error.status': '"Status" is the same word in German, and the rest is a number',
  'recordings.solo': 'the word every mixing desk prints on that button, in German too',
  'settings.appearance.system':
    'German calls the operating system "das System" too, and this label names it',
  'admin.destinations.nameLabel': '"Name" is the same word in German, and this label is that word',
  'admin.destinations.formatLabel':
    'German borrowed "Format" unchanged; "Dateityp" would name something narrower than this field',
}

describe('the two locale files', () => {
  it('agree on which keys exist', () => {
    // Both directions, because both failures happen: a key added to English
    // and forgotten in German leaves a German page with an English hole,
    // and a key left in German after English dropped it is a sentence that
    // nothing can ever render.
    expect(Object.keys(DE).sort()).toEqual(Object.keys(EN).sort())
  })

  it.each(['en', 'de'])('says something for every key in %s', (locale) => {
    // An empty string is not a translation, it is a key that renders as
    // nothing at all -- a button with no label rather than a button with
    // the wrong one, which is the harder of the two to notice.
    for (const [key, value] of Object.entries(locale === 'en' ? EN : DE)) {
      expect(value.trim(), `${locale}: ${key} is empty`).not.toBe('')
    }
  })

  it('keeps every placeholder the English sentence had', () => {
    // `{command}` dropped from a translation is a sentence that instructs
    // somebody to run nothing, and vue-i18n renders it without complaint.
    for (const [key, english] of Object.entries(EN)) {
      const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort()
      expect(placeholders(DE[key] ?? ''), `${key} lost a placeholder in German`).toEqual(
        placeholders(english),
      )
    }
  })
})

describe('the German', () => {
  it('is a translation rather than a copy', () => {
    const untranslated = Object.keys(EN).filter(
      (key) => DE[key] === EN[key] && !(key in IDENTICAL_BY_RIGHT),
    )
    expect(
      untranslated,
      `still English in de.json: ${untranslated.join(', ')} -- translate them, or, if the German `
      + 'really is the same word, add the key to IDENTICAL_BY_RIGHT with the reason',
    ).toEqual([])
  })

  it('spells German with the letters German has', () => {
    // `fuer`, `loeschen`, `Massnahme`: the transliterations a stack that
    // could not carry umlauts used to force. Nothing in this stack has that
    // excuse, and a page written in them reads as machine output.
    const TRANSLITERATED = /\b(fuer|ueber|koennen|moechte|loeschen|schliessen|groesse|massnahme)\b/i
    for (const [key, value] of Object.entries(DE)) {
      expect(TRANSLITERATED.test(value), `${key}: "${value}" spells an umlaut out`).toBe(false)
    }
  })

  it('does not address the reader as "Sie"', () => {
    // The console speaks impersonally in both languages -- "Abmelden",
    // "Erneut versuchen" -- and a stray "Melden Sie sich ab" is not merely
    // inconsistent: it is a second register, and a reader meeting both
    // learns that nobody is deciding.
    for (const [key, value] of Object.entries(DE)) {
      expect(/\bSie\b/.test(value), `${key}: "${value}" says "Sie"`).toBe(false)
    }
  })
})

describe('a page whose translations never arrived', () => {
  // `error.vue` renders when things are broken, and one of the things that
  // can be broken is the fetch of the locale file it wanted to render in.
  it('shows the English sentence when the key resolves to itself', () => {
    // What vue-i18n does with a key it has no message for: it hands the key
    // back. Rendering that is the error page showing its own source code to
    // somebody who came here because something already went wrong.
    const noMessages = (key: string) => key
    expect(translateOr(noMessages, 'error.retry', 'Try again')).toBe('Try again')
  })

  it('shows the English sentence when there is no translator at all', () => {
    // The i18n plugin failing to install is a real state, and it is exactly
    // the state in which somebody is looking at `error.vue`.
    expect(translateOr(null, 'error.retry', 'Try again')).toBe('Try again')
    expect(translateOr(undefined, 'error.retry', 'Try again')).toBe('Try again')
  })

  it('shows the English sentence when the translator throws', () => {
    const broken = () => {
      throw new Error('no i18n instance')
    }
    expect(translateOr(broken, 'error.retry', 'Try again')).toBe('Try again')
  })

  it('shows the English sentence rather than a blank one', () => {
    expect(translateOr(() => '   ', 'error.retry', 'Try again')).toBe('Try again')
  })

  it('prefers the translation whenever there is one', () => {
    // The fallback is a floor, not a ceiling: a page that ignored a loaded
    // German message would be an error page permanently in English.
    expect(translateOr(() => 'Erneut versuchen', 'error.retry', 'Try again')).toBe(
      'Erneut versuchen',
    )
  })

  it('passes the named values through to the translator', () => {
    const t = (key: string, named?: Record<string, unknown>) => `${key}:${named?.code}`
    expect(translateOr(t, 'error.status', 'Status 500', { code: 500 })).toBe('error.status:500')
  })
})

/**
 * The third failure: a key that is in the file and not in the object.
 *
 * `JSON.parse` keeps the last of two identically-named siblings and drops
 * the first without a word. Every check above runs against the parsed
 * object, so a file carrying two `"admin"` blocks passes all of them --
 * the shadowed block is simply not there to be compared, and if both
 * locales were corrupted the same way they still agree with each other.
 * Lint, typecheck and build are equally blind to it.
 *
 * That is not hypothetical. Rebasing the console pull requests onto one
 * another produced exactly it: git's line-based merge is happy to place
 * two `"dashboard"` objects in one file, and a whole namespace stopped
 * existing at runtime while this suite stayed green.
 *
 * Re-serialising catches it, because a dropped key comes back shorter. It
 * also pins the files to the shape a tool writes -- two-space indent,
 * insertion order, one trailing newline -- which is what lets them be
 * merged structurally rather than textually the next time two branches
 * both add keys.
 */
describe('the locale files say everything they contain', () => {
  it.each(['en', 'de'])(
    '%s.json is exactly what re-serialising it produces',
    (locale) => {
      const text = readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8')

      expect(`${JSON.stringify(JSON.parse(text), null, 2)}\n`).toBe(text)
    },
  )
})
