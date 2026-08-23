/**
 * A list whose rows open, and the checkboxes that make a bulk action
 * possible without making it dangerous.
 *
 * Every page of this console that lists things has re-invented the first
 * half: a row, a chevron, a boolean somewhere. The second half is the part
 * that has never been built, and it is the part with teeth. A bulk action
 * is the only control in this console that does something to rows the
 * person pressing it may not be able to see — and the two ways it goes
 * wrong are both arithmetic:
 *
 * - **The header checkbox lies.** "Select all" on a paged list means
 *   either "these twenty" or "all four hundred", and a control that does
 *   not say which is a control that will one day do the other one.
 * - **The selection outlives the page and nobody says so.** Ticking three
 *   rows, paging on, and pressing Erase has to be a statement about those
 *   three rows — and the reader has to be told they are not the rows in
 *   front of them.
 *
 * So the count, the indeterminate state and the sentence about scope are
 * all decided here, where they can be checked without a checkbox.
 */
import { describe, expect, it } from 'vitest'

import {
  type UiRow,
  bulkStatement,
  headerState,
  isExpanded,
  selectableIds,
  selectionSummary,
  toggleAllOnPage,
  toggleExpanded,
  toggleSelected,
} from '../app/utils/uiDisclosureList'

const PAGE_ONE = ['a', 'b', 'c']
const PAGE_TWO = ['d', 'e', 'f']

describe('opening a row', () => {
  it('opens it, and closes it again', () => {
    const open = toggleExpanded([], 'a')
    expect(isExpanded(open, 'a')).toBe(true)
    expect(isExpanded(toggleExpanded(open, 'a'), 'a')).toBe(false)
  })

  it('leaves the others as they were', () => {
    // More than one row open at a time, on purpose: these rows reveal
    // actions, and comparing what two rows offer is a normal thing to
    // want. An accordion that shuts the row you were reading is a
    // decision, and not the right one here.
    const open = toggleExpanded(toggleExpanded([], 'a'), 'b')
    expect([...open].sort()).toEqual(['a', 'b'])
  })
})

describe('which rows may be ticked at all', () => {
  it('is every row unless a row says otherwise', () => {
    const rows: UiRow[] = [{ id: 'a' }, { id: 'b' }, { id: 'c' }]
    expect(selectableIds(rows)).toEqual(['a', 'b', 'c'])
  })

  it('leaves out the ones that say otherwise', () => {
    // A recording somebody has already erased, a job already running.
    // Including it in "select all" would make the header checkbox promise
    // an action that is refused row by row afterwards.
    const rows: UiRow[] = [{ id: 'a' }, { id: 'b', selectable: false }, { id: 'c' }]
    expect(selectableIds(rows)).toEqual(['a', 'c'])
  })
})

describe('ticking one row', () => {
  it('adds it and takes it away again', () => {
    const one = toggleSelected([], 'a')
    expect(one).toEqual(['a'])
    expect(toggleSelected(one, 'a')).toEqual([])
  })

  it('keeps the order rows were ticked in', () => {
    expect(toggleSelected(toggleSelected([], 'c'), 'a')).toEqual(['c', 'a'])
  })
})

describe('the header checkbox', () => {
  it('is empty when nothing on this page is ticked', () => {
    expect(headerState([], PAGE_ONE)).toBe('none')
  })

  it('is half-ticked when some of this page is', () => {
    // The indeterminate state is not decoration. Without it the box shows
    // either empty or full, and a page with two of twenty rows ticked
    // shows empty — so the next click ticks the other eighteen when the
    // reader meant to clear the two.
    expect(headerState(['a'], PAGE_ONE)).toBe('some')
  })

  it('is ticked when all of this page is', () => {
    expect(headerState(PAGE_ONE, PAGE_ONE)).toBe('all')
  })

  it('is empty on a page where none of the ticked rows live', () => {
    // Three rows ticked on page one, and the reader is now looking at page
    // two. The box describes *this page*, and saying "all" here would be a
    // lie about six rows.
    expect(headerState(PAGE_ONE, PAGE_TWO)).toBe('none')
  })

  it('is empty on a page with nothing tickable on it', () => {
    expect(headerState(PAGE_ONE, [])).toBe('none')
  })
})

describe('ticking the whole page', () => {
  it('adds every row on it', () => {
    expect([...toggleAllOnPage([], PAGE_ONE)].sort()).toEqual(['a', 'b', 'c'])
  })

  it('clears them again when they are all already ticked', () => {
    expect(toggleAllOnPage(PAGE_ONE, PAGE_ONE)).toEqual([])
  })

  it('completes a partial page rather than clearing it', () => {
    // What the half-ticked box means when it is clicked: the reader is
    // reaching for "all of them", not for "undo the two I did".
    expect([...toggleAllOnPage(['b'], PAGE_ONE)].sort()).toEqual(['a', 'b', 'c'])
  })

  it('never touches a row that is not on this page', () => {
    // The whole reason this takes the page's ids rather than clearing
    // everything: a reader who ticked three rows on page one and clears
    // page two has not changed their mind about page one.
    expect(toggleAllOnPage(['x', 'a', 'b', 'c'], PAGE_ONE)).toEqual(['x'])
    expect([...toggleAllOnPage(['x'], PAGE_ONE)].sort()).toEqual(['a', 'b', 'c', 'x'])
  })
})

describe('what the selection says about itself', () => {
  it('says nothing when nothing is ticked', () => {
    // `null`, so the bar can be absent rather than reading "0 selected",
    // which is a sentence nobody needs and a control nobody can use.
    expect(selectionSummary([], PAGE_ONE)).toBeNull()
  })

  it('counts them when they are all in front of the reader', () => {
    expect(selectionSummary(['a', 'b'], PAGE_ONE)).toEqual({
      key: 'ui.list.selected',
      params: { count: 2 },
    })
  })

  it('says how many are somewhere else when some are', () => {
    // The sentence this whole module exists for. A selection that survived
    // a page change and does not say so is a bulk action about rows the
    // reader cannot see.
    expect(selectionSummary(['a', 'b', 'd'], PAGE_ONE)).toEqual({
      key: 'ui.list.selectedWithOffPage',
      params: { count: 3, away: 1 },
    })
  })

  it('survives a page change intact', () => {
    const selected = ['a', 'b']
    expect(headerState(selected, PAGE_TWO)).toBe('none')
    expect(selectionSummary(selected, PAGE_TWO)).toEqual({
      key: 'ui.list.selectedWithOffPage',
      params: { count: 2, away: 2 },
    })
  })
})

describe('what a bulk action would apply to', () => {
  it('names the action and counts the rows', () => {
    // Named, not implied. "Apply to selected" beside a button reading
    // "Erase" is two sentences that agree today and drift apart the moment
    // a second action appears beside the first.
    expect(bulkStatement('Erase', ['a', 'b'])).toEqual({
      key: 'ui.list.bulkScope',
      params: { action: 'Erase', count: 2 },
    })
  })

  it('says nothing when there is nothing to apply it to', () => {
    expect(bulkStatement('Erase', [])).toBeNull()
  })
})
