/**
 * The only error the console's API layer is allowed to throw.
 *
 * `ofetch` -- what `$fetch` is -- attaches the URL it called to every error
 * it raises. During a server-side render that URL is the in-cluster Service
 * address, and **Nuxt serialises a thrown error into the hydration payload
 * whether or not any page displays it.** A page can render entirely clean
 * visible text and still ship `http://sturnus-api:8080/...` inside
 * `window.__NUXT__`.
 *
 * That is a leak of the cluster's internal topology to every visitor,
 * caused by nothing the page author did wrong and visible nowhere they
 * would look. So the API layer never rethrows what `$fetch` threw: it
 * constructs one of these, carrying the status and the API path -- which
 * are the two things a page legitimately needs to decide what to say -- and
 * nothing else.
 */

/** What a failed call is allowed to tell a page. */
export interface FetchFailure {
  /** The HTTP status, or `0` when the request never got a response at all
   *  (a network failure, a DNS failure, a tunnel that is down). Zero is
   *  distinguishable from every real status, which is the point: "could
   *  not reach the API" and "the API said no" need different words. */
  status: number
}

/**
 * Extracts the status from whatever `$fetch` threw, and nothing else.
 *
 * Deliberately does not read `data`, `response`, `request` or the message.
 * `data` is the API's own JSON error body, which this project's API keeps
 * free of user input by construction -- but relying on that from here
 * would make one module's discipline load-bearing for another's, and the
 * status is enough to decide what to say.
 */
export function sanitiseFetchError(cause: unknown): FetchFailure {
  const status = (cause as { status?: unknown; statusCode?: unknown } | null)?.status
  const statusCode = (cause as { statusCode?: unknown } | null)?.statusCode
  const found = typeof status === 'number' ? status : statusCode
  return { status: typeof found === 'number' ? found : 0 }
}

export class ApiError extends Error {
  /** The API path, relative -- `/dashboard`, never the base it was joined
   *  to. Safe to display and safe to serialise. */
  readonly path: string
  readonly status: number

  constructor(path: string, failure: FetchFailure) {
    // The message is built from the two safe fields rather than from the
    // cause's own, which is the whole point of this class existing.
    super(`API request to ${path} failed with status ${failure.status}`)
    this.name = 'ApiError'
    this.path = path
    this.status = failure.status
  }

  /** Whether this failure means "nobody is signed in", as opposed to any
   *  other reason a call did not succeed. Only a 401 does. */
  get isUnauthenticated(): boolean {
    return this.status === 401
  }

  /** Whether the request never reached the API at all. */
  get isUnreachable(): boolean {
    return this.status === 0
  }
}
