/**
 * What the reorder endpoints answer, and what a person is told about it.
 *
 * Two of these are worth more than the rest, and both are about a reply
 * that is not a success:
 *
 * - **A 409 carries an order, and an order is what it has to be read as.**
 *   Two administrators reordering at once serialise, and the loser gets
 *   the queue as it now stands. Reading that as a failure would throw away
 *   the one thing a page whose picture has just gone stale actually needs.
 * - **`{"error": "no such session"}` is not an empty queue.** It has no
 *   `order` in it, and a parser that shrugged and returned one would
 *   redraw the page as though every session had left the queue at once.
 *
 * The wording is asserted as keys rather than as sentences, because these
 * functions return keys -- see `i18n/README.md`. What the keys say is
 * `i18n.spec.ts`'s business, and that both files say it is too.
 */
import { describe, expect, it } from 'vitest'

import type { QueuedSession } from '../app/utils/queue'
import {
  QUEUE_RULES,
  applyQueueOrder,
  findQueueRule,
  guildPriorityPath,
  parseQueueOrder,
  reorderFailure,
  reorderReport,
  ruleConfirmation,
  sessionPriorityPath,
} from '../app/utils/queueOrder'

function session(id: string, priority: number | null): QueuedSession {
  return {
    id,
    channel_id: '555',
    channel_name: 'meeting',
    started_at: '2026-08-21T12:00:00+00:00',
    ended_at: '2026-08-21T13:00:00+00:00',
    status: 'closed',
    document_url: null,
    counts: { pending: 1, running: 0, done: 0, dead: 0 },
    priority,
  }
}

const ACCEPTED = {
  accepted: true,
  refusal: null,
  changed: ['512'],
  order: [
    { session_id: '77', priority: 0 },
    { session_id: '512', priority: 1 },
  ],
}

describe('reading what the endpoints answered', () => {
  it('reads a success', () => {
    expect(parseQueueOrder(ACCEPTED)).toEqual({
      accepted: true,
      refusal: null,
      changed: ['512'],
      order: [
        { sessionId: '77', priority: 0 },
        { sessionId: '512', priority: 1 },
      ],
    })
  })

  it('reads a stale drag as an order, because that is what it is', () => {
    const refused = parseQueueOrder({ ...ACCEPTED, accepted: false, changed: [], refusal: 'moved' })
    expect(refused?.accepted).toBe(false)
    expect(refused?.refusal).toBe('moved')
    expect(refused?.order).toHaveLength(2)
  })

  it('refuses a reply that is not an order at all', () => {
    // A 404's body. Reading it as an empty queue would redraw the page as
    // though every session had left at once.
    expect(parseQueueOrder({ error: 'no such session' })).toBeNull()
    expect(parseQueueOrder(null)).toBeNull()
    expect(parseQueueOrder('nonsense')).toBeNull()
    expect(parseQueueOrder({ accepted: true })).toBeNull()
  })

  it('drops a position it cannot read rather than inventing one', () => {
    const order = parseQueueOrder({
      accepted: true,
      refusal: null,
      changed: ['', 5, '9'],
      order: [{ session_id: '1' }, { priority: 2 }, { session_id: '3', priority: 2 }],
    })
    expect(order?.order).toEqual([{ sessionId: '3', priority: 2 }])
    expect(order?.changed).toEqual(['9'])
  })
})

describe('where a reorder is sent', () => {
  it('escapes an id, the same way every other path in this console does', () => {
    expect(sessionPriorityPath('512')).toBe('/sessions/512/queue/priority')
    expect(guildPriorityPath('../guilds/1')).toBe('/guilds/..%2Fguilds%2F1/queue/priority')
  })
})

describe('renumbering the rows from what came back', () => {
  it('gives every row the number the server just committed to', () => {
    // Including rows nobody touched. That is not a bug to be hidden: it is
    // what "nothing is ever moved forward" looks like from the page.
    const rows = applyQueueOrder([session('77', 0), session('512', 0)], parseQueueOrder(ACCEPTED)!)
    expect(rows.map((row) => [row.id, row.priority])).toEqual([
      ['77', 0],
      ['512', 1],
    ])
  })

  it('takes the place away from a session the order does not mention', () => {
    // The order is the whole queue, so an id missing from it has no
    // outstanding work any more -- and a row with no place must not offer
    // a handle.
    const rows = applyQueueOrder([session('99', 3)], parseQueueOrder(ACCEPTED)!)
    expect(rows[0]!.priority).toBeNull()
  })

  it('leaves a row it has nothing to say about exactly as it was', () => {
    const rows = [session('1', null)]
    expect(applyQueueOrder(rows, { accepted: true, refusal: null, changed: [], order: [] })[0]).toBe(
      rows[0],
    )
  })
})

describe('what to say once it has landed', () => {
  it('says how many places changed, and why untouched rows are among them', () => {
    expect(reorderReport(parseQueueOrder(ACCEPTED)!)).toEqual({
      message: { key: 'admin.queue.order.moved', params: { count: 1 } },
      tone: 'clear',
      redraw: true,
    })
  })

  it('says "nothing to do" rather than "done" when nothing moved', () => {
    // An administrator who dragged a session two pixels and put it back,
    // or pressed a quick action twice. Saying "done" about a request that
    // did nothing is how somebody comes to believe an order is in place
    // that is not.
    const report = reorderReport({ accepted: true, refusal: null, changed: [], order: [] })
    expect(report.message.key).toBe('admin.queue.order.unchanged')
  })

  it('reads a refusal as somebody else having moved something, not as a failure', () => {
    const report = reorderReport({ accepted: false, refusal: 'moved', changed: [], order: [] })
    expect(report.message.key).toBe('admin.queue.order.stale')
    // Redrawn from the refusal's own body, which is the queue as it now
    // stands. That is the whole reason the body is asked for.
    expect(report.redraw).toBe(true)
    expect(report.tone).not.toBe('alarm')
  })
})

describe('a reorder the API would not do', () => {
  it('tells a signed-out reader from one who no longer administers the server', () => {
    expect(reorderFailure({ status: 401 }).key).toBe('admin.queue.order.failedSignedOut')
    expect(reorderFailure({ status: 404 }).key).toBe('admin.queue.order.failedGone')
    expect(reorderFailure({ status: 403 }).key).toBe('admin.queue.order.failedGone')
  })

  it('says a malformed request is this console’s fault rather than the reader’s', () => {
    // Nothing a person can type reaches this endpoint: the placement comes
    // from a list the page is holding and the rule is one of two literals.
    expect(reorderFailure({ status: 400 }).key).toBe('admin.queue.order.failedRefused')
  })

  it('tells "could not reach it" from "it answered"', () => {
    expect(reorderFailure({ status: 0 }).key).toBe('admin.queue.order.failedUnreachable')
    expect(reorderFailure(new Error('offline')).key).toBe('admin.queue.order.failedUnreachable')
    expect(reorderFailure({ status: 500 }).key).toBe('admin.queue.order.failedUnknown')
  })
})

describe('the two quick actions', () => {
  it('names exactly the rules the API has', () => {
    // Literals of the API's own registry. An unknown one is a 400 rather
    // than a fallback, so this list and `KNOWN_RULES` have to agree.
    expect(QUEUE_RULES.map((rule) => rule.rule)).toEqual([
      'many-participants-first',
      'short-recordings-first',
    ])
    expect(findQueueRule('newest-first')).toBeNull()
  })

  it('says the three things a button cannot show, before it is pressed', () => {
    const keys = ruleConfirmation('many-participants-first').consequences.map((line) => line.key)
    // It reaches sessions the list was cut short of showing; it holds work
    // back rather than speeding anything up; and the other button does not
    // undo it.
    expect(keys).toContain('admin.queue.rules.wholeServer')
    expect(keys).toContain('admin.queue.rules.neverForward')
    expect(keys).toContain('admin.queue.rules.notUndone')
  })

  it('warns the shortest-first rule does little on a queue of fresh recordings', () => {
    // Null is not zero: a recording nothing has measured ranks after every
    // measured one and keeps the place it had. Somebody choosing this rule
    // deserves to learn that where they choose it, not afterwards.
    const confirmation = ruleConfirmation('short-recordings-first')
    expect(confirmation.consequences[0]!.key).toBe('admin.queue.rules.short.unmeasured')
    expect(confirmation.title.key).toBe('admin.queue.rules.short.confirmTitle')
  })
})
