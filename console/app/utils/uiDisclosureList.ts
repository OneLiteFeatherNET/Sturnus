/**
 * A list whose rows open, and the checkboxes that make a bulk action
 * possible without making it dangerous.
 *
 * Every page in this console that lists things has re-invented the first
 * half — a row, a chevron, a boolean somewhere. The second half has never
 * been built here, and it is the half with teeth: a bulk action is the
 * only kind of control in this console that acts on rows the person
 * pressing it may not be able to see.
 *
 * Two arithmetical failures, and both of them are invisible in a
 * screenshot:
 *
 * - **The header checkbox lies.** "Select all" over a paged list means
 *   either "these twenty" or "all four hundred". A control that does not
 *   settle which will eventually do the other one, and a box that renders
 *   empty when two of twenty rows are ticked turns "clear these two" into
 *   "tick the other eighteen".
 * - **The selection outlives the page and nobody says so.** Ticking three
 *   rows, paging on, and pressing a bulk action is a statement about those
 *   three rows — which is right, and has to be said out loud, because the
 *   reader is looking at six different ones.
 *
 * Selection is a list of ids rather than a flag on a row, precisely so it
 * *can* outlive the page: rows are re-fetched on every page change and any
 * state kept on them goes with them.
 */
import type { Message } from './message'

/** A row of the list, as far as this module is concerned. Whatever else it
 *  carries is the caller's business and travels in its own type. */
export interface UiRow {
  id: string
  /** False for a row that may not take part in a bulk action — a
   *  recording already erased, a job already running. Leaving it out of
   *  "select all" is what stops the header checkbox promising an action
   *  that is then refused row by row. */
  selectable?: boolean
}

export function selectableIds(rows: readonly UiRow[]): string[] {
  return rows.filter((row) => row.selectable !== false).map((row) => row.id)
}

/* -------------------------------------------------------------------- */
/* Opening a row                                                         */
/* -------------------------------------------------------------------- */

/**
 * A row opened, or closed.
 *
 * More than one row may be open at once, deliberately. These rows reveal
 * actions, and comparing what two of them offer is an ordinary thing to
 * want; an accordion that shuts the row somebody was reading in order to
 * open the next one is a decision, and not the right one for a list of
 * this shape.
 */
export function toggleExpanded(open: readonly string[], id: string): readonly string[] {
  return open.includes(id) ? open.filter((held) => held !== id) : [...open, id]
}

export function isExpanded(open: readonly string[], id: string): boolean {
  return open.includes(id)
}

/* -------------------------------------------------------------------- */
/* Ticking rows                                                          */
/* -------------------------------------------------------------------- */

export function toggleSelected(selected: readonly string[], id: string): readonly string[] {
  return selected.includes(id) ? selected.filter((held) => held !== id) : [...selected, id]
}

/** Empty, part, or whole — the three states a header checkbox has, and the
 *  middle one is `indeterminate`. It describes **this page** and nothing
 *  else, which is the only reading of it that can be kept honest. */
export type HeaderState = 'none' | 'some' | 'all'

export function headerState(
  selected: readonly string[],
  pageIds: readonly string[],
): HeaderState {
  if (pageIds.length === 0) return 'none'
  const here = pageIds.filter((id) => selected.includes(id)).length
  if (here === 0) return 'none'
  return here === pageIds.length ? 'all' : 'some'
}

/**
 * The whole page ticked, or the whole page cleared.
 *
 * A partly-ticked page completes rather than clears: somebody clicking a
 * half-ticked box is reaching for "all of them", not for "undo the two I
 * did by hand".
 *
 * Rows that are not on this page are never touched. That is the reason
 * this takes the page's ids at all rather than simply emptying the
 * selection — a reader who ticked three rows on page one and then clears
 * page two has not changed their mind about page one.
 */
export function toggleAllOnPage(
  selected: readonly string[],
  pageIds: readonly string[],
): readonly string[] {
  if (headerState(selected, pageIds) === 'all') {
    return selected.filter((id) => !pageIds.includes(id))
  }
  const added = pageIds.filter((id) => !selected.includes(id))
  return [...selected, ...added]
}

/* -------------------------------------------------------------------- */
/* Saying what is selected                                               */
/* -------------------------------------------------------------------- */

/**
 * How much is ticked, and how much of it the reader cannot see.
 *
 * `null` when nothing is, so the bar is absent rather than announcing "0
 * selected" — a sentence nobody needs beside controls nobody can use.
 *
 * The second form is the sentence this module exists for. A selection that
 * survived a page change and does not mention it is a bulk action about
 * rows that are not on screen.
 */
export function selectionSummary(
  selected: readonly string[],
  pageIds: readonly string[],
): Message | null {
  const count = selected.length
  if (count === 0) return null
  const away = selected.filter((id) => !pageIds.includes(id)).length
  return away === 0
    ? { key: 'ui.list.selected', params: { count } }
    : { key: 'ui.list.selectedWithOffPage', params: { count, away } }
}

/**
 * What pressing a bulk action would do, named.
 *
 * The action's own word goes into the sentence rather than being implied
 * by whichever button is nearest. "Applies to the selection" beside a
 * button reading "Erase" is two sentences that agree today and drift apart
 * the moment a second action appears next to the first.
 */
export function bulkStatement(action: string, selected: readonly string[]): Message | null {
  if (selected.length === 0) return null
  return { key: 'ui.list.bulkScope', params: { action, count: selected.length } }
}
