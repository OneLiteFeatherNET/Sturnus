/**
 * A dropdown with two hundred rows in it, and the field that makes it
 * usable.
 *
 * The select's keyboard is not enough once the list is long: nobody scrolls
 * to `#voice-standup-thursday` and nobody types eleven characters of
 * type-ahead blind. So the popup grows a filter field, and with it a set of
 * decisions the select never had to make — what a query matches, where the
 * highlight goes when the list under it changes shape, what the first
 * Escape means when there is a half-typed filter to abandon, and what
 * happens to the query once something has been chosen.
 *
 * Each of those is a way a filtered list can quietly do the wrong thing.
 * A highlight left on row four while the list shrinks to two rows commits
 * whatever is now at row four.
 */
import { describe, expect, it } from 'vitest'

import type { UiOption } from '../app/utils/uiOption'
import {
  type ComboboxState,
  filterOptions,
  filterSummary,
  initialCombobox,
  reduceCombobox,
} from '../app/utils/uiCombobox'

/** A guild's channels, which is the list this control exists for. */
const CHANNELS: UiOption[] = [
  { value: '10', label: 'general', detail: 'Text · 900000000000000010' },
  { value: '11', label: 'standup', detail: 'Voice · 900000000000000011' },
  { value: '12', label: 'standup-notes', detail: 'Text · 900000000000000012' },
  { value: '13', label: 'archive', detail: 'Text · 900000000000000013', disabled: true },
]

function after(
  start: ComboboxState,
  options: readonly UiOption[],
  ...events: Parameters<typeof reduceCombobox>[2][]
): ComboboxState {
  return events.reduce((state, event) => reduceCombobox(state, options, event).state, start)
}

const press = (key: string) => ({ kind: 'key', key }) as const
const type = (query: string) => ({ kind: 'query', query }) as const

describe('what a query matches', () => {
  it('matches anywhere in the label, not only at the start', () => {
    // The difference between a filter and type-ahead. Somebody looking for
    // `standup-notes` types `notes`, because that is the part they
    // remember.
    expect(filterOptions(CHANNELS, 'notes').map((option) => option.value)).toEqual(['12'])
  })

  it('matches the subtext too, so a pasted snowflake finds its row', () => {
    expect(filterOptions(CHANNELS, '900000000000000011').map((o) => o.value)).toEqual(['11'])
  })

  it('does not care about case or surrounding space', () => {
    expect(filterOptions(CHANNELS, '  STANDUP ').map((o) => o.value)).toEqual(['11', '12'])
  })

  it('is the whole list when nothing has been typed', () => {
    expect(filterOptions(CHANNELS, '')).toHaveLength(4)
    expect(filterOptions(CHANNELS, '   ')).toHaveLength(4)
  })

  it('keeps a row nobody may choose when it matches, rather than hiding it', () => {
    // Hiding it would make "there is no #archive any more" the answer to a
    // search that should say "#archive is here and you may not pick it".
    expect(filterOptions(CHANNELS, 'archive').map((o) => o.value)).toEqual(['13'])
  })

  it('is empty when nothing matches, and says so without an exception', () => {
    expect(filterOptions(CHANNELS, 'zzz')).toEqual([])
    expect(filterOptions([], 'anything')).toEqual([])
  })
})

describe('the sentence beside the field', () => {
  it('counts what is left, so a screen reader hears the list change', () => {
    expect(filterSummary(2, 'standup')).toEqual({
      key: 'ui.combobox.matchCount',
      params: { count: 2 },
    })
  })

  it('names the query when nothing matched, because that is the useful half', () => {
    expect(filterSummary(0, 'zzz')).toEqual({
      key: 'ui.combobox.noMatches',
      params: { query: 'zzz' },
    })
  })
})

describe('typing in the field', () => {
  it('opens the list, because a filter with nothing under it says nothing', () => {
    expect(reduceCombobox(initialCombobox(null), CHANNELS, type('st')).state.open).toBe(true)
  })

  it('puts the highlight on the first row that is left', () => {
    const state = after(initialCombobox(null), CHANNELS, { kind: 'open' }, type('standup'))
    expect(state.active).toBe(0)
    expect(filterOptions(CHANNELS, state.query)[state.active]?.value).toBe('11')
  })

  it('takes the highlight off the list entirely when nothing matches', () => {
    // The defect this pins: an index left at 0 over an empty list, and an
    // Enter that reads `options[0]` of nothing.
    const state = after(initialCombobox(null), CHANNELS, type('zzz'))
    expect(state.active).toBe(-1)
  })

  it('does not leave the highlight pointing past the end of a shrinking list', () => {
    // Walk to the third row, then filter down to one. Row three of one row
    // is nothing at all, and committing it would choose whatever happened
    // to be there.
    const state = after(
      initialCombobox(null),
      CHANNELS,
      { kind: 'open' },
      press('ArrowDown'),
      press('ArrowDown'),
      type('notes'),
    )
    expect(state.active).toBe(0)
    expect(filterOptions(CHANNELS, state.query)).toHaveLength(1)
  })

  it('skips straight past a row nobody may choose', () => {
    const state = after(initialCombobox(null), CHANNELS, type('archive'))
    expect(state.active).toBe(-1)
  })
})

describe('walking the filtered list', () => {
  it('steps within what the filter left behind, not within the whole list', () => {
    const state = after(initialCombobox(null), CHANNELS, type('standup'), press('ArrowDown'))
    expect(filterOptions(CHANNELS, state.query)[state.active]?.value).toBe('12')
  })

  it('leaves Home and End to the caret, because the field is a text field', () => {
    // Deliberately different from `UiSelect`. In a combobox the keys that
    // move to the ends of a list are the keys that move to the ends of
    // what has been typed, and taking them would make the filter
    // uneditable by keyboard from the middle.
    const open = after(initialCombobox(null), CHANNELS, { kind: 'open' }, press('ArrowDown'))
    const outcome = reduceCombobox(open, CHANNELS, press('Home'))
    expect(outcome.handled).toBe(false)
    expect(outcome.state.active).toBe(open.active)
  })
})

describe('choosing something', () => {
  it('commits the highlighted row and closes', () => {
    const open = after(initialCombobox(null), CHANNELS, type('standup'))
    const outcome = reduceCombobox(open, CHANNELS, press('Enter'))
    expect(outcome.chosen).toBe('11')
    expect(outcome.state.open).toBe(false)
    expect(outcome.returnFocus).toBe(true)
  })

  it('clears the filter, so the next opening shows the whole list again', () => {
    // A query left behind is a list that opens already narrowed, by a
    // search somebody finished with. They find their channel missing.
    const open = after(initialCombobox(null), CHANNELS, type('standup'))
    expect(reduceCombobox(open, CHANNELS, press('Enter')).state.query).toBe('')
  })

  it('commits nothing on Enter when the filter matched nothing', () => {
    const empty = after(initialCombobox('10'), CHANNELS, type('zzz'))
    const outcome = reduceCombobox(empty, CHANNELS, press('Enter'))
    expect(outcome.chosen).toBeUndefined()
    expect(outcome.state.value).toBe('10')
  })

  it('takes a click on a row of the filtered list', () => {
    const open = after(initialCombobox(null), CHANNELS, type('standup'))
    expect(reduceCombobox(open, CHANNELS, { kind: 'choose', index: 1 }).chosen).toBe('12')
  })

  it('refuses a click on a row nobody may choose', () => {
    const open = after(initialCombobox(null), CHANNELS, type('archive'))
    const outcome = reduceCombobox(open, CHANNELS, { kind: 'choose', index: 0 })
    expect(outcome.chosen).toBeUndefined()
    expect(outcome.state.open).toBe(true)
  })
})

describe('Escape', () => {
  it('clears a filter that has something in it, and keeps the list open', () => {
    // The first Escape undoes the search; the second dismisses the
    // control. Collapsing both into one dismissal makes a mistyped filter
    // cost the whole interaction.
    const open = after(initialCombobox(null), CHANNELS, type('zzz'))
    const outcome = reduceCombobox(open, CHANNELS, press('Escape'))
    expect(outcome.state.query).toBe('')
    expect(outcome.state.open).toBe(true)
    expect(outcome.returnFocus).toBe(false)
  })

  it('closes once there is no filter left to clear', () => {
    const cleared = after(initialCombobox(null), CHANNELS, type('zzz'), press('Escape'))
    const outcome = reduceCombobox(cleared, CHANNELS, press('Escape'))
    expect(outcome.state.open).toBe(false)
    expect(outcome.returnFocus).toBe(true)
  })

  it('puts the highlight back at the top when it clears the filter', () => {
    const open = after(initialCombobox(null), CHANNELS, type('notes'))
    expect(reduceCombobox(open, CHANNELS, press('Escape')).state.active).toBe(0)
  })
})

describe('opening and dismissing', () => {
  it('starts on the chosen row when the list is unfiltered', () => {
    expect(after(initialCombobox('12'), CHANNELS, { kind: 'open' }).active).toBe(2)
  })

  it('drops the filter when it is dismissed from outside', () => {
    const open = after(initialCombobox(null), CHANNELS, type('standup'))
    expect(reduceCombobox(open, CHANNELS, { kind: 'close' }).state.query).toBe('')
  })

  it('closes on Tab without dragging focus backwards', () => {
    const open = after(initialCombobox(null), CHANNELS, { kind: 'open' })
    const outcome = reduceCombobox(open, CHANNELS, press('Tab'))
    expect(outcome.state.open).toBe(false)
    expect(outcome.returnFocus).toBe(false)
    expect(outcome.handled).toBe(false)
  })

  it('takes a value set from outside without opening anything', () => {
    const outcome = reduceCombobox(initialCombobox('10'), CHANNELS, { kind: 'sync', value: '12' })
    expect(outcome.state.value).toBe('12')
    expect(outcome.state.open).toBe(false)
  })
})
