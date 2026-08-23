/**
 * A list of things somebody can pick one of, and the arithmetic of moving
 * around it.
 *
 * Six pages of this console have hand-rolled a `<select>`, and the two
 * that needed something better than a `<select>` — a guild switcher, a
 * channel picker over two hundred rows — each grew their own idea of what
 * ArrowDown does. This module is the one idea. `uiSelect` and `uiCombobox`
 * are both written against it, which is what stops the seventh dropdown
 * from being a seventh answer.
 *
 * Everything here is a function of a list and a number. That is on
 * purpose: which option is active is the part of a dropdown that is
 * genuinely hard, and it is also the part that is invisible in a
 * screenshot — an active index sitting on a disabled row renders exactly
 * like one sitting on a choosable row, and the difference only shows when
 * somebody presses Enter. A decision that cannot be seen has to be
 * checkable without looking, so none of it is allowed to touch a document.
 */

/**
 * One row of a dropdown.
 *
 * `label` and `detail` are text rather than translation keys. The rows of
 * these controls are data the page already holds — a guild's name, a
 * channel's name, a person's display name — and the one thing they are
 * never is a sentence this console wrote. Where a caller does want a
 * translated label it translates it before handing the list over, which
 * keeps `$t` in the template it belongs to.
 *
 * `detail` is the secondary line: the Discord id under a name, the
 * category above a channel. It exists because that is how every one of
 * these pickers has had to render a snowflake, and because a snowflake
 * squeezed into the label with a dash in front of it cannot be styled as
 * the subordinate thing it is.
 */
export interface UiOption {
  value: string
  label: string
  detail?: string
  disabled?: boolean
}

const choosable = (option: UiOption | undefined): boolean =>
  option !== undefined && option.disabled !== true

/** Where a value sits in the list, or -1 when the list no longer has it. */
export function indexOfValue(options: readonly UiOption[], value: string | null): number {
  if (value === null) return -1
  return options.findIndex((option) => option.value === value)
}

/** How many rows there are to walk. Zero is a real answer and a common one:
 *  a guild with no text channels, a filter that matched nothing. */
export function enabledCount(options: readonly UiOption[]): number {
  return options.reduce((total, option) => (choosable(option) ? total + 1 : total), 0)
}

/** The first row somebody may choose, or -1 when there is none. */
export function firstEnabled(options: readonly UiOption[]): number {
  return options.findIndex((option) => choosable(option))
}

/** The last row somebody may choose, or -1 when there is none. */
export function lastEnabled(options: readonly UiOption[]): number {
  for (let at = options.length - 1; at >= 0; at -= 1) {
    if (choosable(options[at])) return at
  }
  return -1
}

/**
 * One arrow key's worth of movement.
 *
 * Two properties, and both of them are the kind that gets lost when this
 * lives in a template:
 *
 * - **Disabled rows are stepped over, not landed on.** An active index on
 *   a row nobody may choose is invisible until Enter does nothing.
 * - **It does not wrap.** Holding ArrowDown at the bottom of a two hundred
 *   row channel list would otherwise arrive silently back at the top, and
 *   the rows going past look identical either way. Home and End are how
 *   the ends get reached deliberately.
 *
 * `from` of -1 means nothing is active yet, which is what a closed
 * dropdown looks like: ArrowDown lands on the first row and ArrowUp on the
 * last. An index off the end of the list — the state after the list
 * shrank underneath an open dropdown — is treated the same way rather than
 * read out of bounds.
 */
export function stepEnabled(options: readonly UiOption[], from: number, delta: number): number {
  const step = delta < 0 ? -1 : 1
  const anchored = from >= 0 && from < options.length
  if (!anchored) return step > 0 ? firstEnabled(options) : lastEnabled(options)
  for (let at = from + step; at >= 0 && at < options.length; at += step) {
    if (choosable(options[at])) return at
  }
  // Nothing further in that direction. Staying put is the honest answer,
  // unless where we are standing is not somewhere to stand.
  return choosable(options[from]) ? from : -1
}

/* -------------------------------------------------------------------- */
/* Type-ahead                                                            */
/* -------------------------------------------------------------------- */

/**
 * How long a typed prefix survives without another keystroke.
 *
 * The figure every desktop list box uses. Long enough that `gam` typed at
 * a normal speed is one search; short enough that coming back to the
 * control a moment later starts a new one rather than searching for a word
 * with the last one glued to its front.
 */
export const TYPE_AHEAD_GAP_MS = 800

/**
 * The prefix being searched for, after one more key.
 *
 * A pure function of the previous buffer, the key, and how long ago the
 * previous key was — so the clock lives in the component and the rule
 * lives here, and a test can move time without a timer.
 *
 * Space is deliberately not allowed to *start* a buffer: on a closed
 * dropdown Space means "open", and a search term beginning with a space
 * matches nothing anybody was looking for.
 */
export function typeAheadBuffer(previous: string, key: string, sinceMs: number): string {
  if (key.length !== 1) return previous
  if (key === ' ' && previous === '') return previous
  return sinceMs > TYPE_AHEAD_GAP_MS ? key : previous + key
}

/**
 * The row somebody typing that prefix meant, or -1 for none.
 *
 * Two behaviours, both of them what a desktop list box does and neither of
 * them obvious:
 *
 * - **A repeated single character cycles.** `a`, `a`, `a` walks every row
 *   beginning with A instead of sitting on the first one, because that is
 *   what somebody pressing the same key three times is asking for.
 * - **Anything else searches from where the cursor already is.** Typing
 *   `g` and then `a` must keep the row `g` found; restarting the search
 *   from the top would let a second keystroke move the highlight
 *   backwards.
 *
 * The search wraps, unlike the arrow keys, because a search is a jump and
 * not a walk: there is no held key to overshoot with.
 *
 * The label is matched and the subtext is not. The subtext is usually a
 * Discord snowflake, and letting it match would make every row in a guild
 * list answer to every digit.
 */
export function matchTypeAhead(
  options: readonly UiOption[],
  buffer: string,
  from: number,
): number {
  if (buffer === '' || options.length === 0) return -1
  const needle = buffer.toLowerCase()
  const repeated = needle.length > 1 && [...needle].every((character) => character === needle[0])
  const prefix = repeated ? (needle[0] ?? '') : needle
  // A repeated character starts one past the current row so it advances;
  // a real prefix starts on it so more letters refine rather than move.
  const start = repeated ? from + 1 : Math.max(from, 0)
  for (let hop = 0; hop < options.length; hop += 1) {
    const at = (((start + hop) % options.length) + options.length) % options.length
    const option = options[at]
    if (!choosable(option)) continue
    if (option!.label.toLowerCase().startsWith(prefix)) return at
  }
  return -1
}

/* -------------------------------------------------------------------- */
/* Addressing a row                                                      */
/* -------------------------------------------------------------------- */

/**
 * The DOM id of one row, for `aria-activedescendant` to point at.
 *
 * Built from the position rather than from the value, and that is not a
 * detail. A value here is a Discord snowflake, a channel name, a locale
 * code — none of which is a valid id, and sanitising two different values
 * can produce one string, which would point the attribute at the wrong
 * row without ever failing.
 *
 * `undefined` when nothing is active, because the attribute has to be
 * absent rather than empty: an empty `aria-activedescendant` is a pointer
 * to nowhere, and a screen reader reports it as a broken control rather
 * than as an unpositioned one.
 */
export function optionDomId(base: string, index: number): string | undefined {
  return index < 0 ? undefined : `${base}-option-${index}`
}
