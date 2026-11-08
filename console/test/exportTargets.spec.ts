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
  FORMAT_HTML,
  FORMAT_MARKDOWN,
  FORMAT_OUTLINE,
  SINK_OBJECT_STORE,
  SINK_OUTLINE,
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
  formatLabelKey,
  formatSpec,
  isDraftReady,
  orderTargets,
  parseFormats,
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
  type FormatInfo,
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

/**
 * What `GET /api/export-formats` says on a deployment built as this one is.
 *
 * A fixture rather than a constant imported from the module under test,
 * which is the whole point of the change these tests cover: the console
 * holds no list any more, so every one of these assertions has to be handed
 * the deployment's answer the way the page is handed it. A test that
 * imported the list would be testing the old design.
 */
const CATALOGUE: readonly FormatInfo[] = [
  { name: FORMAT_OUTLINE, available: true, sink: SINK_OUTLINE },
  { name: FORMAT_MARKDOWN, available: true, sink: SINK_OBJECT_STORE },
  { name: FORMAT_HTML, available: true, sink: SINK_OBJECT_STORE },
  { name: 'pdf', available: false, sink: null },
  { name: 'confluence', available: false, sink: null },
]

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

describe('reading the format catalogue', () => {
  it('keeps the order the API sent, because the first row is the ordinary choice', () => {
    const parsed = parseFormats({
      formats: [
        { name: 'outline', available: true, sink: 'outline' },
        { name: 'html', available: true, sink: 'object_store' },
        { name: 'pdf', available: false, sink: null },
      ],
    })
    expect(parsed.map((entry) => entry.name)).toEqual(['outline', 'html', 'pdf'])
  })

  it('reads a missing or unknown sink as none rather than guessing one', () => {
    // The address field follows the sink family. A default here would put
    // an object-store rule under a field nobody has said takes one.
    const parsed = parseFormats({
      formats: [{ name: 'pdf', available: false }, { name: 'zip', available: true, sink: 'tape' }],
    })
    expect(parsed.map((entry) => entry.sink)).toEqual([null, null])
  })

  it('treats a format as unavailable unless the API said otherwise', () => {
    // Absent is false rather than true, the same way `enabled` is on a
    // destination: a format this console cannot tell the state of is better
    // drawn as not buildable — the reader is told to reload — than offered
    // and then refused on save.
    expect(parseFormats({ formats: [{ name: 'pdf' }] })[0]!.available).toBe(false)
  })

  it('drops a row with no name and keeps everything else', () => {
    // There is nothing to store or to render for a nameless format.
    const parsed = parseFormats({ formats: [{ available: true }, { name: '  ' }, { name: 'html' }] })
    expect(parsed.map((entry) => entry.name)).toEqual(['html'])
  })

  it('answers an empty list for a payload of the wrong shape', () => {
    // Which the page reports as "the catalogue could not be read", never as
    // "this deployment publishes nothing".
    for (const payload of [null, undefined, 'no', 42, {}, { formats: 'no' }]) {
      expect(parseFormats(payload)).toEqual([])
    }
  })
})

describe('which formats are offered', () => {
  it('offers exactly what the deployment reported, in the order it reported them', () => {
    // The property this whole change exists for. There is no list in the
    // console any more, so there is nothing here that can disagree with the
    // registry.
    const values = formatChoices((key) => key, CATALOGUE).map((option) => option.value)
    expect(values).toEqual([FORMAT_OUTLINE, FORMAT_MARKDOWN, FORMAT_HTML, 'pdf', 'confluence'])
  })

  it('renders a format this build cannot run as a disabled row saying so', () => {
    // The decision #150 could not take. It argued for absence, and the
    // decisive half of the argument was that the console could not see the
    // registry — so a "PDF, not built" row would have been a claim about a
    // build it could not inspect, still being made after `pdf` was built.
    // It can see it now. What is left is that PDF exists and this
    // deployment does not build it, which is what somebody who came here
    // looking for PDF needs told rather than left to infer from a gap.
    const row = formatChoices((key) => key, CATALOGUE).find((option) => option.value === 'pdf')!
    expect(row.disabled).toBe(true)
    expect(row.detail).toBe('admin.destinations.formatUnavailableDetail')
  })

  it('leaves every buildable format choosable', () => {
    const options = formatChoices((key) => key, CATALOGUE)
    for (const name of [FORMAT_OUTLINE, FORMAT_MARKDOWN, FORMAT_HTML]) {
      expect(options.find((option) => option.value === name)!.disabled).toBe(false)
    }
  })

  it('leaves the stored format choosable even where the build cannot run it', () => {
    // A disabled row is a fact stated; a disabled row that the control's
    // own value sits on is a picker pointing at something it may not
    // select, which is how the wrong value gets reported silently.
    const row = formatChoices((key) => key, CATALOGUE, 'pdf').find(
      (option) => option.value === 'pdf',
    )!
    expect(row.disabled).toBe(false)
  })

  it('offers a stored format the catalogue never mentioned, rather than dropping it', () => {
    // #150's rule, and it matters more now rather than less: the reported
    // list can arrive empty because the request failed. A picker that
    // omitted the stored value would render as though the first row were
    // chosen and rewrite the destination to it on the next save.
    const values = formatChoices((key) => key, CATALOGUE, 'zip').map((option) => option.value)
    expect(values.at(-1)).toBe('zip')
    const row = formatChoices((key) => key, CATALOGUE, 'zip').at(-1)!
    expect(row.label).toBe('zip')
    expect(row.detail).toBe('admin.destinations.formatUnknownDetail')
    expect(row.disabled).toBeFalsy()
  })

  it('keeps the stored format even when the catalogue could not be read at all', () => {
    // The worst version of this page: a timed-out request read as "this
    // deployment supports nothing", and every destination rewritten on the
    // next save.
    const values = formatChoices((key) => key, [], FORMAT_HTML).map((option) => option.value)
    expect(values).toEqual([FORMAT_HTML])
  })

  it('does not add a second row for a stored format the catalogue reports', () => {
    const values = formatChoices((key) => key, CATALOGUE, FORMAT_HTML).map(
      (option) => option.value,
    )
    expect(values).toEqual([FORMAT_OUTLINE, FORMAT_MARKDOWN, FORMAT_HTML, 'pdf', 'confluence'])
  })

  it('labels a format it has no word for with that format’s own raw name', () => {
    // The API has no message catalogue, so the words stay here — and a
    // format this deployment builds that this console has never been taught
    // a word for renders as itself rather than as nothing.
    const row = formatChoices((key) => key, [{ name: 'zip', available: true, sink: null }]).at(0)!
    expect(row.label).toBe('zip')
    expect(formatLabelKey('zip')).toBeNull()
    expect(formatLabelKey(FORMAT_OUTLINE)).toBe('common.formatOutline')
  })

  it('asks each sink family for a different address, and says so differently', () => {
    // Outline wants a collection id and the object store wants a key
    // prefix. A form that showed the union of every field would ask for
    // both. Decided by family and never by name, so a format built tomorrow
    // gets the right field with nothing added here.
    expect(formatSpec(FORMAT_OUTLINE, CATALOGUE)!.targetLabelKey).toBe(
      'admin.destinations.collectionLabel',
    )
    expect(formatSpec(FORMAT_MARKDOWN, CATALOGUE)!.targetLabelKey).toBe(
      'admin.destinations.prefixLabel',
    )
    expect(formatSpec(FORMAT_HTML, CATALOGUE)!.targetLabelKey).toBe(
      'admin.destinations.prefixLabel',
    )
  })

  it('says nothing about the address of a format that has no sink', () => {
    // Nothing in the API has decided what would carry a PDF, so nothing
    // here may render a field claiming to know.
    const spec = formatSpec('pdf', CATALOGUE)!
    expect(spec.sink).toBeNull()
    expect(spec.targetLabelKey).toBeNull()
    expect(spec.noteKey).toBeNull()
  })

  it('has no spec at all for a format the catalogue does not report', () => {
    expect(formatSpec('zip', CATALOGUE)).toBeNull()
    expect(formatSpec(FORMAT_OUTLINE, [])).toBeNull()
  })

  it('knows which formats this deployment can serve back and which it cannot', () => {
    // An Outline document's bytes live in Outline; the object-store ones
    // are served under the session's own rule. `routes_documents` decides
    // the same thing from the same registry.
    expect(formatSpec(FORMAT_OUTLINE, CATALOGUE)!.readable).toBe(false)
    expect(formatSpec(FORMAT_MARKDOWN, CATALOGUE)!.readable).toBe(true)
    expect(formatSpec(FORMAT_HTML, CATALOGUE)!.readable).toBe(true)
  })
})

describe('what a format will accept as an address', () => {
  it('takes an Outline collection id and refuses whitespace in it', () => {
    // The API's `_OUTLINE_TARGET`. What it excludes is a paste that went
    // wrong; the failure otherwise is an unexplained 404 from Outline.
    expect(acceptsTarget(FORMAT_OUTLINE, 'c0ffee-1234', CATALOGUE)).toBe(true)
    expect(acceptsTarget(FORMAT_OUTLINE, 'two words', CATALOGUE)).toBe(false)
    expect(acceptsTarget(FORMAT_OUTLINE, ' leading', CATALOGUE)).toBe(false)
    expect(acceptsTarget(FORMAT_OUTLINE, '/slashfirst', CATALOGUE)).toBe(false)
    expect(acceptsTarget(FORMAT_OUTLINE, '', CATALOGUE)).toBe(false)
  })

  it('takes an object-store prefix and refuses one that could traverse', () => {
    // The API's `_OBJECT_PREFIX`. `..` cannot appear because a dot may not
    // follow a dot; a leading slash is a key nobody meant to write.
    expect(acceptsTarget(FORMAT_MARKDOWN, 'minutes/2026', CATALOGUE)).toBe(true)
    expect(acceptsTarget(FORMAT_HTML, 'a_b-c.d/e', CATALOGUE)).toBe(true)
    expect(acceptsTarget(FORMAT_MARKDOWN, '../secrets', CATALOGUE)).toBe(false)
    expect(acceptsTarget(FORMAT_MARKDOWN, '/leading', CATALOGUE)).toBe(false)
    expect(acceptsTarget(FORMAT_MARKDOWN, 'trailing/', CATALOGUE)).toBe(false)
    expect(acceptsTarget(FORMAT_MARKDOWN, 'a b', CATALOGUE)).toBe(false)
  })

  it('accepts anything non-empty for a format with no reported sink', () => {
    // This console has no pattern for a format the deployment named no sink
    // family for, and inventing one would refuse a target the deployment is
    // perfectly happy with. Three situations reach this and all three want
    // the same answer: unbuilt, never reported, and a catalogue that could
    // not be read.
    expect(acceptsTarget('pdf', 'whatever a pdf target is', CATALOGUE)).toBe(true)
    expect(acceptsTarget('pdf', '  ', CATALOGUE)).toBe(false)
    expect(acceptsTarget('zip', 'anything at all', CATALOGUE)).toBe(true)
    expect(acceptsTarget(FORMAT_MARKDOWN, '../secrets', [])).toBe(true)
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
    expect(Object.keys(emptyDraft(CATALOGUE)).sort()).toEqual([
      'enabled',
      'format',
      'name',
      'target',
    ])
    expect(Object.keys(draftBody(emptyDraft(CATALOGUE))).sort()).toEqual([
      'config',
      'enabled',
      'format',
      'name',
      'target',
    ])
  })
})

describe('a blank draft', () => {
  it('opens on the first format the deployment says it can run', () => {
    // Not on a name this file used to hold. On every deployment shipped so
    // far that is `outline` — what every guild configured before this page
    // existed — but it is that because the API says so.
    expect(emptyDraft(CATALOGUE).format).toBe(FORMAT_OUTLINE)
  })

  it('never opens on a format the deployment cannot run', () => {
    expect(emptyDraft([
      { name: 'pdf', available: false, sink: null },
      { name: FORMAT_HTML, available: true, sink: SINK_OBJECT_STORE },
    ]).format).toBe(FORMAT_HTML)
  })

  it('opens on nothing when the catalogue could not be read', () => {
    // Blank rather than a guess: the reader is then looking at the truth —
    // this console does not currently know what may be chosen — instead of
    // at a default a save would refuse.
    expect(emptyDraft([]).format).toBe('')
    expect(problemFor(draftProblems(emptyDraft([]), [], []), 'format')?.key).toBe(
      'admin.destinations.formatEmpty',
    )
  })
})

describe('what is wrong with a draft', () => {
  it('is happy with a complete one', () => {
    expect(draftProblems(
      { format: FORMAT_OUTLINE, name: 'Wiki', target: 'c0ffee', enabled: true },
      [],
      CATALOGUE,
    )).toEqual([])
    expect(isDraftReady(draftOf(target()), [], CATALOGUE)).toBe(true)
  })

  it('refuses a format the deployment reported it cannot run', () => {
    // The rule that could not be stated before `/api/export-formats`
    // existed. It is the other half of rendering the row at all: the row
    // says PDF is not built, and this stops Save on a draft that names it,
    // with the reason beside the field instead of arriving as a bare 400.
    const problems = draftProblems(
      { format: 'pdf', name: 'Paper', target: 'minutes', enabled: true },
      [],
      CATALOGUE,
    )
    expect(problemFor(problems, 'format')?.key).toBe('admin.destinations.formatUnavailable')
    expect(isDraftReady(
      { format: 'pdf', name: 'Paper', target: 'minutes', enabled: true }, [], CATALOGUE,
    )).toBe(false)
  })

  it('does not refuse a format merely because the catalogue never mentioned it', () => {
    // "This console has not been told about it" is evidence of nothing. An
    // unreadable catalogue must not make every stored destination look like
    // a broken configuration.
    const problems = draftProblems(
      { format: 'zip', name: 'Archive', target: 'minutes', enabled: true },
      [],
      CATALOGUE,
    )
    expect(problemFor(problems, 'format')).toBeNull()
    expect(isDraftReady(draftOf(target({ format: 'zip' })), [], [])).toBe(true)
  })

  it('refuses a name that is blank or only spaces', () => {
    // `_BAD_NAME` in the API: a non-empty string after stripping.
    expect(problemFor(
      draftProblems({ ...emptyDraft(CATALOGUE), target: 'c0ffee' }, [], CATALOGUE),
      'name',
    )?.key).toBe('admin.destinations.nameEmpty')
    const spaces = draftProblems(
      { ...emptyDraft(CATALOGUE), name: '   ', target: 'c0ffee' }, [], CATALOGUE,
    )
    expect(problemFor(spaces, 'name')?.key).toBe('admin.destinations.nameEmpty')
  })

  it('refuses a name this guild already uses', () => {
    // The API answers 409, because a create that upserted would let a typo
    // redirect a guild's protocols with nothing said.
    const problems = draftProblems(
      { format: FORMAT_OUTLINE, name: 'Wiki', target: 'c0ffee', enabled: true },
      ['Wiki', 'Archive'],
      CATALOGUE,
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
    expect(isDraftReady(draftOf(stored), takenNames([stored, others], 4), CATALOGUE)).toBe(true)
  })

  it('names the field each complaint is about', () => {
    // So the page can put it beside that field rather than in a list of
    // grievances at the bottom.
    const problems = draftProblems({ format: '', name: '', target: '', enabled: true }, [], CATALOGUE)
    expect(problems.map((problem) => problem.field)).toEqual(['name', 'format', 'target'])
  })

  it('says the address is wrong in the words of the format that refused it', () => {
    // "Not a collection id" and "not an object prefix" are different
    // sentences, because they are different mistakes.
    const outline = draftProblems(
      { format: FORMAT_OUTLINE, name: 'Wiki', target: 'two words', enabled: true }, [], CATALOGUE,
    )
    expect(problemFor(outline, 'target')?.key).toBe('admin.destinations.targetNotCollection')
    const markdown = draftProblems(
      { format: FORMAT_MARKDOWN, name: 'Bucket', target: '../up', enabled: true }, [], CATALOGUE,
    )
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
    // Every word this console has for a format, and every sentence a sink
    // family carries. Gathered from the catalogue rather than from a list
    // in the module, which is what makes this check follow a format the
    // deployment adds instead of one somebody remembered to add here.
    ...CATALOGUE.flatMap((entry) => {
      const spec = formatSpec(entry.name, CATALOGUE)!
      return [spec.labelKey, spec.noteKey, spec.targetLabelKey, spec.targetHintKey]
    }).filter((key): key is string => key !== null),
    'admin.destinations.formatUnknownDetail',
    'admin.destinations.formatUnavailableDetail',
    'admin.destinations.formatsNote',
    'admin.destinations.formatsUnavailable',
    'admin.destinations.addressLabel',
    'admin.destinations.addressHint',
    enabledLabelKey(target()),
    enabledLabelKey(target({ enabled: false })),
    ...keysIn(targetSummary(target(), CATALOGUE)),
    ...keysIn(targetSummary(target({ format: 'pdf' }), CATALOGUE)),
    ...keysIn(targetSummary(target({ format: 'zip' }), CATALOGUE)),
    ...keysIn(fallbackNote([])),
    ...keysIn(fallbackNote([target()])),
    ...Object.values(secretState(target({ hasSecret: true }))).filter(
      (value): value is string => typeof value === 'string',
    ),
    ...Object.values(secretState(target({ hasSecret: false }))).filter(
      (value): value is string => typeof value === 'string',
    ),
    ...draftProblems({ format: '', name: '', target: '', enabled: true }, [], CATALOGUE)
      .flatMap((problem) => keysIn(problem.message)),
    ...draftProblems(
      { format: FORMAT_OUTLINE, name: 'Wiki', target: 'two words', enabled: true },
      ['Wiki'],
      CATALOGUE,
    ).flatMap((problem) => keysIn(problem.message)),
    ...draftProblems(
      { format: FORMAT_MARKDOWN, name: 'Bucket', target: '../up', enabled: true }, [], CATALOGUE,
    ).flatMap((problem) => keysIn(problem.message)),
    ...draftProblems(
      { format: 'pdf', name: 'Paper', target: 'minutes', enabled: true }, [], CATALOGUE,
    ).flatMap((problem) => keysIn(problem.message)),
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
