/**
 * A dropdown, decided.
 *
 * The console has hand-rolled a `<select>` on six pages. A native one is
 * fine until it has to render a Discord snowflake under a guild name, or
 * carry this console's own palette, or be anything other than what the
 * operating system draws — and at that point the whole of what the
 * platform was doing silently becomes ours to write: Enter commits,
 * Escape abandons, the arrows step over rows nobody may choose, Home and
 * End reach the ends, typing jumps, and focus goes back where it came
 * from.
 *
 * All of that is here, as a reducer over a plain object, and none of it is
 * in `UiSelect.vue`. The split is not tidiness. Every property above is
 * invisible in a rendered frame — an active index on a disabled row looks
 * exactly like one on a choosable row — so a test that had to mount
 * something to check them would be checking the markup and trusting the
 * behaviour, which is the wrong way round.
 *
 * The moving-around arithmetic is `~/utils/uiOption`, shared with the
 * combobox. What is added here is the part a select has and a plain list
 * does not: an open state, and the difference between highlighting a row
 * and choosing it.
 */
import {
  type UiOption,
  firstEnabled,
  indexOfValue,
  lastEnabled,
  matchTypeAhead,
  stepEnabled,
  typeAheadBuffer,
} from './uiOption'

export interface SelectState {
  open: boolean
  /** What the control holds. `null` is "nothing chosen", which is what a
   *  placeholder renders for. */
  value: string | null
  /** The highlighted row, or -1 for none. Not the same thing as the
   *  value: walking the list moves this and leaves the value alone. */
  active: number
  /** The type-ahead prefix currently being searched for. */
  typed: string
  /** When the last character of it was typed, in milliseconds. Passed in
   *  by the component so the clock stays out of this module. */
  typedAt: number
}

export type SelectEvent =
  /** The trigger was pressed. */
  | { kind: 'toggle' }
  /** Opened without a toggle — the down-arrow on a closed trigger. */
  | { kind: 'open' }
  /** Dismissed from outside: a click elsewhere, a scroll, a route change.
   *  Focus is deliberately left wherever the dismissal put it. */
  | { kind: 'close' }
  /** The pointer moved over a row. */
  | { kind: 'point', index: number }
  /** A row was clicked. */
  | { kind: 'choose', index: number }
  /** The bound value changed from outside the control. */
  | { kind: 'sync', value: string | null }
  /** A key, and the moment it arrived. */
  | { kind: 'key', key: string, at: number }

export interface SelectOutcome {
  state: SelectState
  /**
   * The value this event settled on, if it settled on one.
   *
   * "Settled on", not "changed to": re-choosing the row that was already
   * chosen reports it again. A control that only reported changes would
   * have to stay open when somebody agreed with it, which is not a thing
   * a dropdown may do.
   */
  chosen?: string | null
  /** Whether focus belongs back on the trigger. False for Tab, which is
   *  already on its way somewhere, and for an outside dismissal, which has
   *  put focus somewhere on purpose. */
  returnFocus: boolean
  /** Whether the component should call `preventDefault`. False for the
   *  keys this control has no opinion about, so they keep working. */
  handled: boolean
}

export function initialSelect(value: string | null): SelectState {
  return { open: false, value, active: -1, typed: '', typedAt: 0 }
}

/**
 * Where the highlight goes when the list opens.
 *
 * On the chosen row, so that opening a guild switcher and pressing Enter
 * changes nothing. Falling back to the first choosable row covers the two
 * states where that row is not available: nothing chosen yet, and a value
 * whose row has since been disabled — a guild the bot was removed from
 * while the page was open.
 */
function resolveActive(options: readonly UiOption[], value: string | null): number {
  const at = indexOfValue(options, value)
  if (at >= 0 && options[at]?.disabled !== true) return at
  return firstEnabled(options)
}

/** The row a value names, or `null` — including when the value is real and
 *  the row is gone, which is what a stored guild id looks like after
 *  somebody leaves the server. */
export function chosenOption(
  options: readonly UiOption[],
  value: string | null,
): UiOption | null {
  const at = indexOfValue(options, value)
  return at < 0 ? null : (options[at] ?? null)
}

const PRINTABLE = (key: string) => key.length === 1

function settle(state: SelectState, options: readonly UiOption[], index: number): SelectOutcome {
  const option = options[index]
  if (!option || option.disabled === true) {
    // Nothing to settle on. Enter with an empty list closes rather than
    // sitting there, because a list with nothing in it has already said
    // everything it has to say.
    return {
      state: { ...state, open: false, typed: '', typedAt: 0 },
      returnFocus: true,
      handled: true,
    }
  }
  return {
    state: { ...state, open: false, value: option.value, typed: '', typedAt: 0 },
    chosen: option.value,
    returnFocus: true,
    handled: true,
  }
}

export function reduceSelect(
  state: SelectState,
  options: readonly UiOption[],
  event: SelectEvent,
): SelectOutcome {
  const unchanged: SelectOutcome = { state, returnFocus: false, handled: true }

  switch (event.kind) {
    case 'open':
      return {
        state: { ...state, open: true, active: resolveActive(options, state.value) },
        returnFocus: false,
        handled: true,
      }

    case 'close':
      return {
        state: { ...state, open: false, typed: '', typedAt: 0 },
        returnFocus: false,
        handled: true,
      }

    case 'toggle':
      return state.open
        ? { state: { ...state, open: false, typed: '', typedAt: 0 }, returnFocus: false, handled: true }
        : {
            state: { ...state, open: true, active: resolveActive(options, state.value) },
            returnFocus: false,
            handled: true,
          }

    case 'point': {
      const option = options[event.index]
      if (!option || option.disabled === true) return unchanged
      return { state: { ...state, active: event.index }, returnFocus: false, handled: true }
    }

    case 'choose': {
      const option = options[event.index]
      // A click on a disabled row is swallowed rather than treated as a
      // dismissal: the list closing under a press that was refused reads
      // as the press having worked.
      if (!option || option.disabled === true) return unchanged
      return settle(state, options, event.index)
    }

    case 'sync':
      return {
        state: {
          ...state,
          value: event.value,
          active: state.open ? resolveActive(options, event.value) : state.active,
        },
        returnFocus: false,
        handled: true,
      }

    case 'key':
      return reduceKey(state, options, event.key, event.at)
  }
}

function reduceKey(
  state: SelectState,
  options: readonly UiOption[],
  key: string,
  at: number,
): SelectOutcome {
  const opened = (active: number): SelectOutcome => ({
    state: { ...state, open: true, active },
    returnFocus: false,
    handled: true,
  })

  if (!state.open) {
    switch (key) {
      case 'ArrowDown':
      case 'Enter':
      case ' ':
        return opened(resolveActive(options, state.value))
      case 'ArrowUp':
        // From nothing, the far end; from a value, that value. Both are
        // "start where a reader would expect to be".
        return opened(state.value === null ? lastEnabled(options) : resolveActive(options, state.value))
      case 'Home':
        return opened(firstEnabled(options))
      case 'End':
        return opened(lastEnabled(options))
      default:
        // A letter opens the list and jumps in one keystroke, the way a
        // native `<select>` does.
        if (PRINTABLE(key)) return search(state, options, key, at, resolveActive(options, state.value), true)
        // Escape, Tab, F5 and everything else this control has no opinion
        // about. Reported as unhandled so the browser still gets them.
        return { state, returnFocus: false, handled: false }
    }
  }

  switch (key) {
    case 'ArrowDown':
      return { state: { ...state, active: stepEnabled(options, state.active, 1) }, returnFocus: false, handled: true }
    case 'ArrowUp':
      return { state: { ...state, active: stepEnabled(options, state.active, -1) }, returnFocus: false, handled: true }
    case 'Home':
      return { state: { ...state, active: firstEnabled(options) }, returnFocus: false, handled: true }
    case 'End':
      return { state: { ...state, active: lastEnabled(options) }, returnFocus: false, handled: true }
    case 'Enter':
      return settle(state, options, state.active)
    case ' ':
      // Space commits, unless a search is under way — in which case it is
      // a character in the thing being searched for, and committing would
      // choose whatever "New" happened to be highlighted halfway through
      // "New members".
      if (state.typed === '') return settle(state, options, state.active)
      return search(state, options, key, at, state.active, false)
    case 'Escape':
      return {
        state: { ...state, open: false, typed: '', typedAt: 0 },
        returnFocus: true,
        handled: true,
      }
    case 'Tab':
      // Not handled: Tab means "leave", and the list should be shut by the
      // time the next control has focus. Pulling focus back to the trigger
      // would make the key move focus backwards.
      return {
        state: { ...state, open: false, typed: '', typedAt: 0 },
        returnFocus: false,
        handled: false,
      }
    default:
      if (PRINTABLE(key)) return search(state, options, key, at, state.active, false)
      return { state, returnFocus: false, handled: false }
  }
}

function search(
  state: SelectState,
  options: readonly UiOption[],
  key: string,
  at: number,
  from: number,
  opening: boolean,
): SelectOutcome {
  const typed = typeAheadBuffer(state.typed, key, at - state.typedAt)
  if (typed === state.typed && typed === '') {
    // A space that started nothing. Swallowed rather than left to the
    // browser, which would scroll the page under an open dropdown.
    return { state: { ...state, open: state.open || opening }, returnFocus: false, handled: true }
  }
  const found = matchTypeAhead(options, typed, from)
  return {
    state: {
      ...state,
      open: true,
      typed,
      typedAt: at,
      // A miss leaves the highlight alone. Sliding to row zero would make
      // a typo look like a jump that worked.
      active: found >= 0 ? found : from,
    },
    returnFocus: false,
    handled: true,
  }
}
