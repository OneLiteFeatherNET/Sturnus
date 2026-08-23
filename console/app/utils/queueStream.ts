/**
 * Being told when the queue moves, and knowing when to go back to asking.
 *
 * Both queue views used to re-read the API on a timer — every five seconds
 * on the guild page, every three in the Transcription panel — and be told
 * "nothing changed" almost every time. The timer now lives on the server:
 * it re-reads the same snapshot, serialises it with the same function, and
 * sends an event only when the serialisation differs. What is left for the
 * browser is holding one connection open and reacting to what arrives on
 * it.
 *
 * A module rather than a composable, and for the same reason
 * `startQueuePolling` is one: everything below is a *decision* — when a
 * stream has failed rather than merely reconnected, when a connection that
 * has said nothing is a connection that never will, when to stop for good
 * — and a decision inside a component can only be tested by mounting one.
 * Here it is an ordinary function with a fake source and fake timers
 * pointed at it. The components import it directly, exactly as they import
 * `startQueuePolling` today.
 *
 * **The fallback is the point of the file, not a courtesy.** Sturnus is
 * served through a Cloudflare Tunnel and a reverse proxy, and an event
 * stream is the one response shape an intermediary can break without
 * breaking anything else: a proxy that buffers holds every event until the
 * response ends, which for these streams is ten minutes later. From the
 * browser that is indistinguishable from a server with nothing to say —
 * the connection is open, no error is raised, and nothing ever arrives. So
 * "nothing has arrived yet" is treated as a failure on a deadline, and an
 * administrator behind such a proxy is moved back to polling rather than
 * shown a page that will never update.
 *
 * Three ends are distinguished, because they lead to three different
 * sentences on the page and three different next actions:
 *
 * - **`rest`** — the server says the queue has come to rest and hung up.
 *   Nothing further will happen on its own, and the client must *not*
 *   reconnect. This is the ordinary end.
 * - **`gone`** — the queue stopped being readable: deleted, or this person
 *   no longer administers the guild. Also terminal, and reconnecting would
 *   be a tab retrying a 404 for the rest of the day.
 * - **anything else** — the connection dropped, or the server's ten-minute
 *   ceiling closed it. `EventSource` reconnects on its own, which is
 *   exactly right, so this is not an end at all until it has happened
 *   often enough in a row to mean something.
 */

/* -------------------------------------------------------------------- */
/* What a source looks like                                             */
/* -------------------------------------------------------------------- */

/**
 * As much of `EventSource` as this module uses.
 *
 * Written out rather than referring to the DOM type so the loop can be
 * driven by an object in a test. Nothing here inspects a source beyond
 * these three members.
 */
export interface EventSourceLike {
  addEventListener: (type: string, listener: (event: { data?: unknown }) => void) => void
  close: () => void
  /** `EventSource.CLOSED` is 2 and means the browser has given up rather
   *  than scheduled a retry — a non-2xx status does that, and a 404 is
   *  what a non-administrator gets. Optional because a fake need not have
   *  one, and an absent value is read as "still trying". */
  readyState?: number
}

/** `EventSource.CLOSED`. Named because a bare `2` in a condition about
 *  reconnection is unreadable. */
const CLOSED = 2

/* -------------------------------------------------------------------- */
/* What the caller is told                                              */
/* -------------------------------------------------------------------- */

/**
 * Which of the two ways of watching is in use, in the caller's words.
 *
 * This is rendered, not logged. A page that is live and a page that is
 * checking every few seconds look identical whenever the figures happen
 * not to be changing, and one of those two is several seconds behind — so
 * a reader has to be able to tell them apart without opening developer
 * tools.
 */
export type QueueStreamMode =
  /** Opening, or reconnecting after a drop. Nothing has arrived yet. */
  | 'connecting'
  /** A stream is open and events are arriving on it. */
  | 'live'
  /** The server said the queue has come to rest. Nothing more will
   *  arrive, and nothing should reconnect. */
  | 'rested'
  /** The queue is no longer readable — gone, or no longer administered. */
  | 'gone'
  /** Streaming failed or was never possible, and the caller should poll. */
  | 'polling'
  /** `stop()` was called. The end of a component's life, not of a queue. */
  | 'stopped'

export interface QueueStreamOptions {
  /** The absolute path of the stream endpoint, cookies and all — same
   *  origin, so the session cookie is attached without being asked. */
  url: string
  /** One snapshot, already parsed from its `data:` line. Shaped as
   *  `unknown` on purpose: this module knows nothing about queues, and the
   *  two callers parse the payload with the parsers they already have. */
  onSnapshot: (payload: unknown) => void
  /** Every change of mode, in order. The caller renders it. */
  onMode: (mode: QueueStreamMode) => void
  /** How a source is made. The default returns `null` where `EventSource`
   *  does not exist — during a server render, and in a browser too old
   *  for it — which is read as "cannot stream" and falls straight back. */
  open?: (url: string) => EventSourceLike | null
  /** How many consecutive failures, with no event in between, mean the
   *  stream is not going to work. Three rather than one: a single drop is
   *  ordinary and `EventSource` recovers from it without help. */
  maxFailures?: number
  /** How long a connection may say nothing before it is treated as
   *  broken. This is the buffering proxy above: open, silent, and never
   *  going to deliver. Eight seconds is comfortably longer than the
   *  server takes to send its first event, which it sends on connect. */
  firstEventMs?: number
  setTimer?: (callback: () => void, ms: number) => QueueStreamTimer
  clearTimer?: (handle: QueueStreamTimer) => void
}

/** A timer handle, without committing to a runtime — the same union and
 *  the same reason as `QueueTimer` in `~/utils/queue`: this module is
 *  compiled with both the Node and the DOM libraries in scope, and
 *  `ReturnType<typeof setTimeout>` resolves to whichever overload the
 *  checker reaches first. Nothing here ever inspects a handle. */
export type QueueStreamTimer = ReturnType<typeof setTimeout> | number

export interface QueueStreamHandle {
  /** Closes the source and stops for good. Safe to call more than once,
   *  and safe to call from inside a listener. */
  stop: () => void
  /** The current mode. Exposed for the tests, which is the whole reason
   *  this is a function and not fifteen lines in a component. */
  readonly mode: QueueStreamMode
}

/* -------------------------------------------------------------------- */
/* Reading what arrived                                                 */
/* -------------------------------------------------------------------- */

/**
 * The JSON on a `data:` line, or `null` if it was not JSON.
 *
 * `null` rather than a throw. A malformed frame is a defect somewhere
 * upstream, and the useful response to one is to ignore it and keep the
 * last good snapshot on the screen — a stream that tore itself down over a
 * single bad line would turn a cosmetic fault into a page that stopped
 * updating.
 */
export function parseStreamPayload(data: unknown): unknown | null {
  if (typeof data !== 'string') return null
  try {
    return JSON.parse(data) as unknown
  } catch {
    return null
  }
}

/**
 * Whether this many consecutive failures mean streaming is not working.
 *
 * "Consecutive" is the whole of it, and the counter is reset by every
 * event that arrives. The server closes a healthy stream every ten minutes
 * on purpose, so that a client reconnects and the server keeps no task for
 * a browser that has gone; a client that counted those closures as
 * failures would fall back to polling on a perfectly working connection,
 * ten minutes in, every time.
 */
export function shouldFallBack(consecutiveFailures: number, maxFailures: number): boolean {
  return consecutiveFailures >= maxFailures
}

/* -------------------------------------------------------------------- */
/* The loop                                                             */
/* -------------------------------------------------------------------- */

/** The default source: a real `EventSource`, or `null` where there is
 *  none to make. `withCredentials` is not set, and must not be: the
 *  console and its API share an origin, so the session cookie is attached
 *  as a matter of course, and asking for credentials on a same-origin
 *  request only adds a CORS mode nothing here needs. */
function defaultOpen(url: string): EventSourceLike | null {
  if (typeof EventSource === 'undefined') return null
  try {
    return new EventSource(url)
  } catch {
    // A browser that has the constructor and refuses the URL. Rare, and
    // the answer is the same as not having one at all.
    return null
  }
}

export function openQueueStream(options: QueueStreamOptions): QueueStreamHandle {
  const open = options.open ?? defaultOpen
  const setTimer = options.setTimer ?? setTimeout
  const clearTimer = options.clearTimer ?? clearTimeout
  const maxFailures = options.maxFailures ?? 3
  const firstEventMs = options.firstEventMs ?? 8000

  let mode: QueueStreamMode = 'connecting'
  let source: EventSourceLike | null = null
  let silenceTimer: QueueStreamTimer | null = null
  let failures = 0
  let alive = true

  function announce(next: QueueStreamMode) {
    if (mode === next) return
    mode = next
    options.onMode(next)
  }

  function disarmSilence() {
    if (silenceTimer !== null) {
      clearTimer(silenceTimer)
      silenceTimer = null
    }
  }

  function armSilence() {
    disarmSilence()
    silenceTimer = setTimer(() => {
      // Cleared first: this handle has fired and can no longer be
      // cancelled, so leaving it in place would make `disarmSilence`
      // believe it had cancelled something.
      silenceTimer = null
      if (!alive) return
      // Open, silent, and out of time. The most likely cause is an
      // intermediary buffering the response, which never raises an error
      // and never delivers anything either.
      finish('polling')
    }, firstEventMs)
  }

  /** Ends the stream in a named state, releasing everything it held. */
  function finish(next: QueueStreamMode) {
    alive = false
    disarmSilence()
    if (source) {
      source.close()
      source = null
    }
    announce(next)
  }

  function onMessage(event: { data?: unknown }) {
    if (!alive) return
    // Anything arriving at all is proof the path works, so both the
    // silence deadline and the failure count go back to nothing.
    disarmSilence()
    failures = 0
    announce('live')
    const payload = parseStreamPayload(event.data)
    if (payload !== null) options.onSnapshot(payload)
  }

  function onError() {
    if (!alive) return
    failures += 1
    // `CLOSED` means the browser has given up rather than scheduled a
    // retry — which is what a non-2xx answer produces, and 404 is what a
    // non-administrator gets. Waiting for two more failures that will
    // never come would leave the page watching nothing.
    const permanent = source?.readyState === CLOSED
    if (permanent || shouldFallBack(failures, maxFailures)) {
      finish('polling')
      return
    }
    // Otherwise `EventSource` is already reconnecting on its own, which is
    // the right thing to be doing, and the deadline is re-armed so that a
    // reconnection that never completes is not silence for ever.
    announce('connecting')
    armSilence()
  }

  let started: EventSourceLike | null
  try {
    started = open(options.url)
  } catch {
    // A browser that has the constructor and refuses anyway. Rare, and
    // the answer is the same as having no constructor at all.
    started = null
  }

  if (!started) {
    // No `EventSource` to be had — a server render, or a browser without
    // one. Reported as `polling` immediately rather than as an error: the
    // caller has a working way to read the queue and should use it.
    alive = false
    mode = 'polling'
    options.onMode('polling')
    return {
      stop: () => {},
      get mode() {
        return mode
      },
    }
  }

  source = started
  source.addEventListener('message', onMessage)
  // Terminal, both of them: the server has hung up and means it. Closing
  // here rather than letting the source reconnect is the whole difference
  // between a stream that ends and a tab that reopens one for ever.
  source.addEventListener('rest', () => finish('rested'))
  source.addEventListener('gone', () => finish('gone'))
  source.addEventListener('error', onError)
  armSilence()

  return {
    stop: () => {
      if (!alive) {
        // Already finished; nothing left to release. The mode is left as
        // it was, because "the queue came to rest" is still the true
        // account of why this stream ended.
        return
      }
      finish('stopped')
    },
    get mode() {
      return mode
    },
  }
}

/* -------------------------------------------------------------------- */
/* Saying which of the two it is                                        */
/* -------------------------------------------------------------------- */

/**
 * What to tell the reader about how this page is keeping itself current.
 *
 * One sentence, in the same place the page already said whether it was
 * watching. "Live" and "checking every few seconds" are several seconds
 * apart, and the difference is invisible whenever the figures happen not
 * to be changing — which is most of the time, and exactly when somebody is
 * deciding whether to believe what is on the screen.
 */
export function describeQueueMode(mode: QueueStreamMode, movingWord = 'Work is moving'): string {
  switch (mode) {
    case 'live':
      return `${movingWord} in this server, and it is being reported as it happens — the server sends an update the moment anything changes.`
    case 'connecting':
      return 'Connecting to the live feed. Until it arrives, what is on screen is the last thing read.'
    case 'polling':
      return `${movingWord}, and the live feed is unavailable — something between this page and the server does not carry one. Falling back to re-reading every few seconds, so figures may be a few seconds old.`
    case 'rested':
      return 'Nothing is queued or running, so the server closed the live feed. Refresh to check again.'
    case 'gone':
      return 'This queue is no longer readable — it may have been removed, or you may no longer administer this server. Refresh the page.'
    case 'stopped':
      return 'Not watching.'
  }
}

/**
 * The same distinction in three words, for a place that has no room for a
 * sentence.
 *
 * The Transcription panel appends this to a line that is already in a live
 * region, because pressing "Transcribe again" drops focus — the button
 * disables itself — and this line is one of the two things that change
 * afterwards. Somebody using a screen reader has to be told which way the
 * page is watching for the same reason everybody else does.
 */
export function queueWatchWords(mode: QueueStreamMode): string | null {
  switch (mode) {
    case 'live':
      return 'watching live'
    case 'connecting':
      return 'connecting to the live feed'
    case 'polling':
      return 'checking every few seconds'
    case 'rested':
    case 'gone':
    case 'stopped':
      // Nothing to append. The line these words hang off already says
      // what the queue is doing, and "not watching" beside a finished
      // queue reads as a fault rather than as the end of one.
      return null
  }
}
