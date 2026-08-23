/**
 * Everything a dropdown does, decided without a dropdown.
 *
 * A `<select>` gets all of this from the operating system for free, which
 * is why replacing one is so much more work than it looks: the moment the
 * console draws its own list it owes the keyboard everything the platform
 * was doing silently. Enter commits, Escape abandons, the arrows step over
 * rows nobody may choose, Home and End reach the ends, typing a few
 * letters jumps, and focus goes back where it came from afterwards.
 *
 * None of that is visible in a rendered frame, and all of it is a
 * one-character mistake away. So the whole of it is a reducer over a plain
 * object here, and `UiSelect.vue` is the markup that asks it questions.
 */
import { describe, expect, it } from 'vitest'

import type { UiOption } from '../app/utils/uiOption'
import {
  chosenOption,
  initialSelect,
  reduceSelect,
  type SelectState,
} from '../app/utils/uiSelect'

const GUILDS: UiOption[] = [
  { value: '1', label: 'Alpha', detail: '100000000000000001' },
  { value: '2', label: 'Beta', detail: '100000000000000002' },
  { value: '3', label: 'Gamma', detail: '100000000000000003' },
]

const WITH_HOLE: UiOption[] = [
  { value: '1', label: 'Alpha' },
  { value: '2', label: 'Beta', disabled: true },
  { value: '3', label: 'Gamma' },
]

/** A state, reached by pushing events through the reducer rather than by
 *  writing the object out — so the tests below start from states the
 *  control can actually be in. */
function after(
  start: SelectState,
  options: readonly UiOption[],
  ...events: Parameters<typeof reduceSelect>[2][]
): SelectState {
  return events.reduce((state, event) => reduceSelect(state, options, event).state, start)
}

const press = (key: string, at = 0) => ({ kind: 'key', key, at }) as const

describe('a dropdown nobody has touched yet', () => {
  it('is closed, holds whatever it was given, and has no active row', () => {
    expect(initialSelect(null)).toEqual({
      open: false,
      value: null,
      active: -1,
      typed: '',
      typedAt: 0,
    })
  })

  it('remembers the value it was handed', () => {
    expect(initialSelect('2').value).toBe('2')
  })
})

describe('opening it', () => {
  it('starts on the row that is already chosen', () => {
    // Not at the top. Opening a guild switcher on Alpha when the console
    // is showing Gamma is one ArrowDown away from switching servers by
    // accident.
    const open = after(initialSelect('3'), GUILDS, { kind: 'open' })
    expect(open.open).toBe(true)
    expect(open.active).toBe(2)
  })

  it('starts at the top when nothing is chosen yet', () => {
    expect(after(initialSelect(null), GUILDS, { kind: 'open' }).active).toBe(0)
  })

  it('starts on a choosable row when the chosen one has since been disabled', () => {
    // A guild the bot was removed from while the page was open. Landing on
    // it would give Enter nothing to do and no way to say so.
    expect(after(initialSelect('2'), WITH_HOLE, { kind: 'open' }).active).toBe(0)
  })

  it('has no active row at all when there is nothing to choose', () => {
    expect(after(initialSelect(null), [], { kind: 'open' }).active).toBe(-1)
  })

  it('happens on ArrowDown, ArrowUp, Enter and Space from a closed control', () => {
    for (const key of ['ArrowDown', 'ArrowUp', 'Enter', ' ']) {
      const outcome = reduceSelect(initialSelect(null), GUILDS, press(key))
      expect(outcome.state.open, `${key} did not open the list`).toBe(true)
      expect(outcome.handled, `${key} was left to the browser`).toBe(true)
    }
  })

  it('lands at the far end when a closed control is opened with ArrowUp', () => {
    expect(reduceSelect(initialSelect(null), GUILDS, press('ArrowUp')).state.active).toBe(2)
  })

  it('does not open on Escape or Tab, and leaves both to the browser', () => {
    for (const key of ['Escape', 'Tab']) {
      const outcome = reduceSelect(initialSelect(null), GUILDS, press(key))
      expect(outcome.state.open).toBe(false)
      expect(outcome.handled, `${key} was swallowed by a closed dropdown`).toBe(false)
    }
  })
})

describe('walking the open list', () => {
  const open = after(initialSelect('1'), GUILDS, { kind: 'open' })

  it('moves down and up one row at a time', () => {
    expect(after(open, GUILDS, press('ArrowDown')).active).toBe(1)
    expect(after(open, GUILDS, press('ArrowDown'), press('ArrowDown'), press('ArrowUp')).active)
      .toBe(1)
  })

  it('reaches the ends with Home and End', () => {
    expect(after(open, GUILDS, press('End')).active).toBe(2)
    expect(after(open, GUILDS, press('End'), press('Home')).active).toBe(0)
  })

  it('changes nothing about the value while it is only being walked', () => {
    // The whole difference between a listbox and a native `<select>` on
    // some platforms. Moving the highlight is not choosing, so a reader
    // who arrows past three guilds and presses Escape is on the one they
    // started on.
    const walked = after(open, GUILDS, press('ArrowDown'), press('ArrowDown'))
    expect(walked.value).toBe('1')
  })

  it('reports no choice while walking, so nothing is emitted', () => {
    expect(reduceSelect(open, GUILDS, press('ArrowDown')).chosen).toBeUndefined()
  })
})

describe('committing', () => {
  const open = after(initialSelect(null), GUILDS, { kind: 'open' })

  it('takes the active row on Enter, closes, and asks for focus back', () => {
    const outcome = reduceSelect(after(open, GUILDS, press('ArrowDown')), GUILDS, press('Enter'))
    expect(outcome.chosen).toBe('2')
    expect(outcome.state.value).toBe('2')
    expect(outcome.state.open).toBe(false)
    expect(outcome.returnFocus).toBe(true)
  })

  it('takes it on Space too, when nothing is being typed', () => {
    const outcome = reduceSelect(open, GUILDS, press(' '))
    expect(outcome.chosen).toBe('1')
    expect(outcome.state.open).toBe(false)
  })

  it('takes it on a click, wherever the highlight happened to be', () => {
    const outcome = reduceSelect(open, GUILDS, { kind: 'choose', index: 2 })
    expect(outcome.chosen).toBe('3')
    expect(outcome.state.open).toBe(false)
    expect(outcome.returnFocus).toBe(true)
  })

  it('refuses a click on a row nobody may choose, and stays open', () => {
    const held = after(initialSelect(null), WITH_HOLE, { kind: 'open' })
    const outcome = reduceSelect(held, WITH_HOLE, { kind: 'choose', index: 1 })
    expect(outcome.chosen).toBeUndefined()
    expect(outcome.state.open).toBe(true)
  })

  it('reports the same value again when somebody re-picks what was already chosen', () => {
    // `chosen` is "this event settled on a value", not "the value
    // changed". A control that stayed open because the answer had not
    // changed would be a control that cannot be dismissed by agreeing
    // with it.
    const held = after(initialSelect('1'), GUILDS, { kind: 'open' })
    const outcome = reduceSelect(held, GUILDS, { kind: 'choose', index: 0 })
    expect(outcome.chosen).toBe('1')
    expect(outcome.state.open).toBe(false)
  })

  it('closes on Enter with nothing active, without inventing a value', () => {
    const empty = after(initialSelect(null), [], { kind: 'open' })
    const outcome = reduceSelect(empty, [], press('Enter'))
    expect(outcome.chosen).toBeUndefined()
    expect(outcome.state.open).toBe(false)
    expect(outcome.returnFocus).toBe(true)
  })
})

describe('abandoning', () => {
  it('closes on Escape and keeps the value that was there', () => {
    const walked = after(
      initialSelect('1'),
      GUILDS,
      { kind: 'open' },
      press('ArrowDown'),
      press('ArrowDown'),
    )
    const outcome = reduceSelect(walked, GUILDS, press('Escape'))
    expect(outcome.state.open).toBe(false)
    expect(outcome.state.value).toBe('1')
    expect(outcome.chosen).toBeUndefined()
    expect(outcome.returnFocus).toBe(true)
  })

  it('closes on Tab without dragging focus backwards', () => {
    // Tab means "leave". Pulling focus back to the trigger would make the
    // key move focus to the trigger it just left, and the reader would
    // have to press it twice for no reason they can see.
    const open = after(initialSelect('1'), GUILDS, { kind: 'open' })
    const outcome = reduceSelect(open, GUILDS, press('Tab'))
    expect(outcome.state.open).toBe(false)
    expect(outcome.returnFocus).toBe(false)
    expect(outcome.handled).toBe(false)
  })

  it('closes on an outside dismissal without moving focus at all', () => {
    const open = after(initialSelect('1'), GUILDS, { kind: 'open' })
    const outcome = reduceSelect(open, GUILDS, { kind: 'close' })
    expect(outcome.state.open).toBe(false)
    expect(outcome.returnFocus).toBe(false)
  })

  it('forgets the half-typed search, so the next opening starts clean', () => {
    const typed = after(initialSelect(null), GUILDS, { kind: 'open' }, press('g'))
    expect(typed.typed).toBe('g')
    expect(after(typed, GUILDS, press('Escape')).typed).toBe('')
  })
})

describe('typing at it', () => {
  it('jumps to the row that starts with what was typed', () => {
    const outcome = reduceSelect(
      after(initialSelect(null), GUILDS, { kind: 'open' }),
      GUILDS,
      press('b', 100),
    )
    expect(outcome.state.active).toBe(1)
    expect(outcome.handled).toBe(true)
  })

  it('refines rather than restarts while the letters keep coming', () => {
    const state = after(
      initialSelect(null),
      GUILDS,
      { kind: 'open' },
      press('g', 100),
      press('a', 200),
    )
    expect(state.typed).toBe('ga')
    expect(state.active).toBe(2)
  })

  it('starts a new search after a pause', () => {
    const state = after(
      initialSelect(null),
      GUILDS,
      { kind: 'open' },
      press('g', 100),
      press('b', 5000),
    )
    expect(state.typed).toBe('b')
    expect(state.active).toBe(1)
  })

  it('opens a closed dropdown and jumps in one keystroke', () => {
    const outcome = reduceSelect(initialSelect(null), GUILDS, press('g', 100))
    expect(outcome.state.open).toBe(true)
    expect(outcome.state.active).toBe(2)
  })

  it('leaves the highlight where it was when nothing matches', () => {
    // Moving to row zero on a miss would make a typo look like a
    // successful jump.
    const state = after(initialSelect('3'), GUILDS, { kind: 'open' }, press('z', 100))
    expect(state.active).toBe(2)
  })

  it('does not commit anything, however much is typed', () => {
    const state = after(initialSelect('1'), GUILDS, { kind: 'open' }, press('g', 100))
    expect(state.value).toBe('1')
  })

  it('lets Space extend the search once a search is under way', () => {
    const state = after(
      initialSelect(null),
      GUILDS,
      { kind: 'open' },
      press('a', 100),
      press(' ', 200),
    )
    expect(state.typed).toBe('a ')
    expect(state.open).toBe(true)
  })
})

describe('the pointer', () => {
  it('moves the highlight as it moves over the rows', () => {
    const open = after(initialSelect(null), GUILDS, { kind: 'open' })
    expect(after(open, GUILDS, { kind: 'point', index: 2 }).active).toBe(2)
  })

  it('does not move it onto a row nobody may choose', () => {
    const open = after(initialSelect(null), WITH_HOLE, { kind: 'open' })
    expect(after(open, WITH_HOLE, { kind: 'point', index: 1 }).active).toBe(0)
  })
})

describe('the value changing from outside', () => {
  it('is taken without opening or closing anything', () => {
    // A page that resets its own filter, or a guild switcher whose stored
    // choice arrives after hydration. Neither is a reason to open a list
    // in front of somebody.
    const outcome = reduceSelect(initialSelect('1'), GUILDS, { kind: 'sync', value: '3' })
    expect(outcome.state.value).toBe('3')
    expect(outcome.state.open).toBe(false)
    expect(outcome.chosen).toBeUndefined()
  })

  it('moves the highlight of an open list to the new value', () => {
    const open = after(initialSelect('1'), GUILDS, { kind: 'open' })
    expect(after(open, GUILDS, { kind: 'sync', value: '3' }).active).toBe(2)
  })
})

describe('the trigger', () => {
  it('opens a closed list and closes an open one', () => {
    const opened = reduceSelect(initialSelect(null), GUILDS, { kind: 'toggle' })
    expect(opened.state.open).toBe(true)
    const closed = reduceSelect(opened.state, GUILDS, { kind: 'toggle' })
    expect(closed.state.open).toBe(false)
  })

  it('shows the chosen row, and nothing when nothing is chosen', () => {
    expect(chosenOption(GUILDS, '2')?.label).toBe('Beta')
    expect(chosenOption(GUILDS, null)).toBeNull()
    // The state a stored guild id reaches after somebody leaves the
    // server: the value is real and the row is gone, and the trigger has
    // to fall back to its placeholder rather than render an id.
    expect(chosenOption(GUILDS, '9')).toBeNull()
  })
})
