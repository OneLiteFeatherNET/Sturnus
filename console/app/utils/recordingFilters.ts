/**
 * Which of somebody's recordings the list is being asked to show.
 *
 * **The filter lives in the URL.** A filtered list is a place: it can be
 * bookmarked, opened in a second tab and reached with the back button,
 * and a colleague can be sent "the retro meetings from August" as a link
 * rather than as instructions. State held in a component has none of
 * that, and the back button out of a filtered list lands somewhere the
 * reader did not put it.
 *
 * So this module is two translations and nothing else: from a route's
 * query into the fields on screen, and from those fields into the query
 * string the API reads. Both are pure, both are tested without rendering
 * anything, and the round trip between them is what keeps the address bar
 * and the controls describing the same list.
 *
 * **Metadata only.** The search matches a channel name, the display names
 * of the people who were in a session, and the reader's own tags — never
 * a transcript. That is a decision about other people's speech, made in
 * `sturnus.console.filters`, whose docstring is where the reasoning
 * lives; what matters here is that the console must not offer a control
 * that promises more than the API will do.
 */

/** What the API calls things, so the two spellings live in one place. */
const TEXT = 'q'
const TAG = 'tag'
const FROM = 'from'
const TO = 'to'
const PROTOCOL = 'protocol'

/** Whether a recording produced a protocol. The empty string is "either",
 *  because that is what an unset `<select>` sends and inventing a third
 *  word for it would mean translating in both directions. */
export type ProtocolFilter = '' | 'with' | 'without'

export interface RecordingFilters {
  /** Free text. Matched against the channel, the participants and the
   *  reader's own tags — never a transcript. */
  q: string
  /** Tags the recording must carry, all of them. */
  tags: string[]
  /** Inclusive bounds as `YYYY-MM-DD`, or empty. */
  from: string
  to: string
  protocol: ProtocolFilter
}

/** A filter that narrows nothing, which is what a bare list means. */
export const NO_FILTERS: RecordingFilters = { q: '', tags: [], from: '', to: '', protocol: '' }

function firstOf(value: unknown): string {
  if (Array.isArray(value)) return String(value[0] ?? '')
  if (value === null || value === undefined) return ''
  return String(value)
}

function allOf(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((entry) => String(entry ?? '')).filter(Boolean)
  if (value === null || value === undefined || value === '') return []
  return [String(value)]
}

/**
 * The filter a route's query names.
 *
 * Anything unrecognised becomes "no filter" rather than an error: this is
 * a URL somebody may have edited or truncated, and a console that answers
 * a mangled link with an error page has turned a bad paste into a dead
 * end. The API still refuses a filter it cannot apply — this decides only
 * what the console asks for.
 */
export function filtersFromQuery(query: Record<string, unknown>): RecordingFilters {
  const protocol = firstOf(query[PROTOCOL])
  return {
    q: firstOf(query[TEXT]).trim(),
    tags: allOf(query[TAG]),
    from: firstOf(query[FROM]).trim(),
    to: firstOf(query[TO]).trim(),
    protocol: protocol === 'with' || protocol === 'without' ? protocol : '',
  }
}

/**
 * The same filter as a route query, with the empty fields left out.
 *
 * Left out rather than written as empty strings, so that the address of
 * an unfiltered list is `/recordings` and not `/recordings?q=&from=&to=`.
 * A URL that carries every control whether or not it was touched is one
 * nobody can read and nobody can tell apart from another.
 */
export function filtersToRouteQuery(filters: RecordingFilters): Record<string, string | string[]> {
  const query: Record<string, string | string[]> = {}
  if (filters.q) query[TEXT] = filters.q
  if (filters.tags.length > 0) query[TAG] = [...filters.tags]
  if (filters.from) query[FROM] = filters.from
  if (filters.to) query[TO] = filters.to
  if (filters.protocol) query[PROTOCOL] = filters.protocol
  return query
}

/**
 * The API path for one page of a filtered list.
 *
 * Built with `URLSearchParams` rather than by joining strings: a channel
 * name with an ampersand in it is a search that silently becomes two
 * parameters otherwise, and the failure looks like the search box being
 * broken for one person.
 */
export function filteredSessionsPath(
  filters: RecordingFilters,
  limit: number,
  offset: number,
): string {
  const params = new URLSearchParams()
  params.set('limit', String(limit))
  params.set('offset', String(offset))
  if (filters.q) params.set(TEXT, filters.q)
  // Repeated rather than joined: `?tag=a&tag=b` is what the API reads,
  // and a comma-joined value would be one tag containing a comma.
  for (const tag of filters.tags) params.append(TAG, tag)
  if (filters.from) params.set(FROM, filters.from)
  if (filters.to) params.set(TO, filters.to)
  if (filters.protocol) params.set(PROTOCOL, filters.protocol)
  return `/sessions?${params.toString()}`
}

/** Whether anything is being narrowed at all.
 *
 *  The page needs it to tell "you have no recordings" from "nothing
 *  matched what you asked for" — two sentences that mean very different
 *  things to somebody who was recorded yesterday. */
export function hasActiveFilters(filters: RecordingFilters): boolean {
  return Boolean(
    filters.q || filters.tags.length > 0 || filters.from || filters.to || filters.protocol,
  )
}

/**
 * The filter after a chip is pressed.
 *
 * Pressing a chip that is already on takes it off, because a chip that
 * can only be added is a filter somebody has to edit the URL to escape.
 */
export function toggledTag(filters: RecordingFilters, tag: string): RecordingFilters {
  const tags = filters.tags.includes(tag)
    ? filters.tags.filter((held) => held !== tag)
    : [...filters.tags, tag].sort()
  return { ...filters, tags }
}

/**
 * What is being narrowed, in words, one phrase per active control.
 *
 * Shown above the list so that a reader arriving on a link knows why they
 * are seeing eleven recordings out of forty-seven. A list that is
 * filtered without saying so is one people report as having lost their
 * meetings.
 */
export function activeFilterLabels(filters: RecordingFilters): string[] {
  const said: string[] = []
  if (filters.q) said.push(`matching “${filters.q}”`)
  for (const tag of filters.tags) said.push(`tagged ${tag}`)
  if (filters.from && filters.to) said.push(`between ${filters.from} and ${filters.to}`)
  else if (filters.from) said.push(`since ${filters.from}`)
  else if (filters.to) said.push(`up to ${filters.to}`)
  if (filters.protocol === 'with') said.push('with a protocol')
  if (filters.protocol === 'without') said.push('without a protocol')
  return said
}
