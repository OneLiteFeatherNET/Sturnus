/**
 * The dropdown for a list nobody can scroll.
 *
 * A guild has two hundred channels. `UiSelect`'s keyboard is a good
 * keyboard and it does not help here: type-ahead matches prefixes, and
 * nobody remembers that the channel they want begins with `voice-` rather
 * than containing `standup`. So the popup grows a filter field, and the
 * control grows the four decisions a filtered list needs and a plain one
 * does not.
 *
 * **The filter lives inside the popup, under the same trigger button
 * `UiSelect` has.** The other shape — the trigger *is* the text field —
 * looks tidier and is a trap: the field then has to show a label when
 * closed and a query when open, so the value somebody sees is sometimes
 * the chosen thing and sometimes what they were typing, and clicking away
 * mid-search leaves text that means nothing sitting in a field that looks
 * committed. One trigger that always shows the chosen row, and a field
 * that only exists while the list is open, has neither problem.
 *
 * There is deliberately **no type-ahead** here: the query *is* the search,
 * and a second, invisible one racing it would move the highlight for
 * reasons nobody could see.
 */
import type { Message } from './message'
import {
  type UiOption,
  firstEnabled,
  indexOfValue,
  stepEnabled,
} from './uiOption'

export interface ComboboxState {
  open: boolean
  value: string | null
  /** What has been typed into the filter. Never part of the value. */
  query: string
  /** The highlighted row **within the filtered list**, or -1. Indices into
   *  the whole list would be meaningless the moment a filter is applied. */
  active: number
}

export type ComboboxEvent =
  | { kind: 'toggle' }
  | { kind: 'open' }
  | { kind: 'close' }
  | { kind: 'query', query: string }
  | { kind: 'point', index: number }
  | { kind: 'choose', index: number }
  | { kind: 'sync', value: string | null }
  | { kind: 'key', key: string }

export interface ComboboxOutcome {
  state: ComboboxState
  chosen?: string | null
  returnFocus: boolean
  handled: boolean
}

export function initialCombobox(value: string | null): ComboboxState {
  return { open: false, value, query: '', active: -1 }
}

/**
 * The rows a query leaves behind.
 *
 * Substring rather than prefix, over the label **and** the subtext. The
 * subtext is where the Discord id lives, and pasting an id somebody copied
 * out of a log is exactly how these pickers get used in an incident.
 *
 * A disabled row that matches stays in the list. Dropping it would answer
 * "where is #archive?" with "there is no #archive", when the truth is that
 * it is right there and may not be chosen.
 */
export function filterOptions(options: readonly UiOption[], query: string): UiOption[] {
  const needle = query.trim().toLowerCase()
  if (needle === '') return [...options]
  return options.filter(
    (option) =>
      option.label.toLowerCase().includes(needle)
      || (option.detail ?? '').toLowerCase().includes(needle),
  )
}

/**
 * What the live region says after a filter runs.
 *
 * A list shrinking from two hundred rows to two is the most useful thing
 * that happens in this control and the only one a sighted reader gets for
 * free. The empty case names the query rather than counting to zero,
 * because "nothing matches `stanup`" is a sentence somebody can act on and
 * "0 results" is not.
 */
export function filterSummary(count: number, query: string): Message {
  return count === 0
    ? { key: 'ui.combobox.noMatches', params: { query } }
    : { key: 'ui.combobox.matchCount', params: { count } }
}

function highlightFor(visible: readonly UiOption[], value: string | null): number {
  const at = indexOfValue(visible, value)
  if (at >= 0 && visible[at]?.disabled !== true) return at
  return firstEnabled(visible)
}

function settle(
  state: ComboboxState,
  visible: readonly UiOption[],
  index: number,
): ComboboxOutcome {
  const option = visible[index]
  if (!option || option.disabled === true) {
    return { state, returnFocus: false, handled: true }
  }
  return {
    // The query goes with the choice. A filter left behind means the next
    // opening shows a list already narrowed by a search somebody finished
    // with, and the row they want appears to have vanished.
    state: { ...state, open: false, value: option.value, query: '', active: -1 },
    chosen: option.value,
    returnFocus: true,
    handled: true,
  }
}

export function reduceCombobox(
  state: ComboboxState,
  options: readonly UiOption[],
  event: ComboboxEvent,
): ComboboxOutcome {
  const visible = filterOptions(options, state.query)

  switch (event.kind) {
    case 'open':
      return {
        state: { ...state, open: true, active: highlightFor(visible, state.value) },
        returnFocus: false,
        handled: true,
      }

    case 'close':
      return {
        state: { ...state, open: false, query: '', active: -1 },
        returnFocus: false,
        handled: true,
      }

    case 'toggle':
      return state.open
        ? { state: { ...state, open: false, query: '', active: -1 }, returnFocus: false, handled: true }
        : {
            state: { ...state, open: true, active: highlightFor(visible, state.value) },
            returnFocus: false,
            handled: true,
          }

    case 'query': {
      const next = filterOptions(options, event.query)
      return {
        // The highlight is recomputed from the *new* list rather than
        // carried across. Carrying it is how an index of four survives a
        // filter down to two rows and commits whatever is now at four.
        state: { ...state, open: true, query: event.query, active: firstEnabled(next) },
        returnFocus: false,
        handled: true,
      }
    }

    case 'point': {
      const option = visible[event.index]
      if (!option || option.disabled === true) return { state, returnFocus: false, handled: true }
      return { state: { ...state, active: event.index }, returnFocus: false, handled: true }
    }

    case 'choose':
      return settle(state, visible, event.index)

    case 'sync':
      return {
        state: {
          ...state,
          value: event.value,
          active: state.open ? highlightFor(visible, event.value) : state.active,
        },
        returnFocus: false,
        handled: true,
      }

    case 'key':
      return reduceKey(state, options, visible, event.key)
  }
}

function reduceKey(
  state: ComboboxState,
  options: readonly UiOption[],
  visible: readonly UiOption[],
  key: string,
): ComboboxOutcome {
  if (!state.open) {
    if (key === 'ArrowDown' || key === 'ArrowUp' || key === 'Enter' || key === ' ') {
      return {
        state: { ...state, open: true, active: highlightFor(visible, state.value) },
        returnFocus: false,
        handled: true,
      }
    }
    return { state, returnFocus: false, handled: false }
  }

  switch (key) {
    case 'ArrowDown':
      return {
        state: { ...state, active: stepEnabled(visible, state.active, 1) },
        returnFocus: false,
        handled: true,
      }
    case 'ArrowUp':
      return {
        state: { ...state, active: stepEnabled(visible, state.active, -1) },
        returnFocus: false,
        handled: true,
      }
    case 'Enter':
      return settle(state, visible, state.active)
    case 'Escape':
      // The first Escape abandons the search; the second abandons the
      // control. Doing both at once makes one mistyped letter cost the
      // whole interaction.
      if (state.query !== '') {
        return {
          state: { ...state, query: '', active: firstEnabled(filterOptions(options, '')) },
          returnFocus: false,
          handled: true,
        }
      }
      return {
        state: { ...state, open: false, active: -1 },
        returnFocus: true,
        handled: true,
      }
    case 'Tab':
      return {
        state: { ...state, open: false, query: '', active: -1 },
        returnFocus: false,
        handled: false,
      }
    default:
      // Home, End, the arrows that move a caret, and every letter. All of
      // them belong to the text field: this control's list is walked with
      // the vertical arrows alone, and taking Home would make the filter
      // uneditable from the middle by keyboard.
      return { state, returnFocus: false, handled: false }
  }
}
