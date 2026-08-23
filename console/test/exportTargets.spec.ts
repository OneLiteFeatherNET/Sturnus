/**
 * Where a guild publishes, and the four properties of that page that are
 * invisible in a screenshot.
 *
 * Every one of these is a decision the API also makes, restated on the
 * console side so that a reader gets the reason rather than a bare status —
 * `apiError` keeps nothing of a refusal but its number. A test that only
 * checked the happy path would leave all four free to drift out of step
 * with `sturnus.console.routes_exports`.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  EXPORT_FORMATS,
  FORMAT_HTML,
  FORMAT_MARKDOWN,
  FORMAT_OUTLINE,
  acceptsTarget,
  canSubmitSecret,
  describeTargetError,
  draftBody,
  draftOf,
  draftProblems,
  emptyDraft,
  enabledLabelKey,
  enabledTargetCount,
  fallbackNote,
  formatChoices,
  formatSpec,
  isDraftReady,
  orderTargets,
  parseTarget,
  parseTargets,
  primaryTarget,
  problemFor,
  secretState,
  takenNames,
  targetPath,
  targetSecretPath,
  targetSummary,
  targetsPath,
  type ExportTarget,
} from '../app/utils/exportTargets'
import type { Message } from '../app/utils/message'

/** Read rather than imported, the way `palette.spec.ts` reads the
 *  stylesheet: what ships is the file, so the file is what is checked. */
function messages(locale: 'en' | 'de'): Record<string, unknown> {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

/** Whether a dotted key resolves to a string in a locale file. */
function translated(bundle: Record<string, unknown>, key: string): boolean {
  let node: unknown = bundle
  for (const part of key.split('.')) {
    if (typeof node !== 'object' || node === null) return false
    node = (node as Record<string, unknown>)[part]
  }
  return typeof node === 'string' && node.trim() !== ''
}

const EN = messages('en')
const DE = messages('de')

/** Every key a message tree names, including the nested ones. */
function keysIn(message: Message): string[] {
  const found = [message.key]
  for (const value of Object.values(message.params ?? {})) {
    if (typeof value === 'object' && value !== null && 'key' in value) {
      found.push(...keysIn(value as Message))
    }
  }
  return found
}

function target(over: Partial<ExportTarget> = {}): ExportTarget {
  return {
    id: 1,
    guildId: '1289374650912837465',
    format: FORMAT_OUTLINE,
    name: 'Wiki',
    target: 'c0ffee',
    config: {},
    hasSecret: false,
    enabled: true,
    createdAt: '2026-08-01T10:00:00+00:00',
    updatedAt: '2026-08-01T10:00:00+00:00',
    ...over,
  }
}

describe('reading what the API sent', () => {
  it('keeps the guild as a string and the destination id as a number', () => {
    // The API's own asymmetry, and it is not an inconsistency: a Discord
    // snowflake exceeds `Number.MAX_SAFE_INTEGER`, so anything that
    // round-trips through a JavaScript number hands back an id ending in
    // other digits. The destination's own key is a `SERIAL`.
    const parsed = parseTarget({
      id: 7,
      guild_id: '1289374650912837465',
      format: 'markdown',
      name: 'Bucket',
      target: 'minutes/2026',
      config: { note: 'kept' },
      has_secret: false,
      enabled: true,
      created_at: '2026-08-01T10:00:00+00:00',
      updated_at: '2026-08-02T10:00:00+00:00',
    })!
    expect(parsed.id).toBe(7)
    expect(parsed.guildId).toBe('1289374650912837465')
  })

  it('never produces a field a credential could sit in', () => {
    // The property the whole feature rests on. `ExportTarget` carries
    // `has_secret` and nothing else about the credential, and a shape here
    // that *could* hold a token is a shape somebody eventually binds to an
    // input.
    const parsed = parseTarget({ id: 1, has_secret: true, secret: 'tok_live_do_not_render' })!
    expect(Object.keys(parsed)).not.toContain('secret')
    expect(Object.values(parsed)).not.toContain('tok_live_do_not_render')
    expect(parsed.hasSecret).toBe(true)
  })

  it('carries an unknown key in `config` through untouched', () => {
    // The page never edits it, so it has to survive a rename: dropping it
    // would send `{}` back on the next save and erase whatever a later
    // format put there.
    const parsed = parseTarget({ id: 1, config: { space: 'ENG', parent: 42 } })!
    expect(parsed.config).toEqual({ space: 'ENG', parent: 42 })
  })

  it('treats a destination with no `enabled` as switched off', () => {
    // A destination this console cannot tell the state of is better drawn
    // as off — the reader switches it on and learns the truth — than drawn
    // as publishing when it is not.
    expect(parseTarget({ id: 1 })!.enabled).toBe(false)
  })

  it('drops a row whose id is null rather than reading it as nought', () => {
    // `Number(null)` is 0, and 0 is a safe integer. Left unchecked, such a
    // row would become a destination every write addressed as
    // `/export-targets/0`.
    expect(parseTarget({ id: null })).toBeNull()
    expect(parseTarget({ id: 'seven' })).toBeNull()
    expect(parseTarget({ id: 1.5 })).toBeNull()
  })

  it('drops a row with no id and keeps a row with no name', () => {
    // Nothing can be done to a row with no id: every write addresses it by
    // that id. A row with no name is a destination that renders as blank,
    // and one nobody can switch off is worse than one with an empty label.
    const parsed = parseTargets({ targets: [{ name: 'nameless' }, { id: 3 }] })
    expect(parsed.map((row) => row.id)).toEqual([3])
    expect(parsed[0]!.name).toBe('')
  })

  it('answers an empty list for a payload of the wrong shape', () => {
    for (const payload of [null, undefined, 'no', 42, {}, { targets: 'no' }]) {
      expect(parseTargets(payload)).toEqual([])
    }
  })
})

describe('which formats are offered', () => {
  it('offers exactly the three this deployment builds', () => {
    // `pdf` and `confluence` are specified and deliberately not built, and
    // configuring one answers 400 with the list of what may be configured
    // instead. An option a save refuses is not an option.
    expect(EXPORT_FORMATS.map((entry) => entry.name)).toEqual([
      FORMAT_OUTLINE,
      FORMAT_MARKDOWN,
      FORMAT_HTML,
    ])
  })

  it('offers no row for a format the API would refuse', () => {
    // The argument, stated as an assertion: absent, not disabled. The
    // opposite choice — an inert "PDF (coming soon)" row — is what the
    // account menu does for two-factor authentication, and the reason it
    // is wrong here is that this is a form field and the console cannot
    // see the deployment's registry to know when the row would stop being
    // true.
    const values = formatChoices((key) => key).map((option) => option.value)
    expect(values).not.toContain('pdf')
    expect(values).not.toContain('confluence')
    expect(formatSpec('pdf')).toBeNull()
  })

  it('offers a stored format it has never heard of, rather than dropping it', () => {
    // The day this deployment adds `pdf` to its registry, a destination
    // configured for it must survive being renamed. A picker that omitted
    // the stored value would render as though `outline` were chosen and
    // rewrite the destination to `outline` on the next save.
    const values = formatChoices((key) => key, 'pdf').map((option) => option.value)
    expect(values).toEqual([FORMAT_OUTLINE, FORMAT_MARKDOWN, FORMAT_HTML, 'pdf'])
  })

  it('does not add a second row for a stored format it does know', () => {
    const values = formatChoices((key) => key, FORMAT_HTML).map((option) => option.value)
    expect(values).toEqual([FORMAT_OUTLINE, FORMAT_MARKDOWN, FORMAT_HTML])
  })

  it('labels a stored unknown format with its own raw name', () => {
    // There is no key for a word this console has never seen, and
    // inventing a friendly one would be the console guessing at somebody
    // else's release note.
    const row = formatChoices((key) => key, 'confluence').at(-1)!
    expect(row.label).toBe('confluence')
  })

  it('asks each format for a different address, and says so differently', () => {
    // Outline wants a collection id and the object store wants a key
    // prefix. A form that showed the union of every field would ask for
    // both.
    expect(formatSpec(FORMAT_OUTLINE)!.targetKind).toBe('collection')
    expect(formatSpec(FORMAT_MARKDOWN)!.targetKind).toBe('prefix')
    expect(formatSpec(FORMAT_HTML)!.targetKind).toBe('prefix')
  })

  it('knows which formats this deployment can serve back and which it cannot', () => {
    // An Outline document's bytes live in Outline; the object-store ones
    // are served under the session's own rule. `routes_documents` decides
    // the same thing from the same registry.
    expect(formatSpec(FORMAT_OUTLINE)!.readable).toBe(false)
    expect(formatSpec(FORMAT_MARKDOWN)!.readable).toBe(true)
    expect(formatSpec(FORMAT_HTML)!.readable).toBe(true)
  })
})

describe('what a format will accept as an address', () => {
  it('takes an Outline collection id and refuses whitespace in it', () => {
    // The API's `_OUTLINE_TARGET`. What it excludes is a paste that went
    // wrong; the failure otherwise is an unexplained 404 from Outline.
    expect(acceptsTarget(FORMAT_OUTLINE, 'c0ffee-1234')).toBe(true)
    expect(acceptsTarget(FORMAT_OUTLINE, 'two words')).toBe(false)
    expect(acceptsTarget(FORMAT_OUTLINE, ' leading')).toBe(false)
    expect(acceptsTarget(FORMAT_OUTLINE, '/slashfirst')).toBe(false)
    expect(acceptsTarget(FORMAT_OUTLINE, '')).toBe(false)
  })

  it('takes an object-store prefix and refuses one that could traverse', () => {
    // The API's `_OBJECT_PREFIX`. `..` cannot appear because a dot may not
    // follow a dot; a leading slash is a key nobody meant to write.
    expect(acceptsTarget(FORMAT_MARKDOWN, 'minutes/2026')).toBe(true)
    expect(acceptsTarget(FORMAT_HTML, 'a_b-c.d/e')).toBe(true)
    expect(acceptsTarget(FORMAT_MARKDOWN, '../secrets')).toBe(false)
    expect(acceptsTarget(FORMAT_MARKDOWN, '/leading')).toBe(false)
    expect(acceptsTarget(FORMAT_MARKDOWN, 'trailing/')).toBe(false)
    expect(acceptsTarget(FORMAT_MARKDOWN, 'a b')).toBe(false)
  })

  it('accepts anything non-empty for a format it has no pattern for', () => {
    // This console has no pattern for a format it has never heard of, and
    // inventing one would refuse a target the deployment is perfectly
    // happy with.
    expect(acceptsTarget('pdf', 'whatever a pdf target is')).toBe(true)
    expect(acceptsTarget('pdf', '  ')).toBe(false)
  })
})

describe('the order the destinations publish in', () => {
  const first = target({ id: 3, name: 'Wiki' })
  const second = target({ id: 9, name: 'Archive' })
  const third = target({ id: 12, name: 'Bucket' })

  it('is by id and never by name', () => {
    // Not cosmetic. `destinations_for` sorts by id precisely so the
    // primary does not move when somebody renames one, and
    // `session.document_url` — the link the Discord announcement carries —
    // is stamped from the primary alone. Alphabetical order would show a
    // reader a first row that is not the first destination.
    expect(orderTargets([third, first, second]).map((row) => row.id)).toEqual([3, 9, 12])
  })

  it('names the oldest enabled destination as the one Discord announces', () => {
    expect(primaryTarget([third, first, second])!.id).toBe(3)
  })

  it('skips a switched-off destination when naming the announced one', () => {
    // `enabled_for` is what the worker reads, so a disabled row is not in
    // the running at all.
    const off = { ...first, enabled: false }
    expect(primaryTarget([third, off, second])!.id).toBe(9)
  })

  it('names none when every destination is switched off', () => {
    expect(primaryTarget([{ ...first, enabled: false }])).toBeNull()
    expect(primaryTarget([])).toBeNull()
  })

  it('does not skip a format it has never heard of when naming the announced one', () => {
    // Whether the deployment can publish `pdf` is the deployment's answer.
    // A console that skipped this row because it does not know the word
    // would name the wrong destination as the announced one on exactly the
    // deployment where it matters.
    const exotic = target({ id: 1, format: 'pdf' })
    expect(primaryTarget([first, exotic])!.id).toBe(1)
  })

  it('counts the ones that are switched on', () => {
    expect(enabledTargetCount([first, { ...second, enabled: false }, third])).toBe(2)
  })
})

describe('what the page says about `document_target`', () => {
  it('says the Bot Settings fallback is what publishes, when nothing here is enabled', () => {
    // The two settings look like rivals, and this is where somebody
    // configuring destinations reads which of them is in force.
    expect(fallbackNote([]).key).toBe('admin.destinations.fallbackInUse')
    expect(fallbackNote([target({ enabled: false })]).key).toBe(
      'admin.destinations.fallbackInUse',
    )
  })

  it('says the fallback has been replaced once anything here is enabled', () => {
    // `destinations_for` **replaces** the fallback rather than joining it,
    // so enabling one destination here silently stops `document_target`
    // being used. That has to be said on the screen where it happened.
    const note = fallbackNote([target(), target({ id: 2, name: 'Bucket' })])
    expect(note.key).toBe('admin.destinations.fallbackReplaced')
    expect(note.params?.count).toBe(2)
  })
})

describe('the credential', () => {
  it('offers no way to read one back', () => {
    // There is no route that returns a secret, so a control that offered
    // to show one would be offering something the API cannot serve.
    const state = secretState(target({ hasSecret: true }))
    expect(Object.keys(state)).toEqual(['stored', 'statusKey', 'actionKey', 'canClear'])
    expect(Object.keys(state)).not.toContain('value')
  })

  it('says which of "store" and "replace" this is', () => {
    expect(secretState(target({ hasSecret: false })).actionKey).toBe(
      'admin.destinations.secretSet',
    )
    expect(secretState(target({ hasSecret: true })).actionKey).toBe(
      'admin.destinations.secretReplace',
    )
  })

  it('offers clearing only where there is something to clear', () => {
    expect(secretState(target({ hasSecret: true })).canClear).toBe(true)
    expect(secretState(target({ hasSecret: false })).canClear).toBe(false)
  })

  it('refuses to submit an empty credential', () => {
    // The API answers 400 to `""`, and refusing it here is what stops
    // "save an empty box" from looking like a way to clear one. Clearing
    // has its own button and it is the only way.
    expect(canSubmitSecret('')).toBe(false)
    expect(canSubmitSecret('tok')).toBe(true)
  })

  it('is not a field of the thing the destination form submits', () => {
    // The failure this prevents: an empty password box beside a configured
    // credential, saved along with the rest of the form, silently wiping a
    // token every time somebody corrects a typo in a name. It is not
    // merely avoided, it is unrepresentable — there is no field.
    expect(Object.keys(emptyDraft()).sort()).toEqual(['enabled', 'format', 'name', 'target'])
    expect(Object.keys(draftBody(emptyDraft())).sort()).toEqual([
      'config',
      'enabled',
      'format',
      'name',
      'target',
    ])
  })
})

describe('what is wrong with a draft', () => {
  it('is happy with a complete one', () => {
    expect(draftProblems({ format: FORMAT_OUTLINE, name: 'Wiki', target: 'c0ffee', enabled: true }))
      .toEqual([])
    expect(isDraftReady(draftOf(target()))).toBe(true)
  })

  it('refuses a name that is blank or only spaces', () => {
    // `_BAD_NAME` in the API: a non-empty string after stripping.
    expect(problemFor(draftProblems({ ...emptyDraft(), target: 'c0ffee' }), 'name')?.key).toBe(
      'admin.destinations.nameEmpty',
    )
    const spaces = draftProblems({ ...emptyDraft(), name: '   ', target: 'c0ffee' })
    expect(problemFor(spaces, 'name')?.key).toBe('admin.destinations.nameEmpty')
  })

  it('refuses a name this guild already uses', () => {
    // The API answers 409, because a create that upserted would let a typo
    // redirect a guild's protocols with nothing said.
    const problems = draftProblems(
      { format: FORMAT_OUTLINE, name: 'Wiki', target: 'c0ffee', enabled: true },
      ['Wiki', 'Archive'],
    )
    expect(problemFor(problems, 'name')?.key).toBe('admin.destinations.nameTaken')
  })

  it('does not complain that a destination collides with its own name', () => {
    // A `PUT` reads the name from the stored row and ignores the body, so
    // renaming is a create plus a delete. Complaining here would refuse a
    // save that changes something else entirely.
    const stored = target({ id: 4, name: 'Wiki' })
    const others = target({ id: 5, name: 'Archive' })
    expect(takenNames([stored, others], 4)).toEqual(['Archive'])
    expect(isDraftReady(draftOf(stored), takenNames([stored, others], 4))).toBe(true)
  })

  it('names the field each complaint is about', () => {
    // So the page can put it beside that field rather than in a list of
    // grievances at the bottom.
    const problems = draftProblems({ format: '', name: '', target: '', enabled: true })
    expect(problems.map((problem) => problem.field)).toEqual(['name', 'format', 'target'])
  })

  it('says the address is wrong in the words of the format that refused it', () => {
    // "Not a collection id" and "not an object prefix" are different
    // sentences, because they are different mistakes.
    const outline = draftProblems({
      format: FORMAT_OUTLINE, name: 'Wiki', target: 'two words', enabled: true,
    })
    expect(problemFor(outline, 'target')?.key).toBe('admin.destinations.targetNotCollection')
    const markdown = draftProblems({
      format: FORMAT_MARKDOWN, name: 'Bucket', target: '../up', enabled: true,
    })
    expect(problemFor(markdown, 'target')?.key).toBe('admin.destinations.targetNotPrefix')
  })

  it('trims what it sends, the way the API trims what it stores', () => {
    // So that a value read straight back from the API equals the draft and
    // the form does not look unsaved the moment it was saved.
    const body = draftBody(
      { format: ' outline ', name: '  Wiki  ', target: ' c0ffee ', enabled: false },
      { space: 'ENG' },
    )
    expect(body).toEqual({
      format: 'outline', name: 'Wiki', target: 'c0ffee', config: { space: 'ENG' }, enabled: false,
    })
  })
})

describe('where the requests go', () => {
  it('escapes the guild in every path', () => {
    // A guild id reaches this from a picker and from local storage, and a
    // path built by concatenation is a path somebody eventually puts a
    // slash into.
    expect(targetsPath('a/b')).toBe('/guilds/a%2Fb/export-targets')
    expect(targetPath('17', 4)).toBe('/guilds/17/export-targets/4')
    expect(targetSecretPath('17', 4)).toBe('/guilds/17/export-targets/4/secret')
  })

  it('writes the credential to a path of its own', () => {
    // Not a field on the destination. A `PUT` on the target that also took
    // the secret would clear it on every rename.
    expect(targetSecretPath('17', 4)).not.toBe(targetPath('17', 4))
  })
})

describe('when a write does not work', () => {
  it.each([
    [400, 'admin.destinations.errorRefused'],
    [401, 'admin.destinations.errorSession'],
    [404, 'admin.destinations.errorGone'],
    [409, 'admin.destinations.errorDuplicate'],
    [0, 'admin.destinations.errorUnreachable'],
    [500, 'admin.destinations.errorStatus'],
  ])('explains a %s', (status, key) => {
    expect(describeTargetError({ status }).key).toBe(key)
  })

  it('treats an error carrying no status as unreachable', () => {
    // Nothing but `ApiError` should reach here, and that always has one —
    // but a page that threw something else must not render `undefined` at
    // somebody.
    expect(describeTargetError(new Error('nope')).key).toBe(
      'admin.destinations.errorUnreachable',
    )
  })

  it('writes an unexplained status as a string rather than as a quantity', () => {
    // `i18n/README.md` draws that line: 500 is not five hundred of
    // anything, and a locale that grouped it would render `500` as `500`
    // in English and as `500` in German only by luck.
    expect(describeTargetError({ status: 503 }).params?.status).toBe('503')
  })
})

describe('the sentences this module names', () => {
  /** Every key any function here can return, gathered by calling them. */
  const KEYS = [
    ...EXPORT_FORMATS.flatMap((entry) => [
      entry.labelKey, entry.noteKey, entry.targetLabelKey, entry.targetHintKey,
    ]),
    'admin.destinations.formatUnknownDetail',
    enabledLabelKey(target()),
    enabledLabelKey(target({ enabled: false })),
    ...keysIn(targetSummary(target())),
    ...keysIn(targetSummary(target({ format: 'pdf' }))),
    ...keysIn(fallbackNote([])),
    ...keysIn(fallbackNote([target()])),
    ...Object.values(secretState(target({ hasSecret: true }))).filter(
      (value): value is string => typeof value === 'string',
    ),
    ...Object.values(secretState(target({ hasSecret: false }))).filter(
      (value): value is string => typeof value === 'string',
    ),
    ...draftProblems({ format: '', name: '', target: '', enabled: true })
      .flatMap((problem) => keysIn(problem.message)),
    ...draftProblems(
      { format: FORMAT_OUTLINE, name: 'Wiki', target: 'two words', enabled: true },
      ['Wiki'],
    ).flatMap((problem) => keysIn(problem.message)),
    ...draftProblems({
      format: FORMAT_MARKDOWN, name: 'Bucket', target: '../up', enabled: true,
    }).flatMap((problem) => keysIn(problem.message)),
    ...[400, 401, 404, 409, 0, 500].flatMap((status) =>
      keysIn(describeTargetError({ status }))),
  ]

  it('names every one of them with a key rather than with English', () => {
    // A sentence that slipped in here would render identically in English
    // and be untranslatable in German, which is the failure that shows up
    // only for a reader who does not speak English.
    //
    // `common.*` is admitted for exactly one thing: a format's name, which
    // the recording page has to say as well and which therefore has no
    // single home. Everything else on this page is this page's.
    for (const key of KEYS) {
      expect(key).toMatch(/^(admin\.destinations|common)\.[a-z][A-Za-z]*$/)
    }
  })

  it.each(['en', 'de'])('has a %s translation for every one of them', (locale) => {
    const bundle = locale === 'en' ? EN : DE
    for (const key of KEYS) {
      expect(translated(bundle, key), `${key} is untranslated in ${locale}`).toBe(true)
    }
  })
})
