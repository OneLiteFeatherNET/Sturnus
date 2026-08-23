/**
 * The pager that stands beside a list, as opposed to the one that *is* a
 * list of links.
 *
 * **Almost nothing new is decided here, and that is the point.** Which
 * page numbers to offer, where the ellipses fall, how many pages a total
 * divides into — all of it was decided once for the recordings list, is
 * argued for in `~/utils/paging`, and is tested in `paging.spec.ts`. A
 * second answer to any of those questions would be a second thing to keep
 * in step, and the two would disagree first at the far end of a long
 * history where nobody is looking.
 *
 * What this adds is the two things a *control* needs and a set of links
 * does not. `RecordingsPager` renders `NuxtLink`s, because every page of
 * that list is a place with an address; a generic pager cannot assume its
 * caller has one, so it emits a page number instead. That difference
 * produces exactly two new decisions: a step that refuses to walk off
 * either end (a link can simply not exist; a button is always there and
 * has to say no), and a sentence saying where you are (a control with no
 * address bar has to state its position somewhere).
 */
import type { Message } from './message'
import { PAGE_SIZE, pageCount, pageNumbers } from './paging'

export interface PaginationView {
  /** The page actually being shown — clamped, so a bookmark to page nine
   *  of a list that has shrunk to three shows three. */
  page: number
  count: number
  /** Page numbers, with `null` standing for a gap. */
  numbers: (number | null)[]
  hasPrevious: boolean
  hasNext: boolean
  /** Where the reader is, in words. */
  position: Message
}

export function paginationView(
  page: number,
  total: number,
  size: number = PAGE_SIZE,
): PaginationView {
  const count = pageCount(total, size)
  const here = Math.min(Math.max(1, Math.floor(page)), count)
  return {
    page: here,
    count,
    numbers: pageNumbers(here, count),
    hasPrevious: here > 1,
    hasNext: here < count,
    // Both numbers go through as strings. A page number is not a
    // quantity, and `1,024` is not a page — `i18n/README.md` draws that
    // line and this is on the far side of it.
    position: {
      key: 'ui.pagination.position',
      params: { page: String(here), count: String(count) },
    },
  }
}

/** One page forwards or backwards, refusing to leave the list. */
export function stepPage(page: number, count: number, delta: number): number {
  const pages = Math.max(1, Math.floor(count))
  return Math.min(Math.max(1, Math.floor(page) + delta), pages)
}
