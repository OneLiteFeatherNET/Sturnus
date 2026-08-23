/**
 * Where a dragged session actually goes.
 *
 * All of it is arithmetic over lists of ids, and all of it is a way a
 * queue can end up in an order nobody asked for. The two that would be
 * hardest to notice, and which each have a test of their own below:
 *
 * - **The neighbour is read off the list with the dragged row taken out.**
 *   With it left in, "after the row above me" means one thing when a
 *   session moves up and a different thing when it moves down, and the
 *   off-by-one only appears when somebody drags downwards.
 * - **Nothing moved sends nothing.** A session picked up and put back
 *   would otherwise be a write the server answers `changed: []` to, and a
 *   page that has to word "done" after the fact.
 *
 * Nothing here touches an element, a pointer or a key. The component
 * decides which row the pointer is over; what that *means* is here, where
 * it can be asked without a browser.
 */
import { describe, expect, it } from 'vitest'

import {
  QUEUE_PAGE_SIZE,
  droppedBackMessage,
  grabSession,
  grabbedOrder,
  heldMessage,
  moveGrabBy,
  moveGrabTo,
  pageForGrab,
  pageOfIndex,
  pageSlice,
  pickedUpMessage,
  placementFor,
} from '../app/utils/queueReorder'

const QUEUE = ['a', 'b', 'c', 'd']

describe('picking a session up', () => {
  it('holds it where it already is', () => {
    expect(grabSession(QUEUE, 'c')).toEqual({ id: 'c', from: 2, to: 2 })
  })

  it('refuses a session that is not in this queue', () => {
    // Never a grab at index zero, which would move whichever row happened
    // to be first -- the one failure a reorder must not have, because it
    // looks exactly like the feature working.
    expect(grabSession(QUEUE, 'z')).toBeNull()
  })
})

describe('moving what is held', () => {
  it('steps one place at a time', () => {
    const grab = grabSession(QUEUE, 'c')!
    expect(moveGrabBy(grab, QUEUE.length, -1).to).toBe(1)
    expect(moveGrabBy(grab, QUEUE.length, 1).to).toBe(3)
  })

  it('refuses to leave the list rather than wrapping round it', () => {
    // A row at the top answering ArrowUp by jumping to the bottom has sent
    // the session to the far end of the queue on a keystroke somebody
    // pressed to check they were already at the top.
    const first = grabSession(QUEUE, 'a')!
    expect(moveGrabBy(first, QUEUE.length, -1).to).toBe(0)
    const last = grabSession(QUEUE, 'd')!
    expect(moveGrabBy(last, QUEUE.length, 1).to).toBe(3)
  })

  it('goes to a named position, clamped to the list', () => {
    const grab = grabSession(QUEUE, 'a')!
    expect(moveGrabTo(grab, QUEUE.length, 2).to).toBe(2)
    expect(moveGrabTo(grab, QUEUE.length, 99).to).toBe(3)
    expect(moveGrabTo(grab, QUEUE.length, -5).to).toBe(0)
  })

  it('previews the order the drop would produce', () => {
    const grab = moveGrabTo(grabSession(QUEUE, 'd')!, QUEUE.length, 1)
    expect(grabbedOrder(QUEUE, grab)).toEqual(['a', 'd', 'b', 'c'])
  })
})

describe('what the server is asked for', () => {
  it('names the ends as ends rather than as a neighbour', () => {
    // The two placements that stay true when the queue changes underneath
    // them. A session dropped at the front is meant to run first, and it
    // should still run first if two more arrive before the write lands.
    expect(placementFor(QUEUE, moveGrabTo(grabSession(QUEUE, 'c')!, 4, 0))).toEqual({
      place: 'first',
    })
    expect(placementFor(QUEUE, moveGrabTo(grabSession(QUEUE, 'a')!, 4, 3))).toEqual({
      place: 'last',
    })
  })

  it('names the neighbour from the list without the dragged row in it', () => {
    // Moving down. `b` landing at index 2 sits after `c`, not after `b`
    // itself -- which is what reading the neighbour off the untouched list
    // would have said.
    expect(placementFor(QUEUE, moveGrabTo(grabSession(QUEUE, 'b')!, 4, 2))).toEqual({
      place: 'after',
      session: 'c',
    })
  })

  it('names the neighbour the same way when the row moves up', () => {
    expect(placementFor(QUEUE, moveGrabTo(grabSession(QUEUE, 'd')!, 4, 1))).toEqual({
      place: 'after',
      session: 'a',
    })
  })

  it('produces the order it asked for', () => {
    // The property that ties the two halves together: what the preview
    // showed and what the placement means have to be the same list.
    for (const id of QUEUE) {
      for (let at = 0; at < QUEUE.length; at += 1) {
        const grab = moveGrabTo(grabSession(QUEUE, id)!, QUEUE.length, at)
        const placement = placementFor(QUEUE, grab)
        if (!placement) continue
        const rest = QUEUE.filter((other) => other !== id)
        const landed
          = placement.place === 'first'
            ? [id, ...rest]
            : placement.place === 'last'
              ? [...rest, id]
              : rest.flatMap((other) => (other === placement.session ? [other, id] : [other]))
        expect(landed).toEqual(grabbedOrder(QUEUE, grab))
      }
    }
  })

  it('asks for nothing at all when the row landed where it started', () => {
    expect(placementFor(QUEUE, grabSession(QUEUE, 'c')!)).toBeNull()
  })

  it('asks for nothing when the session has left the queue', () => {
    expect(placementFor(QUEUE, { id: 'z', from: 0, to: 2 })).toBeNull()
  })
})

describe('the window the queue is read through', () => {
  it('shows fewer rows than the recordings list, because a row here is a paragraph', () => {
    // Twenty would also be a pager that never appears: the API cuts the
    // list at twenty sessions, so a page of twenty is always page one of
    // one.
    expect(QUEUE_PAGE_SIZE).toBeLessThan(20)
  })

  it('counts pages from one', () => {
    expect(pageOfIndex(0, 5)).toBe(1)
    expect(pageOfIndex(4, 5)).toBe(1)
    expect(pageOfIndex(5, 5)).toBe(2)
  })

  it('follows a held row across a page boundary', () => {
    // Otherwise a keyboard move stops dead at the fifth row for a reason
    // that is about pagination and nothing to do with the queue -- and
    // "move this to the front" would be the one move the keyboard could
    // not make.
    expect(pageForGrab({ id: 'x', from: 0, to: 7 }, 1, 5)).toBe(2)
    expect(pageForGrab(null, 3, 5)).toBe(3)
  })

  it('cuts the list into the slice on screen', () => {
    expect(pageSlice([1, 2, 3, 4, 5, 6, 7], 2, 3)).toEqual([4, 5, 6])
    expect(pageSlice([1, 2], 9, 3)).toEqual([])
  })
})

describe('what a move sounds like', () => {
  it('says where a held row is now, as a position rather than a quantity', () => {
    // `1,024` is not a place. The same line `i18n/README.md` draws for
    // page numbers and years.
    expect(heldMessage({ id: 'a', from: 0, to: 3 }, 12)).toEqual({
      key: 'admin.queue.order.held',
      params: { position: '4', total: '12' },
    })
  })

  it('names what was picked up, once, when it is picked up', () => {
    expect(pickedUpMessage('#standup', { id: 'a', from: 1, to: 1 }, 4)).toEqual({
      key: 'admin.queue.order.pickedUp',
      params: { session: '#standup', position: '2', total: '4' },
    })
  })

  it('says a grab was abandoned rather than leaving it to be inferred', () => {
    // The list has been showing the row somewhere else, and the reader has
    // to know that undid itself rather than half-applying.
    expect(droppedBackMessage('#standup')).toEqual({
      key: 'admin.queue.order.droppedBack',
      params: { session: '#standup' },
    })
  })
})
