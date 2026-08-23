/**
 * What one meeting was published as, and the five ways the two answers can
 * stand relative to each other.
 *
 * The states are the whole point of this module. A recording page that
 * rendered one "Open protocol" link would be telling a reader that a
 * meeting reaching two destinations out of three had reached its
 * destination — and a page that rendered a bare list would be telling them
 * nothing about the one that did not arrive. Neither of those failures is
 * visible in a screenshot of a healthy session, which is why they are
 * checked here rather than looked at.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  hasPublished,
  parseSessionDocuments,
  protocolRow,
  publishedProtocols,
  sessionDocumentsPath,
  type SessionDocument,
} from '../app/utils/sessionDocuments'
import type { Message } from '../app/utils/message'

function messages(locale: 'en' | 'de'): Record<string, unknown> {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

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

function keysIn(message: Message): string[] {
  const found = [message.key]
  for (const value of Object.values(message.params ?? {})) {
    if (typeof value === 'object' && value !== null && 'key' in value) {
      found.push(...keysIn(value as Message))
    }
  }
  return found
}

function document(over: Partial<SessionDocument> = {}): SessionDocument {
  return {
    targetId: 3,
    provider: 'outline',
    url: 'https://outline.example/doc/abc',
    createdAt: '2026-08-20T09:15:00+00:00',
    readable: false,
    mediaType: 'text/markdown; charset=utf-8',
    ...over,
  }
}

const OUTLINE_DOC = document()
const MARKDOWN_DOC = document({
  targetId: 9,
  provider: 'markdown',
  url: '/api/sessions/4711/documents/9',
  readable: true,
})

describe('reading the listing', () => {
  it('keeps a document whose destination has been removed', () => {
    // `target_id` is `ON DELETE SET NULL`, and the row survives on
    // purpose: the document still exists in the other system, and the link
    // is what somebody follows when they go looking for last quarter's
    // minutes.
    const [row] = parseSessionDocuments({
      documents: [{ target_id: null, provider: 'outline', url: 'https://outline.example/x' }],
    })
    expect(row!.targetId).toBeNull()
    expect(protocolRow(row!).orphaned).toBe(true)
  })

  it('drops a row with no URL', () => {
    // This list exists to be clicked, and the console can address neither
    // an Outline document nor a stored one except through the URL the
    // worker recorded.
    expect(parseSessionDocuments({ documents: [{ target_id: 1, provider: 'html' }] })).toEqual([])
    expect(parseSessionDocuments({ documents: [{ target_id: 1, url: '  ' }] })).toEqual([])
  })

  it('keeps a provider it has never heard of', () => {
    // A deployment may publish a format this console does not know, and a
    // document that renders as nothing is worse than one that renders as
    // its own raw name.
    const [row] = parseSessionDocuments({
      documents: [{ target_id: 1, provider: 'zip', url: 'https://example/x.zip' }],
    })
    const rendered = protocolRow(row!)
    expect(rendered.label.key).toBe('recordings.protocolUnknownFormat')
    expect(rendered.label.params?.format).toBe('zip')
  })

  it('names a format it has a word for even where this build cannot publish it', () => {
    // `pdf` is specified and unbuilt, so no destination here can be
    // configured for it — but a document published by a deployment that
    // *did* build it is still a document with a working link, and this page
    // renders what was published rather than what could be published now.
    // That is why it asks `~/utils/exportTargets` for the word alone and
    // never for the catalogue.
    const [row] = parseSessionDocuments({
      documents: [{ target_id: 1, provider: 'pdf', url: 'https://example/x.pdf' }],
    })
    const rendered = protocolRow(row!)
    expect(rendered.label.key).toBe('recordings.protocolFormat')
    expect(rendered.label.params?.format).toEqual({ key: 'common.formatPdf' })
  })

  it('answers an empty list for a payload of the wrong shape', () => {
    for (const payload of [null, undefined, 'no', 7, {}, { documents: 'no' }]) {
      expect(parseSessionDocuments(payload)).toEqual([])
    }
  })
})

describe('one row', () => {
  it('says whether the link stays inside the console', () => {
    // An object-store protocol is served under the session's own rule; an
    // Outline one lives in Outline and the link leaves.
    expect(protocolRow(MARKDOWN_DOC).internal).toBe(true)
    expect(protocolRow(OUTLINE_DOC).internal).toBe(false)
  })

  it('dates it as an instant rather than as a string', () => {
    // `i18n/README.md`: a date is a moment plus the name of a format, and
    // the locale decides the month names and the order of the parts.
    const row = protocolRow(OUTLINE_DOC)
    expect(row.at?.format).toBe('utcMoment')
    expect(row.at?.at.toISOString()).toBe('2026-08-20T09:15:00.000Z')
  })

  it('renders with no date rather than an unreadable one', () => {
    expect(protocolRow(document({ createdAt: 'the other day' })).at).toBeNull()
    expect(protocolRow(document({ createdAt: null })).at).toBeNull()
  })

  it('gives two orphaned documents keys of their own', () => {
    // Postgres treats nulls as distinct in a unique index, so the rows
    // whose destination has been removed accumulate. Two rows keyed alike
    // is a `v-for` that renders one of them.
    const first = protocolRow(document({ targetId: null, url: 'https://a' }))
    const second = protocolRow(document({ targetId: null, url: 'https://b' }))
    expect(first.id).not.toBe(second.id)
  })
})

describe('how the two answers stand', () => {
  it('says nothing was published when nothing claims to have been', () => {
    // A meeting still being transcribed, or a guild that has configured
    // nowhere to publish. An ordinary state and not a failure.
    const view = publishedProtocols(null, [])
    expect(view.state).toBe('none')
    expect(hasPublished(view)).toBe(false)
  })

  it('reads a lone announced link as the destination that records no row', () => {
    // The legacy `document_target` publish writes no `session_document`
    // row at all, so every meeting from before destinations existed is
    // this — and it is not a failure.
    const view = publishedProtocols('https://outline.example/doc/abc', [])
    expect(view.state).toBe('announcedOnly')
    expect(view.announced).toBe('https://outline.example/doc/abc')
    expect(view.note).not.toBeNull()
  })

  it('says nothing extra when the announced link is among the destinations', () => {
    // The healthy case, and the one that must not be decorated with a
    // caveat: a note on every session is a note nobody reads on the one
    // session that has something wrong with it.
    const view = publishedProtocols(OUTLINE_DOC.url, [OUTLINE_DOC, MARKDOWN_DOC])
    expect(view.state).toBe('complete')
    expect(view.note).toBeNull()
    expect(view.summary.params?.count).toBe(2)
  })

  it('reports a publish that reached some destinations and was never announced', () => {
    // **The partial failure, and it is a fact rather than an inference.**
    // `session.document_url` is stamped from the primary destination
    // alone, and `publish_session` records only the destinations that
    // succeeded — so documents with no announced link is exactly "the one
    // Discord announces did not produce a document, and these did".
    const view = publishedProtocols(null, [MARKDOWN_DOC])
    expect(view.state).toBe('announcedMissing')
    expect(view.note?.key).toBe('recordings.protocolsAnnouncedMissing')
    expect(view.rows.map((row) => row.url)).toEqual([MARKDOWN_DOC.url])
  })

  it('says so when the announced link is not one of the listed destinations', () => {
    const view = publishedProtocols('https://outline.example/elsewhere', [MARKDOWN_DOC])
    expect(view.state).toBe('announcedElsewhere')
    expect(view.note?.key).toBe('recordings.protocolsAnnouncedElsewhere')
  })

  it('treats a blank announced link as no announced link', () => {
    // `document_url` arrives as `""` from nothing in particular, and a
    // page that compared it literally would report every session as
    // "announced elsewhere".
    expect(publishedProtocols('   ', [MARKDOWN_DOC]).state).toBe('announcedMissing')
    expect(publishedProtocols('   ', []).state).toBe('none')
  })

  it('never diagnoses a destination, only the disagreement', () => {
    // The retry sweep is still running, and a destination that has not
    // been retried yet looks exactly like one that will never work. The
    // same discipline `~/utils/queue` applies to its derived figures.
    const view = publishedProtocols(null, [MARKDOWN_DOC])
    for (const key of keysIn(view.note!)) {
      expect(key).not.toMatch(/failed|broken|error/i)
    }
  })
})

describe('where the listing lives', () => {
  it('is relative to the API base, and escapes the session id', () => {
    // `useApi` prepends the base, which differs between a server render
    // and the browser. A session id reaches this from the address bar.
    expect(sessionDocumentsPath('4711')).toBe('/sessions/4711/documents')
    expect(sessionDocumentsPath('a/b')).toBe('/sessions/a%2Fb/documents')
  })
})

describe('the sentences this module names', () => {
  const KEYS = [
    ...keysIn(protocolRow(OUTLINE_DOC).label),
    ...keysIn(protocolRow(document({ provider: 'pdf' })).label),
    ...[
      publishedProtocols(null, []),
      publishedProtocols('https://a', []),
      publishedProtocols(OUTLINE_DOC.url, [OUTLINE_DOC]),
      publishedProtocols(null, [MARKDOWN_DOC]),
      publishedProtocols('https://elsewhere', [MARKDOWN_DOC]),
    ].flatMap((view) => [
      ...keysIn(view.summary),
      ...(view.note === null ? [] : keysIn(view.note)),
    ]),
  ]

  it('names every one of them with a key rather than with English', () => {
    for (const key of KEYS) {
      expect(key).toMatch(/^(recordings|common)\.[a-z][A-Za-z]*$/)
    }
  })

  it.each(['en', 'de'])('has a %s translation for every one of them', (locale) => {
    const bundle = locale === 'en' ? EN : DE
    for (const key of KEYS) {
      expect(translated(bundle, key), `${key} is untranslated in ${locale}`).toBe(true)
    }
  })
})
