/**
 * One field holding two kinds of thing, and the line between them.
 *
 * A recordings search is `#standup after:2026-08 the bit about the
 * migration`: two tags and a sentence. A field that swallowed all of it as
 * one string would leave the page guessing which parts were tags, and a
 * field that turned all of it into tags would produce a tag called "the".
 * So the value this control emits keeps them apart — a list of chips and
 * the free text that is still free text — and every operation here is
 * about which side of that line something lands on.
 *
 * The failures worth pinning are the quiet ones. A Backspace that deletes
 * a chip when the caret was in the middle of a word. A pasted list that
 * becomes one chip with commas in it. The same tag added twice because it
 * was typed with a capital the second time.
 */
import { describe, expect, it } from 'vitest'

import {
  EMPTY_CHIPS,
  MAX_SUGGESTIONS,
  addChips,
  describeChips,
  normaliseChip,
  reduceChipKey,
  removeChip,
  setText,
  suggestionsFor,
} from '../app/utils/uiChipInput'

const KNOWN = ['standup', 'migration', 'incident', 'retro', 'hiring', 'planning', 'design']

describe('what may become a chip', () => {
  it('is trimmed of the space around it', () => {
    expect(normaliseChip('  standup  ')).toBe('standup')
  })

  it('has its inner whitespace collapsed, so two chips do not merely look alike', () => {
    expect(normaliseChip('sprint   review')).toBe('sprint review')
  })

  it('is nothing when there was nothing but space', () => {
    expect(normaliseChip('   ')).toBeNull()
    expect(normaliseChip('')).toBeNull()
  })
})

describe('adding one', () => {
  it('moves the text across and leaves the field empty', () => {
    expect(addChips(EMPTY_CHIPS, 'standup')).toEqual({ chips: ['standup'], text: '' })
  })

  it('splits a pasted list on its commas', () => {
    // Pasting `standup, migration` is the normal way somebody moves a
    // filter from a chat message into this field, and one chip reading
    // `standup, migration` matches nothing.
    expect(addChips(EMPTY_CHIPS, 'standup, migration').chips).toEqual(['standup', 'migration'])
  })

  it('refuses the same tag twice, whatever case it was typed in', () => {
    const once = addChips(EMPTY_CHIPS, 'Standup')
    expect(addChips(once, 'standup').chips).toEqual(['Standup'])
  })

  it('keeps the spelling of the first one, because that is the one on screen', () => {
    expect(addChips(addChips(EMPTY_CHIPS, 'Standup'), 'STANDUP').chips).toEqual(['Standup'])
  })

  it('adds nothing at all when there is nothing to add', () => {
    expect(addChips(EMPTY_CHIPS, '   ')).toEqual(EMPTY_CHIPS)
  })

  it('clears the field even when the text was a duplicate', () => {
    // Otherwise the word sits there looking unaccepted, and pressing Enter
    // again does nothing at all.
    const once = addChips(EMPTY_CHIPS, 'standup')
    expect(addChips({ ...once, text: 'standup' }, 'standup').text).toBe('')
  })
})

describe('removing one', () => {
  it('takes the named chip out and leaves the rest in order', () => {
    const three = addChips(EMPTY_CHIPS, 'a, b, c')
    expect(removeChip(three, 'b').chips).toEqual(['a', 'c'])
  })

  it('does not care about the case it is named in', () => {
    expect(removeChip(addChips(EMPTY_CHIPS, 'Standup'), 'standup').chips).toEqual([])
  })

  it('leaves the free text alone', () => {
    const held = setText(addChips(EMPTY_CHIPS, 'a'), 'still typing')
    expect(removeChip(held, 'a').text).toBe('still typing')
  })

  it('does nothing for a chip that is not there', () => {
    const one = addChips(EMPTY_CHIPS, 'a')
    expect(removeChip(one, 'z')).toEqual(one)
  })
})

describe('typing into it', () => {
  it('keeps what is typed as free text', () => {
    expect(setText(EMPTY_CHIPS, 'about the migration').text).toBe('about the migration')
  })

  it('turns everything before a comma into chips as the comma is typed', () => {
    const value = setText(EMPTY_CHIPS, 'standup,')
    expect(value.chips).toEqual(['standup'])
    expect(value.text).toBe('')
  })

  it('keeps the tail of a paste as text, because it is not finished yet', () => {
    const value = setText(EMPTY_CHIPS, 'standup, migration, inci')
    expect(value.chips).toEqual(['standup', 'migration'])
    expect(value.text).toBe('inci')
  })
})

describe('the keyboard', () => {
  it('commits what is typed on Enter', () => {
    const outcome = reduceChipKey(setText(EMPTY_CHIPS, 'standup'), 'Enter', false)
    expect(outcome.value.chips).toEqual(['standup'])
    expect(outcome.handled).toBe(true)
  })

  it('leaves Enter alone when there is nothing to commit', () => {
    // Unhandled, so an Enter in an empty field still submits the form the
    // field is sitting in rather than being quietly eaten.
    const outcome = reduceChipKey(EMPTY_CHIPS, 'Enter', true)
    expect(outcome.handled).toBe(false)
  })

  it('removes the last chip on Backspace in an empty field', () => {
    const two = addChips(EMPTY_CHIPS, 'a, b')
    const outcome = reduceChipKey(two, 'Backspace', true)
    expect(outcome.value.chips).toEqual(['a'])
    expect(outcome.handled).toBe(true)
  })

  it('does not touch a chip while there is still text to delete', () => {
    // The defect this exists for: Backspace at the start of a word the
    // reader is still editing silently deleting a tag behind them.
    const held = setText(addChips(EMPTY_CHIPS, 'a'), 'wor')
    const outcome = reduceChipKey(held, 'Backspace', false)
    expect(outcome.value.chips).toEqual(['a'])
    expect(outcome.handled).toBe(false)
  })

  it('does nothing on Backspace when there is no chip left to take', () => {
    expect(reduceChipKey(EMPTY_CHIPS, 'Backspace', true).handled).toBe(false)
  })

  it('clears half-typed text on Escape and keeps the chips', () => {
    const held = setText(addChips(EMPTY_CHIPS, 'a'), 'half')
    const outcome = reduceChipKey(held, 'Escape', false)
    expect(outcome.value).toEqual({ chips: ['a'], text: '' })
    expect(outcome.handled).toBe(true)
  })

  it('leaves Escape alone when there is nothing typed, so a panel can still close', () => {
    expect(reduceChipKey(addChips(EMPTY_CHIPS, 'a'), 'Escape', true).handled).toBe(false)
  })

  it('has no opinion about an ordinary letter', () => {
    expect(reduceChipKey(EMPTY_CHIPS, 'x', false).handled).toBe(false)
  })
})

describe('the suggestions', () => {
  it('offers what has been typed a match for', () => {
    // Anywhere in the word, not only at the start: `hiring` and `planning`
    // both carry an `in`, and somebody who half-remembers a tag is more
    // often right about the middle of it than about its first two letters.
    expect(suggestionsFor(KNOWN, setText(EMPTY_CHIPS, 'in'))).toEqual([
      'incident',
      'hiring',
      'planning',
    ])
  })

  it('never offers a tag that is already a chip', () => {
    const held = setText(addChips(EMPTY_CHIPS, 'incident'), 'in')
    expect(suggestionsFor(KNOWN, held)).toEqual(['hiring', 'planning'])
  })

  it('offers the whole list while nothing has been typed, up to what fits', () => {
    expect(suggestionsFor(KNOWN, EMPTY_CHIPS)).toHaveLength(MAX_SUGGESTIONS)
  })

  it('offers nothing rather than everything when nothing matches', () => {
    expect(suggestionsFor(KNOWN, setText(EMPTY_CHIPS, 'zzz'))).toEqual([])
  })

  it('does not care about case on either side', () => {
    expect(suggestionsFor(['Standup'], setText(EMPTY_CHIPS, 'STAND'))).toEqual(['Standup'])
  })
})

describe('reading the value back', () => {
  it('says the field is empty when it is', () => {
    expect(describeChips(EMPTY_CHIPS)).toEqual({ key: 'ui.chipInput.empty' })
  })

  it('counts the chips when that is all there is', () => {
    expect(describeChips(addChips(EMPTY_CHIPS, 'a, b'))).toEqual({
      key: 'ui.chipInput.chipsOnly',
      params: { count: 2 },
    })
  })

  it('quotes the text when that is all there is', () => {
    expect(describeChips(setText(EMPTY_CHIPS, 'migration'))).toEqual({
      key: 'ui.chipInput.textOnly',
      params: { text: 'migration' },
    })
  })

  it('says both, because the whole point is that they are not the same thing', () => {
    const mixed = setText(addChips(EMPTY_CHIPS, 'a, b'), 'migration')
    expect(describeChips(mixed)).toEqual({
      key: 'ui.chipInput.chipsAndText',
      params: { count: 2, text: 'migration' },
    })
  })

  it('does not count trailing space as free text', () => {
    expect(describeChips(setText(addChips(EMPTY_CHIPS, 'a'), '  '))).toEqual({
      key: 'ui.chipInput.chipsOnly',
      params: { count: 1 },
    })
  })
})
