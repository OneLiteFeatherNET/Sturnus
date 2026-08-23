<script setup lang="ts">
/**
 * The order a server's queued work will run in, and the two ways to change
 * it.
 *
 * Everything that is a *decision* is elsewhere and tested without a render:
 * where a dropped session goes and how that is expressed to the API is
 * `~/utils/queueReorder`, what the answer means and what a quick action has
 * to say before it runs is `~/utils/queueOrder`, and which page numbers to
 * offer is `~/utils/paging` by way of `UiPagination`. What is left here is
 * the half no module can hold: which row the pointer is over, where focus
 * goes when a row moves, and when to send the one request a whole gesture
 * produces.
 *
 * **The keyboard is not the fallback path, it is the same path.** A row is
 * picked up, moved and put down; the arrow keys and a mouse drag both
 * produce a `Grab`, and a `Grab` produces one placement. There is no second
 * implementation to drift, and no move a mouse can make that a keyboard
 * cannot — including the two that matter most, "put this at the front" and
 * "put this at the back", which the keyboard can make across the whole
 * queue while a mouse drag is confined to the page it started on. The
 * handle says all of that in its own description rather than leaving it to
 * be discovered.
 *
 * One request per gesture, not one per keystroke. Four arrow presses to
 * move a session four places would otherwise be four writes, and the three
 * in the middle are orders nobody wanted the queue to be in even briefly.
 *
 * **`UiDisclosureList` is deliberately not the base for this list.** That
 * control owns two things this list must not have: a disclosure, and a
 * selection that survives a page change. Nothing on a queue row is hidden —
 * the page argues at length that a row's state has to be readable without
 * hovering or expanding — and a selection outliving the page is exactly the
 * property that must not be reused here, because the position a row is
 * being dragged to is meaningless on a page the reader has left. Teaching
 * that component to reorder would also push a queue-only concern into a
 * control two other pages share. So this is an `<ol>`, which is what a list
 * whose order is its content already is.
 *
 * **A 409 is not an error here.** Two administrators reordering at once
 * serialise, and the loser is told so with the queue as it now stands
 * attached. That body is the whole point of asking for it, so the request
 * is sent with `ignoreResponseError` and the *shape* of the reply decides
 * whether it was answered: an order is an order whether or not it was
 * accepted, and everything else is a failure with a status to report.
 */
import { ApiError } from '~/utils/apiError'
import type { Message } from '~/utils/message'
import { pageCount } from '~/utils/paging'
import {
  QUEUE_TONE_COLOUR,
  queueSliceSummary,
  queuedSummary,
  type QueuedSession,
} from '~/utils/queue'
import {
  QUEUE_RULES,
  guildPriorityPath,
  parseQueueOrder,
  reorderFailure,
  reorderReport,
  ruleConfirmation,
  sessionPriorityPath,
  type QueueOrder,
  type QueueRuleName,
  type ReorderTone,
} from '~/utils/queueOrder'
import {
  QUEUE_PAGE_SIZE,
  REORDER_INSTRUCTIONS,
  droppedBackMessage,
  grabSession,
  grabbedOrder,
  heldMessage,
  moveGrabBy,
  moveGrabTo,
  pageForGrab,
  pageSlice,
  pickedUpMessage,
  placementFor,
  type Grab,
} from '~/utils/queueReorder'
import { channelNaming } from '~/utils/recordings'

const props = defineProps<{
  guildId: string
  /** The queue, already in the order a worker will reach it. */
  sessions: readonly QueuedSession[]
}>()

const emit = defineEmits<{
  /** An order the server committed to, refusal included. The page renumbers
   *  from it rather than this panel reaching into the page's own data. */
  order: [QueueOrder]
  /** Something changed that the whole page should re-read. */
  reload: []
}>()

const api = useApi()
const say = useSay()
const instructionsId = useId()

const page = ref(1)
const grab = ref<Grab | null>(null)
const busy = ref(false)
const confirming = ref<QueueRuleName | null>(null)

/** The last thing that happened, in a sentence. It lives on the page and
 *  not in a toast: what the queue did is part of the queue, and a message
 *  that fades is a message the person who looked away has lost. */
const report = ref<{ message: Message, tone: ReorderTone } | null>(null)

/** What is being held right now, said after every keystroke. Separate from
 *  `report` because they are two announcements with two jobs — one narrates
 *  a gesture in progress, the other says what came of it. */
const holding = ref<Message | null>(null)

/** The handles, so focus can be put back on one after its row has been
 *  rebuilt in a different page of the list. Vue moves an element that keeps
 *  its key, so an ordinary move keeps focus by itself; crossing a page
 *  boundary destroys and rebuilds the row, and nothing else would bring
 *  focus with it. */
const handles = new Map<string, HTMLElement>()

function keepHandle(id: string, element: unknown) {
  if (element instanceof HTMLElement) handles.set(id, element)
  else handles.delete(id)
}

const ids = computed(() => props.sessions.map((session) => session.id))
const total = computed(() => props.sessions.length)

/** The queue as it would be if what is held were dropped now. A preview,
 *  never a claim: nothing has been written until the server answers. */
const previewed = computed<QueuedSession[]>(() => {
  const current = grab.value
  if (!current) return [...props.sessions]
  const byId = new Map(props.sessions.map((session) => [session.id, session]))
  return grabbedOrder(ids.value, current).flatMap((id) => {
    const session = byId.get(id)
    return session ? [session] : []
  })
})

/**
 * The window follows the grab, so a move never stops at a page boundary
 * for a reason that has nothing to do with the queue.
 *
 * Clamped to the list rather than trusted, because the list shrinks on its
 * own: a session finishing while somebody is reading page three of a queue
 * that now has two would otherwise leave them looking at an empty section
 * under a pager that still offers three pages.
 */
const shownPage = computed(() =>
  Math.min(pageForGrab(grab.value, page.value), pageCount(props.sessions.length, QUEUE_PAGE_SIZE)),
)
const offset = computed(() => (shownPage.value - 1) * QUEUE_PAGE_SIZE)
const rows = computed(() => pageSlice(previewed.value, shownPage.value))
const summary = computed(() => queueSliceSummary(total.value, offset.value, rows.value.length))

/** The one open confirmation, already worded. Computed rather than called
 *  three times in the template, which would ask the same question three
 *  times and leave three places for the answer to be got wrong. */
const confirmation = computed(() =>
  confirming.value === null
    ? null
    : { rule: confirming.value, ...ruleConfirmation(confirming.value) },
)

function label(session: QueuedSession): string {
  return say(channelNaming(session).heading)
}

function sessionById(id: string): QueuedSession | undefined {
  return props.sessions.find((session) => session.id === id)
}

/** Where a row sits in the whole queue, not in the page of it on screen. A
 *  row that says "runs 2nd" on page three has told the reader nothing. */
function positionMessage(index: number): Message {
  return {
    key: 'admin.queue.order.position',
    params: { position: String(offset.value + index + 1), total: String(total.value) },
  }
}

/* ------------------------------------------------------------------ */
/* Picking a row up, moving it, putting it down                        */
/* ------------------------------------------------------------------ */

function pickUp(session: QueuedSession) {
  const picked = grabSession(ids.value, session.id)
  if (!picked) return
  grab.value = picked
  report.value = null
  holding.value = pickedUpMessage(label(session), picked, total.value)
}

async function focusHandle(id: string) {
  await nextTick()
  handles.get(id)?.focus()
}

/**
 * Holds the row somewhere else.
 *
 * `narrate` is false for a pointer drag and true for a keystroke, and the
 * difference is not cosmetic. A drag already shows where the row is, at
 * about sixty positions a second; announcing every one of them would bury
 * the one sentence that matters, and calling `focus()` mid-drag takes focus
 * off whatever the reader was actually on.
 */
function move(next: Grab, narrate: boolean) {
  grab.value = next
  if (!narrate) return
  holding.value = heldMessage(next, total.value)
  void focusHandle(next.id)
}

function nudge(delta: number) {
  const current = grab.value
  if (current) move(moveGrabBy(current, total.value, delta), true)
}

function moveToIndex(at: number, narrate: boolean) {
  const current = grab.value
  if (current) move(moveGrabTo(current, total.value, at), narrate)
}

/** A grab abandoned. Said out loud, because the list has been showing the
 *  row somewhere else and the reader has to know that undid itself rather
 *  than half-applied. */
function cancelGrab() {
  const current = grab.value
  if (!current) return
  const session = sessionById(current.id)
  grab.value = null
  holding.value = null
  report.value = {
    message: droppedBackMessage(session ? label(session) : current.id),
    tone: 'clear',
  }
  void focusHandle(current.id)
}

function onKeydown(event: KeyboardEvent, session: QueuedSession) {
  // A `<button>` turns Enter and Space into a click, so both are stopped
  // here and answered once rather than once per event.
  if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
    event.preventDefault()
    if (grab.value) void commit()
    else pickUp(session)
    return
  }
  if (!grab.value) return
  switch (event.key) {
    case 'Escape':
      event.preventDefault()
      cancelGrab()
      break
    case 'ArrowUp':
      event.preventDefault()
      nudge(-1)
      break
    case 'ArrowDown':
      event.preventDefault()
      nudge(1)
      break
    case 'Home':
      event.preventDefault()
      moveToIndex(0, true)
      break
    case 'End':
      event.preventDefault()
      moveToIndex(total.value - 1, true)
      break
  }
}

function onHandleClick(session: QueuedSession) {
  // Clicking any handle while something is held puts that thing down. The
  // alternative — swapping which row is held mid-gesture — is a click that
  // moves a session the reader was not looking at.
  if (grab.value) void commit()
  else pickUp(session)
}

/* ------------------------------------------------------------------ */
/* The mouse                                                           */
/* ------------------------------------------------------------------ */

function onDragStart(event: DragEvent, session: QueuedSession) {
  if (busy.value) return
  pickUp(session)
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    // Some browsers refuse to start a drag with nothing on the transfer.
    // It is never read back: the grab already knows what is held.
    event.dataTransfer.setData('text/plain', session.id)
  }
}

function onDragOver(event: DragEvent, index: number) {
  if (!grab.value) return
  // Without this the browser refuses the drop and `drop` never fires.
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  moveToIndex(offset.value + index, false)
}

function onDragEnd() {
  // Reached when a drag ended somewhere that is not a row. `commit` clears
  // the grab before it awaits anything, so a drop that already committed
  // leaves nothing here to cancel.
  if (grab.value) cancelGrab()
}

/* ------------------------------------------------------------------ */
/* Talking to the API                                                  */
/* ------------------------------------------------------------------ */

/**
 * One reorder, and the order it produced.
 *
 * `ignoreResponseError` is here for exactly one status. A 409 means the
 * queue moved under this request, and its body is the queue as it now
 * stands — the one thing a page that has just been told its picture is
 * stale actually needs. Discarding it and re-reading would ask the same
 * question twice and answer it with a list assembled a moment later.
 *
 * The status is captured on the way past rather than inferred, so a reply
 * that is not an order at all can still be reported as the refusal it is,
 * in the words its status deserves.
 */
async function send(path: string, body: Record<string, string>): Promise<QueueOrder> {
  let status = 200
  const payload = await api<unknown>(path, {
    method: 'POST',
    body,
    ignoreResponseError: true,
    onResponse: (context: { response?: { status?: number } }) => {
      if (typeof context.response?.status === 'number') status = context.response.status
    },
  })
  const order = parseQueueOrder(payload)
  if (!order) throw new ApiError(path, { status })
  return order
}

async function announce(run: () => Promise<QueueOrder>) {
  busy.value = true
  holding.value = null
  try {
    const order = await run()
    const outcome = reorderReport(order)
    report.value = { message: outcome.message, tone: outcome.tone }
    emit('order', order)
    // Re-read afterwards as well as renumbering from the answer. The order
    // says where everything sits; it does not say that a session has since
    // been documented and should no longer be listed at all.
    emit('reload')
  } catch (error) {
    report.value = { message: reorderFailure(error), tone: 'alarm' }
  } finally {
    busy.value = false
  }
}

async function commit() {
  const current = grab.value
  if (!current) return
  // Cleared before anything is awaited, so a `dragend` arriving after a
  // drop finds nothing to cancel and a second Enter cannot send the same
  // move twice.
  grab.value = null
  const placement = placementFor(ids.value, current)
  if (!placement) {
    const session = sessionById(current.id)
    holding.value = null
    // No request at all. A session dropped where it started would earn
    // `changed: []` and a sentence written after the fact; not asking is
    // both truer and cheaper.
    report.value = {
      message: session ? droppedBackMessage(label(session)) : { key: 'admin.queue.order.stale' },
      tone: 'clear',
    }
    void focusHandle(current.id)
    return
  }
  await announce(() =>
    send(sessionPriorityPath(current.id), {
      place: placement.place,
      // Absent rather than null for the two ends: the endpoint refuses a
      // placement that names both an end and a neighbour.
      ...(placement.session ? { session: placement.session } : {}),
    }),
  )
  void focusHandle(current.id)
}

async function runRule(rule: QueueRuleName) {
  confirming.value = null
  await announce(() => send(guildPriorityPath(props.guildId), { rule }))
}

/** A session that left the queue while it was being held. Nothing can be
 *  placed relative to a list it is no longer in, and holding on would leave
 *  a row on screen at a position the queue does not have. */
watch(ids, (current) => {
  const held = grab.value
  if (held && !current.includes(held.id)) {
    grab.value = null
    holding.value = null
    report.value = { message: { key: 'admin.queue.order.stale' }, tone: 'watch' }
  }
})

onBeforeUnmount(() => handles.clear())
</script>

<template>
  <section
    class="mb-8 rounded-xl border p-4"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
  >
    <h2 class="mb-1 text-sm font-semibold">{{ $t('admin.queue.list.queuedHeading') }}</h2>
    <p class="mb-4 text-xs" :style="{ color: 'var(--text-muted)' }">
      {{ $t('admin.queue.list.queuedNote') }}
    </p>

    <!-- The two quick actions, above the list they reorder. A control that
         rewrites what is underneath it belongs where the reader is already
         looking. -->
    <div
      class="mb-4 rounded-lg border p-3"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface-raised)' }"
    >
      <h3 class="mb-1 text-xs font-semibold">{{ $t('admin.queue.rules.heading') }}</h3>
      <p class="mb-3 text-xs" :style="{ color: 'var(--text-muted)' }">
        {{ $t('admin.queue.rules.note') }}
      </p>
      <div class="flex flex-wrap gap-2">
        <button
          v-for="rule in QUEUE_RULES"
          :key="rule.rule"
          type="button"
          class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface)] disabled:opacity-40"
          :style="{ borderColor: 'var(--border)' }"
          :disabled="busy || sessions.length === 0"
          :aria-expanded="confirming === rule.rule"
          @click="confirming = confirming === rule.rule ? null : rule.rule"
        >
          {{ $t(rule.nameKey) }}
        </button>
      </div>
      <!-- What each one ranks by, beside the button and not only inside the
           confirmation: choosing between two rules is a decision made
           before either is pressed. -->
      <p
        v-for="rule in QUEUE_RULES"
        :key="`blurb-${rule.rule}`"
        class="mt-2 text-xs"
        :style="{ color: 'var(--text-muted)' }"
      >
        <span class="font-medium">{{ $t(rule.nameKey) }}</span> — {{ $t(rule.blurbKey) }}
      </p>

      <!-- What it will do, before it does it, where the button that opened
           it is. A guild-wide reorder reaches sessions the list below was
           cut short of showing, and it is not undone by pressing the other
           button — neither of which is visible from a button. -->
      <section
        v-if="confirmation"
        class="mt-3 rounded-lg border p-3"
        :style="{ borderColor: 'var(--color-brand-yellow)', background: 'var(--surface)' }"
      >
        <h4 class="mb-2 text-sm font-semibold">{{ say(confirmation.title) }}</h4>
        <!-- Kept as separate sentences. One paragraph carrying all of them
             is skimmed exactly where the reader most needs to notice that
             this reaches rows they cannot see. -->
        <p
          v-for="consequence in confirmation.consequences"
          :key="consequence.key"
          class="mb-2 text-sm"
          :style="{ color: 'var(--text-muted)' }"
        >
          {{ say(consequence) }}
        </p>
        <div class="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            class="rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors hover:bg-[var(--surface-raised)] disabled:opacity-40"
            :style="{ borderColor: 'var(--color-brand-yellow)' }"
            :disabled="busy"
            @click="runRule(confirmation.rule)"
          >
            {{ busy ? $t('admin.queue.rules.working') : say(confirmation.confirm) }}
          </button>
          <button
            type="button"
            class="rounded-lg border px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)]"
            :style="{ borderColor: 'var(--border)' }"
            @click="confirming = null"
          >
            {{ $t('admin.queue.rules.cancel') }}
          </button>
        </div>
      </section>
    </div>

    <p class="mb-2 text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ say(queuedSummary(sessions)) }}
    </p>

    <!-- What the keyboard does, on the page rather than only in an
         announcement: a control reachable only by dragging is not a
         control, and one reachable by keys nobody mentions is barely
         better. -->
    <p :id="instructionsId" class="mb-3 text-xs" :style="{ color: 'var(--text-muted)' }">
      {{ say(REORDER_INSTRUCTIONS) }}
    </p>

    <!-- Two announcements, two jobs: one narrates a gesture in progress and
         is only ever heard, the other says what came of it and stays on the
         page for whoever looked away. -->
    <p class="sr-only" role="status" aria-live="assertive">{{ holding ? say(holding) : '' }}</p>
    <p
      v-if="report"
      class="mb-3 rounded-lg border p-3 text-sm"
      role="status"
      aria-live="polite"
      :style="{ borderColor: QUEUE_TONE_COLOUR[report.tone] }"
    >
      {{ say(report.message) }}
    </p>

    <p v-if="sessions.length === 0" class="text-sm" :style="{ color: 'var(--text-muted)' }">
      {{ $t('admin.queue.list.queuedNone') }}
    </p>

    <template v-else>
      <!-- An ordered list, because the order *is* the content. Twenty bare
           articles give assistive technology no count and no way to step
           between them, and this list additionally has to say which number
           each row carries. -->
      <ol
        class="overflow-hidden rounded-xl border"
        :style="{ borderColor: 'var(--border)', background: 'var(--surface-raised)' }"
        :start="offset + 1"
        :aria-busy="busy"
      >
        <li
          v-for="(session, index) in rows"
          :key="session.id"
          class="border-t first:border-t-0"
          :style="{
            borderColor: 'var(--border)',
            background: grab && grab.id === session.id ? 'var(--surface)' : 'transparent',
          }"
          draggable="true"
          @dragstart="onDragStart($event, session)"
          @dragover="onDragOver($event, index)"
          @drop.prevent="commit()"
          @dragend="onDragEnd()"
        >
          <QueueSessionRow :session="session">
            <template #lead>
              <div class="flex shrink-0 flex-col items-center gap-1">
                <button
                  :ref="(element) => keepHandle(session.id, element)"
                  type="button"
                  class="rounded-lg border px-2 py-1 text-sm transition-colors hover:bg-[var(--surface)] disabled:opacity-40"
                  :style="{
                    borderColor:
                      grab && grab.id === session.id
                        ? 'var(--color-brand-cyan)'
                        : 'var(--border)',
                  }"
                  :disabled="busy"
                  :aria-pressed="Boolean(grab && grab.id === session.id)"
                  :aria-label="$t('admin.queue.order.handle', { session: label(session) })"
                  :aria-describedby="instructionsId"
                  @keydown="onKeydown($event, session)"
                  @click="onHandleClick(session)"
                >
                  <span aria-hidden="true">⠿</span>
                </button>
                <span class="text-xs tabular-nums" :style="{ color: 'var(--text-muted)' }">
                  {{ say(positionMessage(index)) }}
                </span>
              </div>
            </template>
          </QueueSessionRow>
        </li>
      </ol>

      <div class="mt-4 flex flex-col items-center gap-2">
        <UiPagination
          :page="shownPage"
          :total="total"
          :size="QUEUE_PAGE_SIZE"
          :label="$t('admin.queue.list.pagerQueued')"
          @update:page="page = $event"
        />
        <p v-if="summary" class="text-sm" :style="{ color: 'var(--text-muted)' }">
          {{ say(summary) }}
        </p>
      </div>
    </template>
  </section>
</template>
