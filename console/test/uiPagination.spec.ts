/**
 * The pager beside the list, which is `paging.ts` wearing a different hat.
 *
 * Almost everything a pager decides — which numbers to show, where the
 * gaps fall, how many pages a total divides into — was decided once
 * already for the recordings list and is tested in `paging.spec.ts`. This
 * module deliberately adds none of it back. What it adds is the two things
 * the recordings pager does not need, because that one is built from links
 * and this one is a control:
 *
 * - a step that cannot walk off either end, since a button has no `href`
 *   to be disabled by not existing;
 * - a sentence saying where you are, because a control with no address
 *   bar has to say it somewhere.
 */
import { describe, expect, it } from 'vitest'

import { PAGE_SIZE } from '../app/utils/paging'
import { paginationView, stepPage } from '../app/utils/uiPagination'

describe('the view a pager renders from', () => {
  it('knows how many pages there are and which one this is', () => {
    const view = paginationView(2, 55, 20)
    expect(view.page).toBe(2)
    expect(view.count).toBe(3)
  })

  it('offers the ends and the neighbourhood, with a gap where numbers were left out', () => {
    // Not re-derived here: this is `pageNumbers` from `paging.ts`, and the
    // assertion is that it is being used rather than re-answered.
    expect(paginationView(10, 400, 20).numbers).toEqual([1, null, 9, 10, 11, null, 20])
  })

  it('knows there is nowhere to go back to from the first page', () => {
    const view = paginationView(1, 55, 20)
    expect(view.hasPrevious).toBe(false)
    expect(view.hasNext).toBe(true)
  })

  it('knows there is nowhere to go on to from the last', () => {
    const view = paginationView(3, 55, 20)
    expect(view.hasPrevious).toBe(true)
    expect(view.hasNext).toBe(false)
  })

  it('is page one of one for an empty list, and goes nowhere', () => {
    // Not one of zero. "1 of 0" is a sentence nobody can act on, and
    // hiding the pager entirely makes an empty list look like a broken
    // one — `paging.ts` settled this and this inherits it.
    const view = paginationView(1, 0, 20)
    expect(view.count).toBe(1)
    expect(view.hasPrevious).toBe(false)
    expect(view.hasNext).toBe(false)
  })

  it('pulls a page past the end back to the last real one', () => {
    // A bookmark to page nine of a list that has since shrunk to three.
    expect(paginationView(9, 55, 20).page).toBe(3)
  })

  it('pulls a page before the beginning back to the first', () => {
    expect(paginationView(0, 55, 20).page).toBe(1)
  })

  it('uses the list\'s own page size when it is not told one', () => {
    expect(paginationView(1, PAGE_SIZE * 2).count).toBe(2)
  })

  it('says where you are, with the numbers as numbers rather than as quantities', () => {
    // A page number is not a measurement: `1,024` is not a page. The
    // README says a number that is not a quantity goes through as a
    // string, and this is one of them.
    expect(paginationView(2, 55, 20).position).toEqual({
      key: 'ui.pagination.position',
      params: { page: '2', count: '3' },
    })
  })
})

describe('stepping', () => {
  it('moves one page at a time', () => {
    expect(stepPage(2, 5, 1)).toBe(3)
    expect(stepPage(2, 5, -1)).toBe(1)
  })

  it('stops at both ends rather than walking off them', () => {
    // A link can be absent; a button is always there and has to refuse.
    expect(stepPage(5, 5, 1)).toBe(5)
    expect(stepPage(1, 5, -1)).toBe(1)
  })

  it('lands somewhere real when it is given somewhere that is not', () => {
    expect(stepPage(99, 5, 1)).toBe(5)
    expect(stepPage(-3, 5, -1)).toBe(1)
  })
})
