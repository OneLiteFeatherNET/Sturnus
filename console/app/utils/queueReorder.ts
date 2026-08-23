/**
 * Moving one session to a different place in the queue — the arithmetic,
 * with no pointer and no element anywhere near it.
 *
 * A drag is the one interaction in this console that has no meaning at all
 * until something has been decided about position, and every one of those
 * decisions is a way a queue can be reordered into something nobody asked
 * for. So they live here, where they are ordinary functions over lists of
 * ids, and the page is left with the two things that genuinely need a
 * browser: which element the pointer is over, and where focus goes.
 *
 * **A move is expressed relative to a neighbour, never as an index.** The
 * API refuses an index and it is right to: an index is an absolute claim
 * about a list the browser was showing a moment ago, and two
 * administrators dragging at once would each be numbering a different
 * list. `placementFor` therefore ends at `first`, `last`, or the id of the
 * session to sit beside — a sentence that still means something after
 * somebody else's move has landed.
 *
 * **A row is picked up, moved, and put down.** The alternative — one
 * request per arrow press — sends four writes to move a session four
 * places, and the three in the middle are orders nobody wanted the queue
 * to be in even briefly. So a {@link Grab} is a held position: the page
 * previews it, and one request is sent when it is dropped. A mouse drag
 * and a keyboard move produce the same `Grab` and therefore the same
 * request, which is what keeps the keyboard path from being a second,
 * lesser implementation that drifts.
 *
 * **Nothing moved is not the same as something moved back.** A grab whose
 * held position equals where it started yields no placement at all, so a
 * session dragged two pixels and released costs no request — rather than a
 * write the server would answer with `changed: []` and the page would have
 * to word as "nothing to do" after the fact.
 *
 * Every sentence here is a {@link Message}, keyed under `admin.queue.*`;
 * see `i18n/README.md` for why a module returns a key and never words.
 */
import type { Message } from './message'

/* -------------------------------------------------------------------- */
/* Where a session ends up                                               */
/* -------------------------------------------------------------------- */

/** The four things a drop can mean, and exactly the four the API takes. */
export type PlacementWhere = 'first' | 'last' | 'before' | 'after'

/**
 * A placement, in the shape `POST /sessions/{id}/queue/priority` reads.
 *
 * `session` is the neighbour to sit beside. It is required by `before` and
 * `after` and **refused** by `first` and `last` rather than ignored, so the
 * field is absent from those two rather than null: the endpoint treats a
 * placement that names both an end and a neighbour as somebody who
 * believes they said where, and refuses the whole request.
 */
export interface Placement {
  place: PlacementWhere
  session?: string
}

/**
 * A session picked up, and where it is currently being held.
 *
 * `from` and `to` are indexes into the queue's own order — the whole
 * queue, not the page of it on screen. Paging is a window, and a window
 * must not be able to change what a move means.
 */
export interface Grab {
  id: string
  from: number
  to: number
}

function clamp(value: number, last: number): number {
  return Math.min(Math.max(0, Math.floor(value)), Math.max(0, last))
}

/**
 * Picks a session up, or `null` when it is not in this queue.
 *
 * Null rather than a grab at index zero. A session that is not in the list
 * cannot be moved within it, and a grab that quietly meant "the first row"
 * would move whichever row happened to be first — which is the one failure
 * a reorder must not have, because it looks exactly like the feature
 * working.
 */
export function grabSession(ids: readonly string[], id: string): Grab | null {
  const at = ids.indexOf(id)
  if (at < 0) return null
  return { id, from: at, to: at }
}

/**
 * The same session held one place further up or down, refusing to leave
 * the list.
 *
 * Clamped rather than wrapped. A row at the top that answers ArrowUp by
 * jumping to the bottom has moved the session to the far end of the queue
 * on a keystroke somebody pressed to check they were already at the top.
 */
export function moveGrabBy(grab: Grab, total: number, delta: number): Grab {
  return { ...grab, to: clamp(grab.to + delta, total - 1) }
}

/** The same session held at a named position — a mouse drop, or the ends
 *  the Home and End keys ask for. */
export function moveGrabTo(grab: Grab, total: number, at: number): Grab {
  return { ...grab, to: clamp(at, total - 1) }
}

/**
 * The order this queue would be in if the grab were dropped now.
 *
 * What the page renders while a row is being held. It is a preview and
 * never a claim: nothing has been written until the drop is answered, and
 * the answer is the order the page then draws.
 */
export function grabbedOrder(ids: readonly string[], grab: Grab): string[] {
  const rest = ids.filter((id) => id !== grab.id)
  const at = clamp(grab.to, rest.length)
  return [...rest.slice(0, at), grab.id, ...rest.slice(at)]
}

/**
 * What to ask the server for, or `null` when the grab landed where it
 * started.
 *
 * The neighbour is read off the list **with the grabbed session taken
 * out**, which is the only reading that survives a move in either
 * direction: with it left in, "after the row above me" means one thing
 * moving up and a different thing moving down, and the off-by-one is
 * invisible until somebody drags a session downwards.
 *
 * The two ends are named as ends rather than as "after the last one",
 * because they are the two placements that stay true when the queue
 * changes underneath them. A session dropped at the front is meant to run
 * first, and it should still run first if two more sessions arrive between
 * the drop and the write.
 */
export function placementFor(ids: readonly string[], grab: Grab): Placement | null {
  if (!ids.includes(grab.id)) return null
  if (grab.to === grab.from) return null
  const rest = ids.filter((id) => id !== grab.id)
  const at = clamp(grab.to, rest.length)
  if (at === 0) return { place: 'first' }
  if (at >= rest.length) return { place: 'last' }
  const neighbour = rest[at - 1]
  if (neighbour === undefined) return null
  return { place: 'after', session: neighbour }
}

/* -------------------------------------------------------------------- */
/* The window the queue is read through                                  */
/* -------------------------------------------------------------------- */

/**
 * How many sessions of the queue are shown at once.
 *
 * Five, and not the twenty `~/utils/paging` uses for recordings. A row
 * there is a line in a list; a row here is a heading, a sentence saying
 * what will happen next without anybody doing anything, four counts and a
 * link — so twenty of them is several screens, which is the complaint this
 * pager exists to answer. Twenty would also be a pager that never appears:
 * the API cuts the list at twenty sessions, so a page of twenty is always
 * page one of one.
 */
export const QUEUE_PAGE_SIZE = 5

/** Which page a position in the whole queue falls on, counting from one. */
export function pageOfIndex(index: number, size: number = QUEUE_PAGE_SIZE): number {
  if (size <= 0) return 1
  return Math.floor(Math.max(0, index) / size) + 1
}

/**
 * The page the reader should be looking at while a row is held.
 *
 * The window follows the grab rather than the grab being confined to the
 * window. Otherwise a keyboard move stops dead at the fifth row for a
 * reason that is about pagination and nothing to do with the queue, and
 * "move this to the front" — the move somebody most often wants — would be
 * the one move the keyboard could not make.
 */
export function pageForGrab(grab: Grab | null, page: number, size: number = QUEUE_PAGE_SIZE): number {
  if (!grab) return page
  return pageOfIndex(grab.to, size)
}

/** The slice of the queue on one page. */
export function pageSlice<T>(rows: readonly T[], page: number, size: number = QUEUE_PAGE_SIZE): T[] {
  if (size <= 0) return [...rows]
  const from = Math.max(0, (Math.max(1, Math.floor(page)) - 1) * size)
  return rows.slice(from, from + size)
}

/* -------------------------------------------------------------------- */
/* What a move sounds like                                               */
/* -------------------------------------------------------------------- */

/**
 * What the keyboard does here, said on the control rather than left to be
 * discovered.
 *
 * A reorder that is only reachable by dragging is not a control at all for
 * anybody who does not drag, and one that is reachable by arrow keys
 * nobody mentions is barely better. The handle carries this sentence as
 * its description, so it is read out when the handle is focused and is on
 * screen for everybody else.
 */
export const REORDER_INSTRUCTIONS: Message = { key: 'admin.queue.order.instructions' }

/**
 * Where a held row is now, for the live region that says so after every
 * keystroke.
 *
 * Both numbers are strings rather than quantities: a position in a queue
 * is an ordinal, and `1,024` is not a place — the same line `i18n/README.md`
 * draws for page numbers and years.
 */
export function heldMessage(grab: Grab, total: number): Message {
  return {
    key: 'admin.queue.order.held',
    params: { position: String(grab.to + 1), total: String(total) },
  }
}

/** What was picked up, said once when it is picked up. */
export function pickedUpMessage(label: string, grab: Grab, total: number): Message {
  return {
    key: 'admin.queue.order.pickedUp',
    params: { session: label, position: String(grab.to + 1), total: String(total) },
  }
}

/** A grab abandoned. Says the row went back, because the page has been
 *  showing it somewhere else and the reader needs to know that undid
 *  itself rather than half-applied. */
export function droppedBackMessage(label: string): Message {
  return { key: 'admin.queue.order.droppedBack', params: { session: label } }
}
