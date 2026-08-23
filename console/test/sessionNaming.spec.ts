/**
 * Naming a meeting, and the one bug this module exists to make
 * unwritable.
 *
 * `PUT /api/sessions/{id}/name` is a **replace**. A body carrying only a
 * title stores `null` over the description, deliberately and correctly —
 * absent and null are the same fact, and the endpoint chose one spelling
 * for it. The failure that follows from that is a form which saves a title
 * and silently deletes the paragraph underneath it, for everybody who was
 * in the meeting, with no history to get it back from.
 *
 * It is the kind of bug that passes review, because the code that causes
 * it reads like care: *only send what changed*. So the module is arranged
 * so that no such body can be built, and the first block below is the
 * check that keeps it that way.
 */
import { describe, expect, it } from 'vitest'

import {
  NAME_MAX_DESCRIPTION_CHARS,
  NAME_MAX_TITLE_CHARS,
  nameBodyFrom,
  nameDraftFrom,
  nameIsDirty,
  nameRefusal,
  nameWriteFailed,
  sessionNamePath,
} from '../app/utils/sessionNaming'

describe('the body a save sends', () => {
  it('always carries both members', () => {
    // The whole point. A `PUT` is the name the meeting will have
    // afterwards, and a member left out of it is a member cleared.
    const body = nameBodyFrom({ title: 'Sprint 34 planning', description: '' })
    expect(Object.keys(body).sort()).toEqual(['description', 'title'])
  })

  it('carries both members even when neither was ever typed', () => {
    const body = nameBodyFrom({ title: '', description: '' })
    expect(body).toEqual({ title: null, description: null })
    expect('title' in body && 'description' in body).toBe(true)
  })

  it('sends a description untouched when only the title changed', () => {
    // The shape of the bug: somebody fixes a typo in the name, and what
    // four colleagues wrote about the meeting has to survive it.
    expect(nameBodyFrom({ title: 'Sprint 34', description: 'we agreed to split it' })).toEqual({
      title: 'Sprint 34',
      description: 'we agreed to split it',
    })
  })

  it('spells an empty field as null rather than as an empty string', () => {
    // One spelling for "nobody has named this", which is the decision
    // `sturnus.console.naming` took in the column.
    expect(nameBodyFrom({ title: '   ', description: '\n\n' })).toEqual({
      title: null,
      description: null,
    })
  })

  it('collapses a title to one line', () => {
    // A title is rendered in a heading, a list row and a browser tab, none
    // of which has a second line. A newline in one is a paste accident.
    expect(nameBodyFrom({ title: '  Sprint\n 34   planning ', description: '' }).title).toBe(
      'Sprint 34 planning',
    )
  })

  it('keeps the line breaks in a description', () => {
    // They are the shape of the text. Collapsing them turns an agenda into
    // a run-on sentence.
    expect(
      nameBodyFrom({ title: '', description: 'Agenda\n\n- one\n- two\n' }).description,
    ).toBe('Agenda\n\n- one\n- two')
  })

  it('normalises the line endings a textarea submits', () => {
    // Every browser posts `\r\n` from a `<textarea>`, so keeping them
    // would make the same paragraph typed on two machines two strings.
    expect(nameBodyFrom({ title: '', description: 'one\r\ntwo\rthree' }).description).toBe(
      'one\ntwo\nthree',
    )
  })

  it('composes what a keyboard decomposed', () => {
    // NFC, so a composed and a decomposed umlaut are stored as one string
    // rather than as whichever keyboard produced them.
    expect(nameBodyFrom({ title: 'Übergabe', description: '' }).title).toBe('Übergabe')
  })
})

describe('the pair a form starts from', () => {
  it('is what is stored', () => {
    expect(nameDraftFrom({ title: 'Retro', description: 'went long' })).toEqual({
      title: 'Retro',
      description: 'went long',
    })
  })

  it('turns an unnamed meeting into two empty boxes', () => {
    // An `<input>` holds `''` and never `null`; a draft admitting both
    // would restore the two spellings the API removed.
    expect(nameDraftFrom({ title: null, description: null })).toEqual({
      title: '',
      description: '',
    })
    expect(nameDraftFrom(null)).toEqual({ title: '', description: '' })
    expect(nameDraftFrom(undefined)).toEqual({ title: '', description: '' })
  })
})

describe('whether there is anything to save', () => {
  const stored = { title: 'Sprint 34', description: 'we agreed to split it' }

  it('is false for the text that is already there', () => {
    expect(nameIsDirty(stored, nameDraftFrom(stored))).toBe(false)
  })

  it('is false for a stray space the keyboard added', () => {
    // Compared after normalisation, so a Save button that is available
    // means there is something to save.
    expect(nameIsDirty(stored, { title: ' Sprint  34 ', description: stored.description })).toBe(
      false,
    )
  })

  it('is true when the title changed', () => {
    expect(nameIsDirty(stored, { title: 'Sprint 35', description: stored.description })).toBe(true)
  })

  it('is true when the description was emptied', () => {
    // Clearing is a change and has to be savable. An editor that could not
    // express "there is nothing to say about this" would leave whatever
    // the first person wrote in place for good.
    expect(nameIsDirty(stored, { title: stored.title, description: '' })).toBe(true)
  })

  it('is true when a meeting is named for the first time', () => {
    expect(nameIsDirty({ title: null, description: null }, { title: 'Retro', description: '' })).toBe(
      true,
    )
  })
})

describe('why a save is refused before it is spent', () => {
  it('says nothing about ordinary text', () => {
    expect(nameRefusal({ title: 'Sprint 34', description: 'short' })).toBeNull()
  })

  it('refuses a title past the ceiling the API enforces', () => {
    const refusal = nameRefusal({ title: 'x'.repeat(NAME_MAX_TITLE_CHARS + 1), description: '' })
    expect(refusal).toEqual({
      key: 'recordings.nameTitleTooLong',
      params: { count: NAME_MAX_TITLE_CHARS },
    })
  })

  it('measures the text that would be sent, not the text in the box', () => {
    // A title padded with spaces is shorter by the time it arrives, and
    // refusing it would be this console disagreeing with the API about a
    // rule it copied from it.
    const padded = `${'x'.repeat(NAME_MAX_TITLE_CHARS)}${'   '}`
    expect(nameRefusal({ title: padded, description: '' })).toBeNull()
  })

  it('refuses a description past its ceiling', () => {
    expect(
      nameRefusal({ title: '', description: 'y'.repeat(NAME_MAX_DESCRIPTION_CHARS + 1) }),
    ).toEqual({
      key: 'recordings.nameDescriptionTooLong',
      params: { count: NAME_MAX_DESCRIPTION_CHARS },
    })
  })

  it('allows exactly the ceiling', () => {
    expect(nameRefusal({ title: 'x'.repeat(NAME_MAX_TITLE_CHARS), description: '' })).toBeNull()
    expect(
      nameRefusal({ title: '', description: 'y'.repeat(NAME_MAX_DESCRIPTION_CHARS) }),
    ).toBeNull()
  })
})

describe('what a failed save says', () => {
  it('tells an unreachable server from a server that said no', () => {
    // "Could not reach the API" and "the API refused" need different
    // words: one of them is worth trying again immediately.
    expect(nameWriteFailed(0)).toEqual({ key: 'recordings.nameSaveUnreachable' })
  })

  it('has its own sentence for a session that is no longer theirs', () => {
    expect(nameWriteFailed(404)).toEqual({ key: 'recordings.nameSaveGone' })
  })

  it('has its own sentence for text the server would not store', () => {
    // A 400 here is text that looks perfectly ordinary — a control
    // character survives a paste invisibly — and "it could not be saved"
    // would leave somebody re-typing a title that was never the problem.
    expect(nameWriteFailed(400)).toEqual({ key: 'recordings.nameSaveRefused' })
  })

  it('falls back to one sentence for everything else', () => {
    expect(nameWriteFailed(500)).toEqual({ key: 'recordings.nameSaveFailed' })
  })
})

describe('where a name is written', () => {
  it('is under the session it belongs to', () => {
    expect(sessionNamePath('4711')).toBe('/sessions/4711/name')
  })

  it('escapes the id, because a slash in one addresses a different endpoint', () => {
    expect(sessionNamePath('4711/../../queue')).toBe('/sessions/4711%2F..%2F..%2Fqueue/name')
  })
})
