/**
 * What one meeting was actually published as, and where.
 *
 * A session used to have *a* protocol: `session.document_url`, one Outline
 * document, one link on the recording page. Since #144 a guild may name
 * several destinations, and `sturnus.application.exporting.publish_session`
 * writes to each of them independently — **surviving each destination's own
 * failure and recording only the ones that succeeded**. So a meeting can
 * now be published to two places out of three, and the recording page has
 * to be able to say that rather than showing one link and implying it is
 * the whole story.
 *
 * **The partial publish is legible, and this module is where it is read.**
 * A participant cannot see how many destinations their guild configured —
 * that listing is administrator-only and answers 404 to them — so this
 * page can never say "one of three failed". What it *can* see is a
 * disagreement between two answers it is given, and one of them is exact:
 *
 * - `session.document_url` is stamped **from the primary destination
 *   alone** (`worker._publish_session_documents`), and the primary is the
 *   guild's oldest enabled destination. It is also the link the Discord
 *   announcement carries.
 * - `GET /api/sessions/{id}/documents` lists every destination that
 *   produced a document, the primary included.
 *
 * So a session with documents and **no** `document_url` is a session whose
 * announced destination did not produce one while others did. That is the
 * partial failure, it is a fact rather than an inference, and
 * {@link publishedProtocols} names it. The reverse — a `document_url` that
 * is not among the listed documents — is the legacy `document_target`
 * publish, which writes no `session_document` row at all, and is the state
 * of every meeting recorded before this table existed.
 *
 * **Nothing here diagnoses.** The states below say which of two answers
 * disagree and stop; they never say a destination is broken, because a
 * destination that has not been retried yet and one that will never work
 * look identical from here and the retry sweep is still running. That is
 * the same discipline `~/utils/queue` applies to its derived figures.
 *
 * Keys and {@link Message}s, never prose — see `i18n/README.md`.
 */
import { formatSpec } from './exportTargets'
import type { Instant, Message } from './message'

/* -------------------------------------------------------------------- */
/* What the API sends                                                    */
/* -------------------------------------------------------------------- */

/** One published protocol, as `routes_documents.document_json` sends it. */
export interface SessionDocument {
  /** `null` for a document whose destination has since been removed. The
   *  row survives that on purpose: the document still exists in the other
   *  system and this link is what somebody follows when they go looking
   *  for last quarter's minutes. */
  targetId: number | null
  /** The format name — `outline`, `markdown`, `html`. A plain string,
   *  because a deployment may publish a format this console has never
   *  heard of and a document that renders as nothing is worse than one
   *  that renders as its own raw name. */
  provider: string
  url: string
  createdAt: string | null
  /** Whether this deployment holds the bytes and can serve them back.
   *  False for an Outline document, whose `url` points at Outline. */
  readable: boolean
  mediaType: string | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asText(value: unknown): string | null {
  if (value === null || value === undefined) return null
  return typeof value === 'string' ? value : String(value)
}

/**
 * The documents in a listing, oldest first, dropping any that carries no
 * URL.
 *
 * A row with no URL is a row with nothing to click, and this list exists
 * to be clicked: the console cannot serve an Outline document itself and
 * cannot address an object-store one except through the URL the worker
 * recorded. Everything else is tolerated — a provider this console has no
 * word for renders as its own name, and an unreadable `created_at` renders
 * as no date rather than taking the row down with it.
 */
export function parseSessionDocuments(payload: unknown): SessionDocument[] {
  if (!isRecord(payload) || !Array.isArray(payload.documents)) return []
  const documents: SessionDocument[] = []
  for (const raw of payload.documents) {
    if (!isRecord(raw)) continue
    const url = asText(raw.url)
    if (!url || url.trim() === '') continue
    const targetId = Number(raw.target_id)
    documents.push({
      targetId: Number.isSafeInteger(targetId) && raw.target_id !== null ? targetId : null,
      provider: asText(raw.provider) ?? '',
      url,
      createdAt: asText(raw.created_at),
      readable: raw.readable === true,
      mediaType: asText(raw.media_type),
    })
  }
  return documents
}

/* -------------------------------------------------------------------- */
/* One row of the list                                                   */
/* -------------------------------------------------------------------- */

export interface ProtocolRow {
  /** Stable and unique within the list, for `v-for`. Built from the
   *  destination where there is one and from the URL where there is not:
   *  every document whose destination has been removed carries a `null`
   *  `target_id`, and a guild that removed two of them would otherwise
   *  have two rows keyed alike. */
  id: string
  /** What the document is, in words this console has — or its raw
   *  provider name, where it has none. */
  label: Message
  url: string
  /** When it was published, or `null` when the API sent a date this
   *  console could not read. */
  at: Instant | null
  /** True where this deployment serves the bytes itself, so the link stays
   *  inside the console and under the session's own authorisation rule.
   *  False where the link leaves for Outline. */
  internal: boolean
  /** The destination this was published to has been removed. The document
   *  is still there and the link still works; nothing new will be
   *  published here. */
  orphaned: boolean
}

/** One document, ready to render. */
export function protocolRow(document: SessionDocument): ProtocolRow {
  const spec = formatSpec(document.provider)
  const at = document.createdAt === null ? null : new Date(document.createdAt)
  return {
    id: document.targetId === null ? `url:${document.url}` : `target:${document.targetId}`,
    label: spec === null
      ? { key: 'recordings.protocolUnknownFormat', params: { format: document.provider } }
      : { key: 'recordings.protocolFormat', params: { format: { key: spec.labelKey } } },
    url: document.url,
    at: at !== null && !Number.isNaN(at.getTime()) ? { at, format: 'utcMoment' } : null,
    internal: document.readable,
    orphaned: document.targetId === null,
  }
}

/* -------------------------------------------------------------------- */
/* What the section as a whole says                                      */
/* -------------------------------------------------------------------- */

/**
 * How the two answers stand relative to each other.
 *
 * - `none` — nothing was published, and nothing claims to have been.
 * - `announcedOnly` — a protocol link exists and no destination recorded
 *   one. The legacy `document_target` publish, which writes no
 *   `session_document` row: every meeting from before destinations
 *   existed is this.
 * - `complete` — the announced link is among the destinations listed.
 * - `announcedElsewhere` — destinations produced documents and the
 *   announced link is not one of them.
 * - `announcedMissing` — destinations produced documents and **nothing was
 *   announced**. The partial publish: the destination whose link Discord
 *   carries did not produce one, and these did.
 */
export type PublishState =
  | 'none'
  | 'announcedOnly'
  | 'complete'
  | 'announcedElsewhere'
  | 'announcedMissing'

export interface ProtocolsView {
  state: PublishState
  rows: ProtocolRow[]
  /** `session.document_url`, trimmed, or `null`. Rendered on its own only
   *  in `announcedOnly`, where there is no row carrying it. */
  announced: string | null
  /** The heading's sentence: how many destinations hold this meeting. */
  summary: Message
  /** What is worth saying beyond the count, or `null` when the two answers
   *  agree and there is nothing to explain. */
  note: Message | null
}

/**
 * What this session's protocols are, as a section of the recording page.
 *
 * Takes the announced URL rather than the whole session, so that this can
 * be asked without a session at all — the property being decided is a
 * relation between two strings and a list, and nothing about it needs a
 * ninety-minute meeting to stand next to it.
 */
export function publishedProtocols(
  announcedUrl: string | null | undefined,
  documents: readonly SessionDocument[],
): ProtocolsView {
  const announced = (announcedUrl ?? '').trim() === '' ? null : (announcedUrl ?? '').trim()
  const rows = documents.map(protocolRow)
  const count = rows.length

  if (count === 0) {
    return announced === null
      ? {
          state: 'none',
          rows,
          announced,
          summary: { key: 'recordings.noProtocolWritten' },
          note: null,
        }
      : {
          state: 'announcedOnly',
          rows,
          announced,
          summary: { key: 'recordings.protocolsAnnouncedOnly' },
          note: { key: 'recordings.protocolsAnnouncedOnlyNote' },
        }
  }

  if (announced === null) {
    return {
      state: 'announcedMissing',
      rows,
      announced,
      summary: { key: 'recordings.protocolsCount', params: { count } },
      note: { key: 'recordings.protocolsAnnouncedMissing', params: { count } },
    }
  }

  const carries = rows.some((row) => row.url === announced)
  return {
    state: carries ? 'complete' : 'announcedElsewhere',
    rows,
    announced,
    summary: { key: 'recordings.protocolsCount', params: { count } },
    note: carries ? null : { key: 'recordings.protocolsAnnouncedElsewhere' },
  }
}

/** Whether the section has anything at all to show. A meeting still being
 *  transcribed has not published anywhere yet, and that is an ordinary
 *  state rather than a failure. */
export function hasPublished(view: ProtocolsView): boolean {
  return view.state !== 'none'
}

/** Where the listing lives. */
export function sessionDocumentsPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/documents`
}
