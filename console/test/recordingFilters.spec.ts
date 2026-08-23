/**
 * Which recordings the list is being asked for, and how that is written down.
 *
 * The property that carries all the others is the round trip: what the
 * address bar says and what the controls show must be the same filter, in
 * both directions. A translation that loses a field one way produces a
 * link that opens a different list than the one it was copied from — and
 * nobody notices until somebody sends one to a colleague.
 */
import { describe, expect, it } from 'vitest'

import {
  NO_FILTERS,
  activeFilterLabels,
  chipsFromFilters,
  filteredSessionsPath,
  filtersFromChips,
  filtersFromQuery,
  filtersToRouteQuery,
  hasActiveFilters,
  type RecordingFilters,
} from '../app/utils/recordingFilters'

function filters(over: Partial<RecordingFilters> = {}): RecordingFilters {
  return { ...NO_FILTERS, tags: [], ...over }
}

describe('reading a filter out of a URL', () => {
  it('finds nothing to narrow in a bare list', () => {
    expect(filtersFromQuery({})).toEqual(filters())
  })

  it('takes the search somebody typed', () => {
    expect(filtersFromQuery({ q: 'weekly retro' }).q).toBe('weekly retro')
  })

  it('reads every tag, not only the first', () => {
    // `?tag=retro&tag=kunde` is how a set of chips is written; reading
    // one would describe a narrower list than the URL asked for.
    expect(filtersFromQuery({ tag: ['retro', 'kunde'] }).tags).toEqual(['retro', 'kunde'])
  })

  it('reads a single tag that is not in an array', () => {
    expect(filtersFromQuery({ tag: 'retro' }).tags).toEqual(['retro'])
  })

  it('ignores a protocol value it does not recognise', () => {
    // A hand-edited or truncated URL. Answering it with an error page
    // turns a bad paste into a dead end.
    expect(filtersFromQuery({ protocol: 'maybe' }).protocol).toBe('')
  })

  it('keeps both ends of a date range', () => {
    const read = filtersFromQuery({ from: '2026-08-01', to: '2026-08-21' })
    expect([read.from, read.to]).toEqual(['2026-08-01', '2026-08-21'])
  })
})

describe('writing a filter back into a URL', () => {
  it('leaves an unfiltered list without a query string at all', () => {
    // So the address of the list is `/recordings`, not
    // `/recordings?q=&from=&to=` — a URL nobody can read and nobody can
    // tell apart from another.
    expect(filtersToRouteQuery(filters())).toEqual({})
  })

  it('survives a round trip through the URL', () => {
    const asked = filters({
      q: 'retro',
      tags: ['kunde', 'retro'],
      from: '2026-08-01',
      to: '2026-08-21',
      protocol: 'without',
    })
    expect(filtersFromQuery(filtersToRouteQuery(asked))).toEqual(asked)
  })

  it('writes tags as a repeated parameter', () => {
    expect(filtersToRouteQuery(filters({ tags: ['retro', 'kunde'] })).tag).toEqual([
      'retro',
      'kunde',
    ])
  })
})

describe('asking the API for a page of a filtered list', () => {
  it('always names the window', () => {
    expect(filteredSessionsPath(filters(), 20, 40)).toBe('/sessions?limit=20&offset=40')
  })

  it('repeats the tag parameter rather than joining the tags', () => {
    // A comma-joined value would be one tag containing a comma.
    expect(filteredSessionsPath(filters({ tags: ['retro', 'kunde'] }), 20, 0)).toContain(
      'tag=retro&tag=kunde',
    )
  })

  it('escapes a search that would otherwise become two parameters', () => {
    // A channel name with an ampersand in it. The failure looks like the
    // search box being broken for exactly one person.
    expect(filteredSessionsPath(filters({ q: 'a&b=c' }), 20, 0)).toContain('q=a%26b%3Dc')
  })

  it('leaves out the controls nobody touched', () => {
    expect(filteredSessionsPath(filters(), 20, 0)).not.toContain('protocol')
  })
})

describe('knowing whether a list is filtered', () => {
  it('says no for a bare list', () => {
    // The page needs this to tell "you have no recordings" from "nothing
    // matched what you asked for".
    expect(hasActiveFilters(filters())).toBe(false)
  })

  it.each([
    ['a search', filters({ q: 'retro' })],
    ['a tag', filters({ tags: ['retro'] })],
    ['a start date', filters({ from: '2026-08-01' })],
    ['an end date', filters({ to: '2026-08-21' })],
    ['a protocol', filters({ protocol: 'with' })],
  ])('says yes for %s', (_name, asked) => {
    expect(hasActiveFilters(asked)).toBe(true)
  })
})

describe('the one field holding tags and words', () => {
  it('shows the required tags as chips and the search as text', () => {
    expect(chipsFromFilters(filters({ tags: ['retro', 'kunde'], q: 'august' }))).toEqual({
      chips: ['retro', 'kunde'],
      text: 'august',
    })
  })

  it('hands the field a copy, so editing chips does not edit the URL', () => {
    // The draft is a working copy; a shared array would let a keystroke
    // change the filter the page is currently describing.
    const asked = filters({ tags: ['retro'] })
    chipsFromFilters(asked).chips.push('kunde')
    expect(asked.tags).toEqual(['retro'])
  })

  it('takes the chips as tags and the words as the search', () => {
    const next = filtersFromChips(filters(), { chips: ['retro'], text: 'the database' })
    expect([next.tags, next.q]).toEqual([['retro'], 'the database'])
  })

  it('keeps text that was typed and never committed', () => {
    // Discarding it loses a search somebody wrote; promoting it to a chip
    // invents a tag they did not ask for. The API matches free text
    // against their own tags anyway, so an uncommitted word still finds
    // what they meant.
    expect(filtersFromChips(filters(), { chips: [], text: 'retro' })).toEqual(
      filters({ q: 'retro' }),
    )
  })

  it('does not send the space somebody left after a word', () => {
    expect(filtersFromChips(filters(), { chips: [], text: '  august ' }).q).toBe('august')
  })

  it('leaves the dates and the protocol alone', () => {
    const held = filters({ from: '2026-08-01', protocol: 'without' })
    const next = filtersFromChips(held, { chips: ['retro'], text: '' })
    expect([next.from, next.protocol]).toEqual(['2026-08-01', 'without'])
  })

  it('survives the round trip, which is what keeps the field and the URL agreeing', () => {
    const held = filters({ tags: ['retro', 'kunde'], q: 'august' })
    expect(filtersFromChips(held, chipsFromFilters(held))).toEqual(held)
  })
})

describe('saying why the list is short', () => {
  it('says nothing about a list that is not filtered', () => {
    expect(activeFilterLabels(filters())).toEqual([])
  })

  it('names each control that is doing something', () => {
    // The order is part of the answer: the phrases are read as one
    // sentence, so the search comes before the tags and the dates before
    // the protocol, whichever language writes them out.
    expect(activeFilterLabels(filters({ q: 'retro', protocol: 'without' }))).toEqual([
      { key: 'recordings.filterMatching', params: { text: 'retro' } },
      { key: 'recordings.filterWithoutProtocol' },
    ])
  })

  it('names every control at once, in the order they are read in', () => {
    expect(
      activeFilterLabels(
        filters({
          q: 'retro',
          tags: ['kunde', 'planung'],
          from: '2026-08-01',
          to: '2026-08-21',
          protocol: 'with',
        }),
      ),
    ).toEqual([
      { key: 'recordings.filterMatching', params: { text: 'retro' } },
      { key: 'recordings.filterTagged', params: { tag: 'kunde' } },
      { key: 'recordings.filterTagged', params: { tag: 'planung' } },
      { key: 'recordings.filterBetween', params: { from: '2026-08-01', to: '2026-08-21' } },
      { key: 'recordings.filterWithProtocol' },
    ])
  })

  it('reads a half-open range as one', () => {
    expect(activeFilterLabels(filters({ from: '2026-08-01' }))).toEqual([
      { key: 'recordings.filterSince', params: { from: '2026-08-01' } },
    ])
    expect(activeFilterLabels(filters({ to: '2026-08-21' }))).toEqual([
      { key: 'recordings.filterUntil', params: { to: '2026-08-21' } },
    ])
  })

  it('reads a closed range as a range', () => {
    expect(activeFilterLabels(filters({ from: '2026-08-01', to: '2026-08-21' }))).toEqual([
      { key: 'recordings.filterBetween', params: { from: '2026-08-01', to: '2026-08-21' } },
    ])
  })
})
