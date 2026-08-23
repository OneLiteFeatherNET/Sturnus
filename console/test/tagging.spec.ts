/**
 * When two chips are one chip.
 *
 * These tests pin the mirror, not the rule. The API decides what a tag is;
 * this module repeats that decision so an editor can recognise a label
 * somebody already has without spending a round trip on it. Every case
 * below is a spelling that must collapse — because the failure they all
 * share is the same one: a person adds a tag they already have, watches
 * nothing happen, and adds it again.
 */
import { describe, expect, it } from 'vitest'

import {
  TAG_MAX_CHARS,
  TAG_MAX_PER_RECORDING,
  normaliseTag,
  sessionTagsPath,
  splitTagInput,
  tagRefusal,
  tagWriteFailed,
  tagsWith,
  tagsWithout,
} from '../app/utils/tagging'

describe('reading a tag somebody typed', () => {
  it('treats a capitalised label as the label it already has', () => {
    expect(normaliseTag('Retro')).toBe('retro')
  })

  it('ignores space around a label, which an input field does not show', () => {
    expect(normaliseTag('  retro  ')).toBe('retro')
  })

  it('collapses inner whitespace, however it was pasted', () => {
    expect(normaliseTag('sprint  planning')).toBe('sprint planning')
  })

  it('reads a decomposed umlaut as the same label as a composed one', () => {
    // Identical in every font, different to every index. Storing both
    // gives a filter that matches half the recordings and looks like it
    // should match all of them.
    expect(normaliseTag('f\u00fchrung')).toBe(normaliseTag('fu\u0308hrung'))
  })

  it('reads a comma as the end of one label and the start of the next', () => {
    expect(splitTagInput('retro, kunde')).toEqual(['retro', 'kunde'])
  })

  it('drops the empty piece a trailing comma leaves behind', () => {
    expect(splitTagInput('retro,')).toEqual(['retro'])
  })

  it('finds no labels in an empty box', () => {
    expect(splitTagInput('   ')).toEqual([])
  })
})

describe('adding a label', () => {
  it('keeps the set alphabetical, which is the order it will be read back in', () => {
    expect(tagsWith(['retro'], 'abschluss')).toEqual(['abschluss', 'retro'])
  })

  it('adds every label a comma separated the input into', () => {
    expect(tagsWith([], 'retro, kunde')).toEqual(['kunde', 'retro'])
  })

  it('reports nothing to do for a label already on the recording', () => {
    // `null` rather than the unchanged set, because that is what decides
    // whether a write is worth making at all.
    expect(tagsWith(['retro'], 'Retro ')).toBeNull()
  })

  it('reports nothing to do for an empty box', () => {
    expect(tagsWith(['retro'], '')).toBeNull()
  })

  it('adds only the half of the input that is new', () => {
    expect(tagsWith(['retro'], 'retro, kunde')).toEqual(['kunde', 'retro'])
  })
})

describe('removing a label', () => {
  it('leaves the rest of the set alone', () => {
    expect(tagsWithout(['kunde', 'retro'], 'retro')).toEqual(['kunde'])
  })

  it('does nothing for a label that is not there', () => {
    expect(tagsWithout(['kunde'], 'retro')).toEqual(['kunde'])
  })

  it('can empty the set, which is how somebody unlabels a recording', () => {
    expect(tagsWithout(['retro'], 'retro')).toEqual([])
  })
})

describe('predicting what the API would refuse', () => {
  it('says nothing about a label that will be accepted', () => {
    expect(tagRefusal([], 'retro')).toBeNull()
  })

  it('says a label is too long before spending a round trip on it', () => {
    // The limit travels with the refusal rather than being written into
    // the sentence here, so the number a reader is told is the same
    // constant the API enforces, in whichever language they are told it.
    expect(tagRefusal([], 'x'.repeat(TAG_MAX_CHARS + 1))).toEqual({
      key: 'recordings.tagTooLong',
      params: { count: TAG_MAX_CHARS },
    })
  })

  it('accepts a label of exactly the greatest allowed length', () => {
    expect(tagRefusal([], 'x'.repeat(TAG_MAX_CHARS))).toBeNull()
  })

  it('says when a recording is already carrying as many labels as it may', () => {
    const full = Array.from({ length: TAG_MAX_PER_RECORDING }, (_, index) => `tag-${index}`)
    expect(tagRefusal(full, 'retro')).toEqual({
      key: 'recordings.tagTooMany',
      params: { count: TAG_MAX_PER_RECORDING },
    })
  })

  it('lets a label that is already on a full recording pass the ceiling', () => {
    // It adds nothing, so it cannot push the set over — the answer is
    // "you already have that", not "you have too many".
    const full = Array.from({ length: TAG_MAX_PER_RECORDING }, (_, index) => `tag-${index}`)
    expect(tagRefusal(full, 'tag-0')).toEqual({ key: 'recordings.tagAlreadyHeld' })
  })

  it('says nothing about an empty box, which is not an attempt at anything', () => {
    expect(tagRefusal([], '  ')).toBeNull()
  })
})

describe('addressing the endpoints', () => {
  it('escapes an id, so that one cannot address a different endpoint', () => {
    expect(sessionTagsPath('7/../9')).toBe('/sessions/7%2F..%2F9/tags')
  })
})

describe('saying that a write failed', () => {
  it('distinguishes a server that was never reached from one that refused', () => {
    // Zero is not a status any server sends, which is the point: "could
    // not reach the API" and "the API said no" need different words.
    expect(tagWriteFailed(0)).toEqual({ key: 'recordings.tagSaveUnreachable' })
    expect(tagWriteFailed(500)).toEqual({ key: 'recordings.tagSaveFailed' })
    expect(tagWriteFailed(0)).not.toEqual(tagWriteFailed(500))
  })

  it('says a recording is no longer theirs rather than that something broke', () => {
    expect(tagWriteFailed(404)).toEqual({ key: 'recordings.tagSaveGone' })
  })
})
