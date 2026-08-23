/**
 * What the two reorder endpoints answer, and what a person has to be told
 * before and after asking for one.
 *
 * `POST /api/sessions/{id}/queue/priority` and
 * `POST /api/guilds/{id}/queue/priority` reply with the same body — a drag
 * and a quick action are one kind of event, "somebody changed the order
 * work will be done in", and the console reads them through one parser for
 * the same reason the API writes them through one serialiser.
 *
 * Three properties of that endpoint govern every sentence below, and none
 * of them are softened. Each one is a way this page could make an
 * administrator believe something untrue about their own queue:
 *
 * - **A reorder only ever holds sessions back.** Priority is one column
 *   shared by every guild in the deployment, so nothing can be moved
 *   forward; "go first" is expressed as everything that was ahead going
 *   second. The visible consequence is that **rows nobody touched show
 *   different numbers afterwards**, and the page must present that as the
 *   move working rather than as something having gone wrong. The invisible
 *   one is that holding a session back holds it back globally, behind other
 *   servers' untouched work as well as this server's, and a quick action
 *   that does it to a whole queue has to say so before it runs.
 * - **A stale drag is an ordinary outcome, not an error.** Two
 *   administrators reordering at once serialise; the second is answered
 *   `409` with `accepted: false` and **the queue as it now stands**. That
 *   is somebody else having moved something, which deserves a sentence and
 *   a redrawn list — not a dialog, and not the word "failed".
 * - **`changed` is the only thing that says whether anything happened.**
 *   An order that already held is answered with an empty one, and somebody
 *   who dropped a session back where it came from is owed "nothing to do"
 *   rather than "done".
 *
 * Every sentence is a {@link Message} keyed under `admin.queue.*`; see
 * `i18n/README.md` for why a module returns a key and never words.
 */
import type { Message } from './message'
import type { QueuedSession } from './queue'

/* -------------------------------------------------------------------- */
/* What the endpoints answer                                             */
/* -------------------------------------------------------------------- */

/** One session's place in the queue. `priority` is never null here, unlike
 *  the field of the same name on a queue listing: everything in this list
 *  has outstanding work by construction, which is what having a place
 *  means. */
export interface QueuePosition {
  sessionId: string
  priority: number
}

export interface QueueOrder {
  /** False when nothing was written. The only reason that happens is a
   *  queue that moved under the request. */
  accepted: boolean
  /** The API's own words for a refusal. Never displayed — this console
   *  writes its own sentences — but parsed so that a refusal without one
   *  is distinguishable from a refusal this console failed to read. */
  refusal: string | null
  /** The sessions this request actually moved. Empty is a real answer and
   *  means "the queue was already in this order". */
  changed: string[]
  /** The whole queue, in the order a worker will now reach it. */
  order: QueuePosition[]
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asId(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const text = value.trim()
  return text === '' ? null : text
}

/**
 * The order in a payload, or `null` when the payload is not one.
 *
 * Null is doing real work here and is not the defensive habit
 * `parseGuildQueue` deliberately avoids. Both a success and a stale drag
 * answer with this shape — the difference is `accepted`, not the body — so
 * the console asks for the body of every reply and decides from the shape
 * whether it was answered or refused for some other reason entirely. A
 * `404` carries `{"error": "no such session"}`, which has no order in it,
 * and reading that as an empty queue would redraw the page as though every
 * session had left it.
 */
export function parseQueueOrder(payload: unknown): QueueOrder | null {
  if (!isRecord(payload)) return null
  if (typeof payload.accepted !== 'boolean') return null
  if (!Array.isArray(payload.order)) return null
  const changed = Array.isArray(payload.changed) ? payload.changed : []
  return {
    accepted: payload.accepted,
    refusal: typeof payload.refusal === 'string' ? payload.refusal : null,
    changed: changed.flatMap((entry) => {
      const id = asId(entry)
      return id ? [id] : []
    }),
    order: payload.order.flatMap((entry) => {
      if (!isRecord(entry)) return []
      const id = asId(entry.session_id)
      const priority = entry.priority
      if (!id || typeof priority !== 'number' || !Number.isFinite(priority)) return []
      return [{ sessionId: id, priority: Math.round(priority) }]
    }),
  }
}

/** Where one session is placed relative to its neighbours. The id is
 *  escaped for the reason `queuePath` escapes its own: a string allowed to
 *  contain a slash is a string allowed to address a different endpoint. */
export function sessionPriorityPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/queue/priority`
}

/** Where a whole guild's queue is reordered by a rule. */
export function guildPriorityPath(guildId: string): string {
  return `/guilds/${encodeURIComponent(guildId)}/queue/priority`
}

/**
 * The rows the page is showing, renumbered by the order that came back.
 *
 * Applied rather than waited for. The answer carries the whole queue —
 * including, necessarily, the numbers of sessions nobody touched, because
 * that is how "go first" is expressed — so the list can settle into the
 * order the server just committed to without a round trip, and without the
 * moved row appearing to snap back while a re-read is in flight.
 *
 * **A session missing from the answer has left the queue**, not kept its
 * old place: the order is the whole of it, so an id that is not in it has
 * no outstanding work any more. Its priority becomes null, which is what
 * takes its handle away, and the row itself stays — a re-read decides
 * whether it is still worth listing at all, and that is not this function's
 * question.
 */
export function applyQueueOrder(
  sessions: readonly QueuedSession[],
  order: QueueOrder,
): QueuedSession[] {
  const places = new Map(order.order.map((position) => [position.sessionId, position.priority]))
  return sessions.map((session) => {
    const priority = places.get(session.id)
    if (priority === undefined) {
      return session.priority === null ? session : { ...session, priority: null }
    }
    return session.priority === priority ? session : { ...session, priority }
  })
}

/* -------------------------------------------------------------------- */
/* What to say once it has landed                                        */
/* -------------------------------------------------------------------- */

/** Whether a sentence about a reorder is news, a refusal, or neither. Used
 *  for the colour of the line and nothing else — the words say the same
 *  thing without it. */
export type ReorderTone = 'clear' | 'watch' | 'alarm'

export interface ReorderReport {
  message: Message
  tone: ReorderTone
  /** Whether the page should redraw from `order`. True even for a refusal:
   *  a 409's body is the queue as it now stands, which is exactly what a
   *  page that has just been told its picture is stale needs. */
  redraw: boolean
}

/**
 * What happened, in one sentence.
 *
 * The refusal is deliberately not worded as a failure. Nothing broke and
 * nobody did anything wrong: somebody else moved a session, or one
 * finished, between the list being drawn and the drop being sent. The
 * sentence therefore says what happened, that nothing was written, and
 * that the list underneath is now the real order — which is the only thing
 * that makes trying again a sensible act rather than a guess.
 *
 * `changed` separates "done" from "nothing to do". An administrator who
 * dropped a session back where it started, or pressed a quick action twice,
 * gets the second — because the alternative is a page that says "done"
 * about a request that did nothing, which is how somebody comes to believe
 * a reorder is in place that is not.
 */
export function reorderReport(order: QueueOrder): ReorderReport {
  if (!order.accepted) {
    return {
      message: { key: 'admin.queue.order.stale' },
      tone: 'watch',
      redraw: true,
    }
  }
  if (order.changed.length === 0) {
    return {
      message: { key: 'admin.queue.order.unchanged' },
      tone: 'clear',
      redraw: true,
    }
  }
  return {
    message: { key: 'admin.queue.order.moved', params: { count: order.changed.length } },
    tone: 'clear',
    redraw: true,
  }
}

/** `ApiError` names it `status`; a raw `$fetch` failure may name it
 *  `statusCode`; a request that never got a response has neither. The same
 *  reading `describeQueueError` does, restated here rather than shared
 *  because that one answers about a *read* and this one about a write, and
 *  the two sets of sentences are not interchangeable. */
function statusOf(error: unknown): number | null {
  if (!isRecord(error)) return null
  for (const candidate of [error.status, error.statusCode]) {
    if (typeof candidate === 'number' && Number.isFinite(candidate)) {
      return candidate === 0 ? null : candidate
    }
  }
  return null
}

/**
 * A reorder the API would not do, in a sentence somebody can act on.
 *
 * Built from the status alone, because that is all `ApiError` carries —
 * the API's own `{"error": …}` never reaches this console by design. The
 * 409 is not here on purpose: it is not a failure and never travels as
 * one, it is parsed as an order and reported by {@link reorderReport}.
 */
export function reorderFailure(error: unknown): Message {
  switch (statusOf(error)) {
    case 401:
      return { key: 'admin.queue.order.failedSignedOut' }
    case 403:
    case 404:
      // The API answers 404 both for a session that is gone and for a
      // guild the caller does not administer, and refuses to tell them
      // apart. One sentence has to cover both without guessing.
      return { key: 'admin.queue.order.failedGone' }
    case 400:
      // Nothing a person can type reaches this endpoint: the placement is
      // built from a list the page is holding, and the rule is one of two
      // literals. A 400 is therefore this console's bug, and saying so is
      // more use than a sentence that implies the reader mis-clicked.
      return { key: 'admin.queue.order.failedRefused' }
    case null:
      return { key: 'admin.queue.order.failedUnreachable' }
    default:
      return { key: 'admin.queue.order.failedUnknown' }
  }
}

/* -------------------------------------------------------------------- */
/* The two quick actions                                                 */
/* -------------------------------------------------------------------- */

/** A rule the guild-wide endpoint knows. Literals of the API's own
 *  registry: an unknown one is a 400 rather than a fallback, so this list
 *  and `sturnus.application.priorities.KNOWN_RULES` have to agree. */
export type QueueRuleName = 'many-participants-first' | 'short-recordings-first'

export interface QueueRule {
  rule: QueueRuleName
  /** The button. */
  nameKey: string
  /** What it ranks by, beside the button, before anything is pressed. */
  blurbKey: string
}

/**
 * The quick actions, in the order they are offered.
 *
 * Biggest-meeting-first leads because it is the one that answers the
 * question people actually arrive with — eight people are waiting on one
 * document and one person is waiting on another.
 */
export const QUEUE_RULES: readonly QueueRule[] = [
  {
    rule: 'many-participants-first',
    nameKey: 'admin.queue.rules.participants.name',
    blurbKey: 'admin.queue.rules.participants.blurb',
  },
  {
    rule: 'short-recordings-first',
    nameKey: 'admin.queue.rules.short.name',
    blurbKey: 'admin.queue.rules.short.blurb',
  },
]

export function findQueueRule(rule: string): QueueRule | null {
  return QUEUE_RULES.find((known) => known.rule === rule) ?? null
}

export interface RuleConfirmation {
  title: Message
  /** Said as separate sentences and kept separate. A paragraph carrying
   *  all of them is skimmed exactly where the reader most needs to notice
   *  that this reaches sessions which are not on the screen. */
  consequences: Message[]
  confirm: Message
}

/**
 * What a quick action will do, said before it does it.
 *
 * Pressing one of these is not like pressing a button on a row. It
 * rewrites the priority of **every** outstanding session in the server,
 * including the ones the list was cut short of showing, and it cannot be
 * undone by pressing it again — the numbers it wrote are the numbers that
 * stay, and the opposite rule would hold yet more work back rather than
 * putting anything back where it was. None of that is visible from the
 * button, so it is said in a panel that opens where the button is, in the
 * shape `ConsentCard` established for a decision somebody may not be able
 * to see the whole of.
 *
 * The unmeasured-audio caveat is part of the shortest-first confirmation
 * rather than a note somewhere on the page, because it is the difference
 * between that rule being useful and being a no-op: a session nothing has
 * ever transcribed has no measured length, ranks after every measured one,
 * and keeps the place it had. On a queue of fresh recordings the rule
 * therefore does very little, and somebody choosing it deserves to learn
 * that where they are choosing it rather than afterwards.
 */
export function ruleConfirmation(rule: QueueRuleName): RuleConfirmation {
  const shared: Message[] = [
    { key: 'admin.queue.rules.wholeServer' },
    { key: 'admin.queue.rules.neverForward' },
    { key: 'admin.queue.rules.notUndone' },
  ]
  if (rule === 'short-recordings-first') {
    return {
      title: { key: 'admin.queue.rules.short.confirmTitle' },
      consequences: [{ key: 'admin.queue.rules.short.unmeasured' }, ...shared],
      confirm: { key: 'admin.queue.rules.short.confirm' },
    }
  }
  return {
    title: { key: 'admin.queue.rules.participants.confirmTitle' },
    consequences: [{ key: 'admin.queue.rules.participants.ties' }, ...shared],
    confirm: { key: 'admin.queue.rules.participants.confirm' },
  }
}
