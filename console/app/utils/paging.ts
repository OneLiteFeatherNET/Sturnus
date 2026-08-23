/**
 * Which slice of a list is on screen, and how to say so.
 *
 * All of it is arithmetic, and all of it lives here rather than in the
 * page for the reason this project keeps repeating: a decision embedded
 * in a template can only be checked by rendering one. The decisions here
 * are small and each of them is a way a paged list can lie — a page
 * number that does not exist, a range that reads `21–20`, a "next" link
 * on the last page.
 *
 * **The page number is in the URL, not in a component.** A list somebody
 * has paged into is a place, and a place needs an address: without one,
 * the back button leaves the list somewhere the reader did not put it and
 * no link can express "the third page of my recordings". The API speaks
 * `limit` and `offset`; a human speaks pages. `offsetForPage` is the only
 * place the two meet.
 */
import type { Message } from './message'

/** How many recordings one page holds. The API's own default, restated
 *  here so the console asks for a window it named rather than inheriting
 *  one silently — a client that does not say how much it wants cannot
 *  notice when the answer changes size. */
export const PAGE_SIZE = 20

/**
 * The page number a query string names, or the first page.
 *
 * Anything that is not a whole number of one or more is the first page
 * rather than an error. `?page=0`, `?page=banana` and a hand-edited URL
 * are all somebody in the address bar, and a console that answers them
 * with an error page has turned a typo into a dead end. The API still
 * refuses a window it cannot serve; this is about what the console asks
 * for in the first place.
 */
export function pageFromQuery(raw: unknown): number {
  const first = Array.isArray(raw) ? raw[0] : raw
  const parsed = Number.parseInt(String(first ?? ''), 10)
  if (!Number.isFinite(parsed) || parsed < 1) return 1
  return parsed
}

/** Where a page begins, in the offset the API speaks. */
export function offsetForPage(page: number, size: number = PAGE_SIZE): number {
  return Math.max(0, (Math.max(1, Math.floor(page)) - 1) * size)
}

/**
 * How many pages a total divides into. Never fewer than one.
 *
 * An empty list is page one of one, not page one of zero: "1 of 0" is a
 * sentence no reader can act on, and the alternative — hiding the pager
 * entirely — makes an empty list look like a broken one.
 */
export function pageCount(total: number, size: number = PAGE_SIZE): number {
  if (size <= 0) return 1
  return Math.max(1, Math.ceil(Math.max(0, total) / size))
}

/**
 * The page numbers to offer, with gaps where numbers were left out.
 *
 * Always the first and the last, always the current one and its
 * neighbours. A pager that lists every page is unusable at forty pages
 * and a pager that only steps one at a time cannot reach the far end of a
 * history at all — the ends and the neighbourhood are what somebody
 * actually navigates by.
 *
 * A gap is a `null`, so a template renders an ellipsis without having to
 * decide when one belongs.
 */
export function pageNumbers(current: number, count: number): (number | null)[] {
  const here = Math.min(Math.max(1, current), count)
  const wanted = new Set([1, count, here - 1, here, here + 1])
  const shown = [...wanted].filter((page) => page >= 1 && page <= count).sort((a, b) => a - b)
  const withGaps: (number | null)[] = []
  let previous = 0
  for (const page of shown) {
    // A gap of exactly one page is written out rather than elided: an
    // ellipsis standing for a single number is longer than the number.
    if (previous > 0 && page - previous === 2) withGaps.push(previous + 1)
    else if (previous > 0 && page - previous > 2) withGaps.push(null)
    withGaps.push(page)
    previous = page
  }
  return withGaps
}

/**
 * What the list is showing, in words, or `null` when it is showing nothing.
 *
 * `null` rather than "0 of 0", because an empty list already says it is
 * empty in a full sentence and a second, arithmetical way of saying so
 * underneath it reads as a fault.
 *
 * The upper bound is the last row actually on screen and not
 * `offset + size`: the final page is usually short, and a list of 47
 * announcing "41–60 of 47" is a list nobody trusts about anything else
 * either.
 */
export function pageSummary(total: number, offset: number, shown: number): Message | null {
  if (total <= 0 || shown <= 0) return null
  const first = offset + 1
  const last = offset + shown
  if (first === last) return { key: 'recordings.pageSummaryOne', params: { from: first, total } }
  return { key: 'recordings.pageSummary', params: { from: first, to: last, total } }
}

/**
 * Whether a page is past the end of a list that does have rows.
 *
 * The state a bookmark reaches after enough recordings have been erased.
 * It is not the same as "no recordings", and telling somebody they have
 * never been recorded because their bookmark went stale is the kind of
 * wrong answer that gets reported as data loss.
 */
export function isPastTheEnd(total: number, shown: number, page: number): boolean {
  return total > 0 && shown === 0 && page > 1
}
