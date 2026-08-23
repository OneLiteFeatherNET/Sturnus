/**
 * The list every dropdown in this console is made of, and the four
 * questions asked of it.
 *
 * Which option is first, which is last, which one an arrow key reaches
 * next, and which one somebody who typed three letters meant. None of
 * those needs a document to answer and every one of them is wrong in a way
 * a screenshot cannot show: an arrow key that walks onto a disabled row
 * looks exactly like one that skipped it until the reader presses Enter.
 *
 * `UiSelect` and `UiCombobox` both ask these functions rather than each
 * keeping its own answer, which is the entire reason the module exists
 * separately from either of them.
 */
import { describe, expect, it } from 'vitest'

import {
  TYPE_AHEAD_GAP_MS,
  type UiOption,
  enabledCount,
  firstEnabled,
  indexOfValue,
  lastEnabled,
  matchTypeAhead,
  optionDomId,
  stepEnabled,
  typeAheadBuffer,
} from '../app/utils/uiOption'

/** A guild list, which is what most of these dropdowns actually hold: a
 *  name somebody chose and a snowflake underneath it. */
const GUILDS: UiOption[] = [
  { value: '1', label: 'Alpha', detail: '100000000000000001' },
  { value: '2', label: 'Beta', detail: '100000000000000002' },
  { value: '3', label: 'Gamma', detail: '100000000000000003' },
]

/** The same list with a middle row nobody may choose — a guild the bot has
 *  been removed from, say. */
const WITH_HOLE: UiOption[] = [
  { value: '1', label: 'Alpha' },
  { value: '2', label: 'Beta', disabled: true },
  { value: '3', label: 'Gamma' },
]

const ALL_DISABLED: UiOption[] = [
  { value: '1', label: 'Alpha', disabled: true },
  { value: '2', label: 'Beta', disabled: true },
]

describe('finding a value in the list', () => {
  it('reports where it is', () => {
    expect(indexOfValue(GUILDS, '2')).toBe(1)
  })

  it('reports -1 for a value the list no longer has', () => {
    // The state after a guild is left while its id is still in
    // `localStorage`. It has to read as "nothing is chosen" rather than as
    // index 0, which would silently switch servers under somebody.
    expect(indexOfValue(GUILDS, '9')).toBe(-1)
    expect(indexOfValue(GUILDS, null)).toBe(-1)
  })

  it('reports -1 for an empty list, without reading past the end', () => {
    expect(indexOfValue([], '1')).toBe(-1)
  })
})

describe('the ends of the list', () => {
  it('are the first and last options when every row is choosable', () => {
    expect(firstEnabled(GUILDS)).toBe(0)
    expect(lastEnabled(GUILDS)).toBe(2)
  })

  it('skip a disabled row at either end', () => {
    const edged: UiOption[] = [
      { value: '0', label: 'Zero', disabled: true },
      ...WITH_HOLE,
      { value: '4', label: 'Delta', disabled: true },
    ]
    expect(firstEnabled(edged)).toBe(1)
    expect(lastEnabled(edged)).toBe(3)
  })

  it('are nowhere when nothing in the list may be chosen', () => {
    // Not 0. A list of disabled options has no active option at all, and
    // Home landing on one would put `aria-activedescendant` on a row the
    // reader cannot act on.
    expect(firstEnabled(ALL_DISABLED)).toBe(-1)
    expect(lastEnabled(ALL_DISABLED)).toBe(-1)
  })

  it('are nowhere in an empty list', () => {
    expect(firstEnabled([])).toBe(-1)
    expect(lastEnabled([])).toBe(-1)
  })

  it('are the same place in a list of one', () => {
    expect(firstEnabled([{ value: '1', label: 'Only' }])).toBe(0)
    expect(lastEnabled([{ value: '1', label: 'Only' }])).toBe(0)
  })
})

describe('walking the list with an arrow key', () => {
  it('moves one choosable row at a time', () => {
    expect(stepEnabled(GUILDS, 0, 1)).toBe(1)
    expect(stepEnabled(GUILDS, 2, -1)).toBe(1)
  })

  it('steps over a row nobody may choose rather than landing on it', () => {
    // The defect worth a test: an active index on a disabled option looks
    // identical to one on an enabled option, and only Enter tells you.
    expect(stepEnabled(WITH_HOLE, 0, 1)).toBe(2)
    expect(stepEnabled(WITH_HOLE, 2, -1)).toBe(0)
  })

  it('stops at the end rather than wrapping round to the other one', () => {
    // Deliberate. A list that wraps takes somebody holding ArrowDown from
    // the bottom of a long list straight back to the top, and they do not
    // see it happen — the rows under the cursor look the same either way.
    // Home and End are how the ends are reached on purpose.
    expect(stepEnabled(GUILDS, 2, 1)).toBe(2)
    expect(stepEnabled(GUILDS, 0, -1)).toBe(0)
  })

  it('lands on the near end when nothing is active yet', () => {
    // ArrowDown into a closed dropdown means "start at the top"; ArrowUp
    // means "start at the bottom". Both arrive here as a step from -1.
    expect(stepEnabled(GUILDS, -1, 1)).toBe(0)
    expect(stepEnabled(GUILDS, -1, -1)).toBe(2)
  })

  it('goes nowhere in a list with nothing to walk', () => {
    expect(stepEnabled([], -1, 1)).toBe(-1)
    expect(stepEnabled(ALL_DISABLED, -1, 1)).toBe(-1)
    expect(stepEnabled(ALL_DISABLED, 0, 1)).toBe(-1)
  })

  it('treats an index off the end of the list as no index at all', () => {
    // A list that shrank underneath a held-open dropdown. Falling back to
    // the near end is a defined answer; reading `options[7]` is not.
    expect(stepEnabled(GUILDS, 7, 1)).toBe(0)
    expect(stepEnabled(GUILDS, -4, -1)).toBe(2)
  })

  it('counts how many rows there are to walk at all', () => {
    expect(enabledCount(GUILDS)).toBe(3)
    expect(enabledCount(WITH_HOLE)).toBe(2)
    expect(enabledCount(ALL_DISABLED)).toBe(0)
  })
})

describe('the type-ahead buffer', () => {
  it('grows while somebody is still typing', () => {
    expect(typeAheadBuffer('g', 'a', 120)).toBe('ga')
  })

  it('starts again after a pause, so the next word is not glued to the last', () => {
    // Without this, coming back to a dropdown ten seconds later and typing
    // `b` searches for `gab` and finds nothing — and the reader has no way
    // to know why.
    expect(typeAheadBuffer('ga', 'b', TYPE_AHEAD_GAP_MS + 1)).toBe('b')
  })

  it('keeps the buffer at exactly the gap, and drops it just past', () => {
    expect(typeAheadBuffer('ga', 'b', TYPE_AHEAD_GAP_MS)).toBe('gab')
  })

  it('ignores a key that is not a character', () => {
    expect(typeAheadBuffer('ga', 'ArrowDown', 10)).toBe('ga')
    expect(typeAheadBuffer('ga', 'Enter', 10)).toBe('ga')
  })

  it('takes a space only once there is something to add it to', () => {
    // A leading space is nobody's search term, and Space on a closed
    // dropdown means "open", so it must not start a buffer.
    expect(typeAheadBuffer('', ' ', 10)).toBe('')
    expect(typeAheadBuffer('new', ' ', 10)).toBe('new ')
  })
})

describe('what somebody who typed a few letters meant', () => {
  it('finds the row whose label starts with them', () => {
    expect(matchTypeAhead(GUILDS, 'be', 0)).toBe(1)
  })

  it('does not care about case', () => {
    expect(matchTypeAhead(GUILDS, 'GA', 0)).toBe(2)
  })

  it('searches from where the cursor already is, so more letters refine', () => {
    // Typing `g`, then `a`: the second keystroke has to keep the same row
    // rather than restart the search and hop somewhere else.
    expect(matchTypeAhead(GUILDS, 'g', 0)).toBe(2)
    expect(matchTypeAhead(GUILDS, 'ga', 2)).toBe(2)
  })

  it('cycles through the rows sharing a letter when that letter is repeated', () => {
    // The other half of the convention: `a`, `a`, `a` walks every option
    // beginning with A rather than sitting on the first one.
    const many: UiOption[] = [
      { value: '1', label: 'Alpha' },
      { value: '2', label: 'Anchor' },
      { value: '3', label: 'Beta' },
    ]
    expect(matchTypeAhead(many, 'a', -1)).toBe(0)
    expect(matchTypeAhead(many, 'aa', 0)).toBe(1)
    expect(matchTypeAhead(many, 'aaa', 1)).toBe(0)
  })

  it('wraps round the end of the list, because a search is not a walk', () => {
    expect(matchTypeAhead(GUILDS, 'a', 2)).toBe(0)
  })

  it('never lands on a row nobody may choose', () => {
    expect(matchTypeAhead(WITH_HOLE, 'b', -1)).toBe(-1)
  })

  it('finds nothing rather than guessing', () => {
    expect(matchTypeAhead(GUILDS, 'zz', 0)).toBe(-1)
    expect(matchTypeAhead(GUILDS, '', 0)).toBe(-1)
    expect(matchTypeAhead([], 'a', 0)).toBe(-1)
  })

  it('matches the label and not the subtext', () => {
    // The subtext is a Discord id. Typing `1` to jump to a snowflake is
    // not a thing anybody does, and letting it match would make every
    // option in a guild list respond to every digit.
    expect(matchTypeAhead(GUILDS, '10000', -1)).toBe(-1)
  })
})

describe('the id an option is addressed by', () => {
  it('is built from the control and the position, never from the value', () => {
    // `aria-activedescendant` points at an id, and a Discord snowflake or
    // a channel name is not one. Positions are, and two options cannot
    // share a position the way two values can sanitise to one string.
    expect(optionDomId('picker', 0)).toBe('picker-option-0')
    expect(optionDomId('picker', 12)).toBe('picker-option-12')
  })

  it('is nothing at all when nothing is active', () => {
    // An empty `aria-activedescendant` is a broken pointer; the attribute
    // has to be absent instead, and `undefined` is how a template does
    // that.
    expect(optionDomId('picker', -1)).toBeUndefined()
  })
})
