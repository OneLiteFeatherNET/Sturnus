/**
 * Labelling a recording: what a chip is, and when two of them are one chip.
 *
 * A tag is a label one person puts on one meeting they were in, and it is
 * theirs alone — nobody else in the meeting ever sees it. That is a
 * decision the API makes and this module only carries; what it means here
 * is that a tag editor never has to ask "may I show this", because the
 * only tags it is ever given are the reader's own.
 *
 * **The server is the authority on what a tag is.** `sturnus.console.tags`
 * decides when two labels are the same label, and every write answers with
 * the set it actually stored — which the caller renders, replacing
 * whatever it had. The normalisation below is a *mirror* of that rule and
 * exists for one purpose: to recognise a chip somebody already has before
 * spending a round trip on it. If the two ever disagree, the server's
 * answer wins on the very next write, because that answer is what gets
 * rendered.
 *
 * Every name here starts with `tag`. Nuxt auto-imports every export in
 * this directory into one namespace, and this project already has two
 * pairs of colliding names (`formatCount`, `channelLabel`) where the
 * winner is decided by file order and nobody is told.
 */

/** The longest a tag may be. The same number the API enforces; it is here
 *  so an input can carry a `maxlength` rather than let somebody type a
 *  sentence and then be refused. */
export const TAG_MAX_CHARS = 48

/** How many tags one recording may carry. Same source, same reason. */
export const TAG_MAX_PER_RECORDING = 20

/** One label the signed-in person uses, and how many of their recordings
 *  carry it. Ordered most-used-first by the API. */
export interface TagUse {
  tag: string
  sessions: number
}

/** What `GET /api/tags` answers. */
export interface TagsResponse {
  tags: TagUse[]
}

/** What `PUT /api/sessions/{id}/tags` answers: the set that was stored. */
export interface StoredTagsResponse {
  tags: string[]
}

/**
 * A label in the single spelling it is stored and compared under.
 *
 * NFC first, so a composed and a decomposed `ü` — identical in every font,
 * different to every index — become one string; whitespace collapsed and
 * trimmed, because a trailing space is invisible in an input field; then
 * lowercased.
 *
 * Deliberately *not* `toLocaleLowerCase()`: the result would depend on the
 * reader's browser locale, so the same word typed by a colleague in
 * Istanbul would become a different tag (Turkish `I` lowercases to a
 * dotless `ı`). The server lowercases without a locale, and this has to
 * agree with the server rather than with the reader.
 */
export function normaliseTag(raw: string): string {
  return raw.normalize('NFC').replace(/\s+/gu, ' ').trim().toLowerCase()
}

/**
 * What somebody typed, split into the labels they meant.
 *
 * A comma separates two tags because that is what people type when they
 * mean two, and because a tag containing a comma reads as two anyway.
 * Empty pieces vanish rather than becoming empty chips, so a trailing
 * comma is not an error to explain.
 */
export function splitTagInput(typed: string): string[] {
  return typed
    .split(',')
    .map(normaliseTag)
    .filter((tag) => tag.length > 0)
}

/**
 * The set after adding what somebody typed, or `null` if it adds nothing.
 *
 * `null` rather than the unchanged set, so a caller can tell "already
 * there" from "added" without comparing arrays — the difference decides
 * whether a write is worth making at all.
 *
 * The two ceilings are `tagRefusal`'s to enforce, and a caller asks it
 * first. This function does not re-check them, because a second copy of a
 * limit is a second copy that drifts; a caller that skipped the check
 * sends a set the API refuses with a 400, which is a refusal and not a
 * corruption.
 */
export function tagsWith(existing: readonly string[], typed: string): string[] | null {
  const wanted = splitTagInput(typed).filter((tag) => !existing.includes(tag))
  if (wanted.length === 0) return null
  // Alphabetical, which is the order the API stores and returns them in.
  // Appending instead would put a new chip at the end and then have it
  // jump on the next page load.
  return [...existing, ...wanted].sort()
}

/** The set after removing one label. */
export function tagsWithout(existing: readonly string[], tag: string): string[] {
  return existing.filter((held) => held !== tag)
}

/**
 * Why an addition was refused, in words somebody can act on.
 *
 * Derived from the same two constants the API enforces, so the sentence
 * and the rule cannot drift apart in this file. It is a *prediction* of
 * what the server would say; the server still decides, and its refusal is
 * handled separately (see `tagWriteFailed`).
 */
export function tagRefusal(existing: readonly string[], typed: string): string | null {
  const wanted = splitTagInput(typed)
  if (wanted.length === 0) return null
  if (wanted.some((tag) => tag.length > TAG_MAX_CHARS)) {
    return `A tag can be at most ${TAG_MAX_CHARS} characters.`
  }
  const added = wanted.filter((tag) => !existing.includes(tag))
  if (existing.length + added.length > TAG_MAX_PER_RECORDING) {
    return `A recording can carry at most ${TAG_MAX_PER_RECORDING} tags.`
  }
  if (added.length === 0) return 'That tag is already on this recording.'
  return null
}

/** Where the labels somebody uses are read from. */
export const TAGS_PATH = '/tags'

/**
 * Where one recording's labels are written.
 *
 * The id is escaped: it is a string from an API, and a string allowed to
 * contain a slash is a string allowed to address a different endpoint.
 */
export function sessionTagsPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/tags`
}

/**
 * What to tell somebody whose tag would not save.
 *
 * The API's own reason is deliberately unreachable from here — `ApiError`
 * carries a status and nothing else, because `ofetch` puts the internal
 * cluster hostname into everything else it throws. So the status is what
 * decides the wording, and there are only three answers worth
 * distinguishing.
 */
export function tagWriteFailed(status: number): string {
  if (status === 0) return 'Your tags could not be saved: the console could not reach the server.'
  if (status === 404) return 'This recording is no longer yours to label.'
  return 'Your tags could not be saved. Nothing was changed.'
}
