/**
 * Which slice of a list is on screen, and how it is said.
 *
 * Each of these pins a way a paged list can lie: a page number that does
 * not exist, a range reading `21–20`, a "next" link on the last page, an
 * empty list announcing itself as page one of zero. None of them is
 * visible until it is wrong, and none of them needs a browser to check.
 */
import { describe, expect, it } from 'vitest'

import {
  PAGE_SIZE,
  isPastTheEnd,
  offsetForPage,
  pageCount,
  pageFromQuery,
  pageNumbers,
  pageSummary,
} from '../app/utils/paging'

describe('reading a page number out of a URL', () => {
  it('takes the number somebody typed', () => {
    expect(pageFromQuery('3')).toBe(3)
  })

  it('starts at the first page when the URL says nothing', () => {
    expect(pageFromQuery(undefined)).toBe(1)
  })

  it('reads a repeated parameter as its first value', () => {
    // Vue Router hands `?page=2&page=9` over as an array, and the answer
    // has to be a number rather than `NaN` on the way to an offset.
    expect(pageFromQuery(['2', '9'])).toBe(2)
  })

  it.each(['0', '-4', 'banana', '', '1.9e3'])(
    'falls back to the first page for %o rather than erroring',
    (nonsense) => {
      // Somebody in the address bar. A console that answers a typo with
      // an error page has turned it into a dead end.
      expect(pageFromQuery(nonsense)).toBe(1)
    },
  )
})

describe('turning a page number into an offset', () => {
  it('starts the first page at the beginning', () => {
    expect(offsetForPage(1, 20)).toBe(0)
  })

  it('starts each page where the last one ended', () => {
    expect(offsetForPage(3, 20)).toBe(40)
  })

  it('never asks for a negative offset, whatever it is given', () => {
    expect(offsetForPage(-5, 20)).toBe(0)
  })
})

describe('counting the pages', () => {
  it('fits an exact multiple without an empty page at the end', () => {
    expect(pageCount(40, 20)).toBe(2)
  })

  it('gives the remainder a page of its own', () => {
    expect(pageCount(41, 20)).toBe(3)
  })

  it('calls an empty list one page rather than none', () => {
    // "Page 1 of 0" is a sentence no reader can act on.
    expect(pageCount(0, 20)).toBe(1)
  })

  it('uses the console page size by default', () => {
    expect(pageCount(PAGE_SIZE + 1)).toBe(2)
  })
})

describe('choosing which page numbers to offer', () => {
  it('offers every page while they still fit', () => {
    expect(pageNumbers(1, 3)).toEqual([1, 2, 3])
  })

  it('keeps both ends and the neighbourhood of where you are', () => {
    expect(pageNumbers(10, 20)).toEqual([1, null, 9, 10, 11, null, 20])
  })

  it('writes out a gap of a single page rather than eliding it', () => {
    // An ellipsis standing for one number is longer than the number.
    expect(pageNumbers(4, 6)).toEqual([1, 2, 3, 4, 5, 6])
  })

  it('does not offer a page beyond the last one', () => {
    expect(pageNumbers(20, 20)).toEqual([1, null, 19, 20])
  })

  it('survives a page number past the end without inventing pages', () => {
    expect(pageNumbers(99, 3)).toEqual([1, 2, 3])
  })
})

describe('saying what is on screen', () => {
  it('names the first and last row actually shown', () => {
    expect(pageSummary(47, 0, 20)).toBe('Recordings 1–20 of 47')
  })

  it('stops at the last row of a short final page', () => {
    // `offset + size` would announce "41–60 of 47", which is a list
    // nobody trusts about anything else either.
    expect(pageSummary(47, 40, 7)).toBe('Recordings 41–47 of 47')
  })

  it('says one recording in the singular', () => {
    expect(pageSummary(1, 0, 1)).toBe('Recording 1 of 1')
  })

  it('says nothing at all about an empty list', () => {
    // The empty state already says it in a full sentence; a second,
    // arithmetical way of saying so underneath it reads as a fault.
    expect(pageSummary(0, 0, 0)).toBeNull()
  })
})

describe('recognising a page past the end', () => {
  it('knows a stale bookmark from an empty history', () => {
    expect(isPastTheEnd(47, 0, 5)).toBe(true)
  })

  it('does not call an empty history a stale bookmark', () => {
    expect(isPastTheEnd(0, 0, 1)).toBe(false)
  })

  it('says nothing about a page that has rows on it', () => {
    expect(isPastTheEnd(47, 20, 2)).toBe(false)
  })
})
