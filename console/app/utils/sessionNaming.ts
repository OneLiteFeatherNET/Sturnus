/**
 * Naming a meeting: what a title is, and why the form always sends both
 * halves of one.
 *
 * A title and a description look like two fields and are one fact. The API
 * serves them from one endpoint and **`PUT` is a replace**: a body without
 * a `description` member stores `null`, because absent and null are the
 * same fact told twice and `sturnus.console.naming` chose one spelling for
 * it. That is a correct decision at the endpoint and a trap at the form.
 *
 * So the trap is closed here rather than remembered at the call site.
 * {@link nameBodyFrom} takes the whole draft — both fields, always — and
 * returns both members, always. There is no function in this module that
 * can build a body carrying only a title, which is what it would take to
 * ship an interface that saves a name and silently deletes the paragraph
 * underneath it.
 *
 * **A title is shared and a tag is not**, and that difference is the whole
 * reason this is not `~/utils/tagging` with longer strings. Everybody who
 * was in the meeting sees the same title and any of them may correct it;
 * a tag is one person's private remark about a conversation other people
 * were also in. `sturnus.console.naming` makes the argument; what it means
 * here is that the form has to say so out loud, because somebody about to
 * clear a field is about to clear it for four colleagues.
 *
 * **The server is the authority on what the text is.** The normalisation
 * below mirrors `sturnus.console.naming` so that a length can be checked
 * before a round trip is spent on it and so that the Save button can tell
 * "changed" from "the same text with a stray space" — but every write
 * answers with the pair it actually stored, and that is what gets
 * rendered. The same arrangement `~/utils/tagging` documents.
 *
 * Every export starts with `name`, `session` or `NAME`. Nuxt auto-imports
 * every export under `app/utils` into one namespace, and this project has
 * already had two collisions resolved silently by file order.
 */
import type { Message } from './message'

/** The longest a title may be. The number `sturnus.console.naming`
 *  enforces, restated so an input can carry a `maxlength` rather than let
 *  somebody type a sentence and then be refused. */
export const NAME_MAX_TITLE_CHARS = 200

/** The longest a description may be. Same source, same reason. */
export const NAME_MAX_DESCRIPTION_CHARS = 4000

/** What `GET`/`PUT /api/sessions/{id}/name` answers, and what a `PUT`
 *  body looks like. One type for all three, because they are the same
 *  pair and a second shape would be a second place to forget a field. */
export interface SessionName {
  title: string | null
  description: string | null
}

/** The same pair while somebody is typing it. Two strings rather than two
 *  nullable ones: an `<input>` holds `''` and never `null`, and a draft
 *  that admitted both would have two spellings of an empty field — which
 *  is the distinction the API went to the trouble of removing. */
export interface NameDraft {
  title: string
  description: string
}

/**
 * Where a meeting's name is read and written.
 *
 * The id is escaped: it is a string from an API, and a string allowed to
 * contain a slash is a string allowed to address a different endpoint.
 */
export function sessionNamePath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/name`
}

/** The stored pair as a form holds it. Absent is an empty field. */
export function nameDraftFrom(name: SessionName | null | undefined): NameDraft {
  return { title: name?.title ?? '', description: name?.description ?? '' }
}

/**
 * One line naming a meeting, or `null` for a meeting nobody has named.
 *
 * NFC, whitespace collapsed to single spaces, trimmed — the mirror of
 * `normalise_title`. A title is rendered in a heading, a list row and a
 * browser tab, none of which has a second line, so a newline in one is a
 * paste accident rather than a decision.
 */
function collapsed(raw: string): string | null {
  const text = raw.normalize('NFC').replace(/\s+/gu, ' ').trim()
  return text === '' ? null : text
}

/**
 * What somebody wrote, or `null` for nothing written.
 *
 * Trimmed at the ends and otherwise left alone: the line breaks between
 * paragraphs are the shape of the text, and collapsing them would turn an
 * agenda into a run-on sentence. Carriage returns go, because a
 * `<textarea>` submits `\r\n` on every platform and the same paragraph
 * typed on two machines must not be two different strings.
 */
function paragraphs(raw: string): string | null {
  const text = raw.normalize('NFC').replaceAll('\r\n', '\n').replaceAll('\r', '\n').trim()
  return text === '' ? null : text
}

/**
 * The body a save sends: **both members, always.**
 *
 * This is the whole point of the module. The endpoint replaces rather than
 * patches, so a body is not "the fields I changed" — it is the name the
 * meeting will have afterwards, and every member left out of it is a
 * member cleared. A caller cannot get that wrong here, because there is
 * nothing to pass but the whole draft and nothing returned but the whole
 * pair.
 */
export function nameBodyFrom(draft: NameDraft): SessionName {
  return { title: collapsed(draft.title), description: paragraphs(draft.description) }
}

/**
 * Why a save was refused, in words somebody can act on, or `null`.
 *
 * A *prediction* of what the server would say, measured against the text
 * that would actually be sent rather than against what is in the box — a
 * title padded to 205 characters with spaces is 200 by the time it
 * arrives, and refusing it would be this console disagreeing with the API
 * about a rule it copied from it.
 */
export function nameRefusal(draft: NameDraft): Message | null {
  const body = nameBodyFrom(draft)
  if ((body.title?.length ?? 0) > NAME_MAX_TITLE_CHARS) {
    return { key: 'recordings.nameTitleTooLong', params: { count: NAME_MAX_TITLE_CHARS } }
  }
  if ((body.description?.length ?? 0) > NAME_MAX_DESCRIPTION_CHARS) {
    return {
      key: 'recordings.nameDescriptionTooLong',
      params: { count: NAME_MAX_DESCRIPTION_CHARS },
    }
  }
  return null
}

/**
 * Whether saving would change anything.
 *
 * Compared after normalisation, so that a trailing space somebody's
 * keyboard added is not a change to write and a Save button does not
 * light up for text that is already stored. It is the difference between
 * a control that means something and one that is always available.
 */
export function nameIsDirty(stored: SessionName, draft: NameDraft): boolean {
  const wanted = nameBodyFrom(draft)
  return wanted.title !== stored.title || wanted.description !== stored.description
}

/**
 * What to tell somebody whose name would not save.
 *
 * The API's own reason is deliberately unreachable from here: `ApiError`
 * carries a status and nothing else, because `ofetch` puts the internal
 * cluster hostname into everything else it throws. A 400 is worth its own
 * sentence, unlike a tag's — the endpoint refuses a control character in
 * text that otherwise looks perfectly ordinary, and "it could not be
 * saved" would leave somebody re-typing a title that was never the
 * problem.
 */
export function nameWriteFailed(status: number): Message {
  if (status === 0) return { key: 'recordings.nameSaveUnreachable' }
  if (status === 404) return { key: 'recordings.nameSaveGone' }
  if (status === 400) return { key: 'recordings.nameSaveRefused' }
  return { key: 'recordings.nameSaveFailed' }
}
