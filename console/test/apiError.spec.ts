/**
 * What a failed API call is allowed to carry into a page -- and, more to
 * the point, what it must not.
 */
import { describe, expect, it } from 'vitest'

import { ApiError, sanitiseFetchError } from '../app/utils/apiError'

/** The shape `ofetch` actually throws, with the field that leaks. */
function ofetchError(status: number, url: string) {
  return Object.assign(new Error(`[GET] "${url}": ${status}`), {
    status,
    statusCode: status,
    request: url,
    response: { url, status },
    data: { error: 'not signed in' },
  })
}

const INTERNAL = 'http://sturnus-api:8080/api/dashboard'

describe('sanitising what fetch threw', () => {
  it('keeps the status', () => {
    expect(sanitiseFetchError(ofetchError(404, INTERNAL)).status).toBe(404)
  })

  it('keeps nothing else', () => {
    // The whole reason this function exists. Anything it returns can end
    // up in the hydration payload, so it must return exactly two things:
    // a status, and nothing.
    const sanitised = sanitiseFetchError(ofetchError(404, INTERNAL))
    expect(Object.keys(sanitised)).toEqual(['status'])
    expect(JSON.stringify(sanitised)).not.toContain('sturnus-api')
    expect(JSON.stringify(sanitised)).not.toContain('8080')
  })

  it('reports an unreachable API as status zero', () => {
    // A network failure carries no status at all. Zero is distinguishable
    // from every real one, because "could not reach the API" and "the API
    // said no" need different words in front of somebody.
    expect(sanitiseFetchError(new Error('fetch failed')).status).toBe(0)
    expect(sanitiseFetchError(null).status).toBe(0)
    expect(sanitiseFetchError(undefined).status).toBe(0)
  })

  it('reads a statusCode when there is no status', () => {
    expect(sanitiseFetchError({ statusCode: 503 }).status).toBe(503)
  })

  it('refuses a status that is not a number', () => {
    expect(sanitiseFetchError({ status: '404' }).status).toBe(0)
  })
})

describe('the error a page receives', () => {
  it('names the path it called, relative', () => {
    expect(new ApiError('/dashboard', { status: 500 }).path).toBe('/dashboard')
  })

  it('never carries the internal hostname, in any of its fields', () => {
    // Serialised in full, because Nuxt serialises a thrown error into the
    // hydration payload whether or not a page ever displays it -- so it is
    // the serialisation, not the rendering, that has to be clean.
    const error = new ApiError('/dashboard', sanitiseFetchError(ofetchError(404, INTERNAL)))
    const serialised = JSON.stringify({
      message: error.message,
      name: error.name,
      path: error.path,
      status: error.status,
      stack: '',
    })
    expect(serialised).not.toContain('sturnus-api')
    expect(serialised).not.toContain('8080')
    expect(error.message).not.toContain('sturnus-api')
  })

  it('distinguishes not-signed-in from every other failure', () => {
    // Only a 401 means anonymous. A 500 or a network blip bouncing a
    // signed-in person to the sign-in page -- where signing in then works
    // -- looks exactly like a random logout.
    expect(new ApiError('/me', { status: 401 }).isUnauthenticated).toBe(true)
    expect(new ApiError('/me', { status: 500 }).isUnauthenticated).toBe(false)
    expect(new ApiError('/me', { status: 0 }).isUnauthenticated).toBe(false)
  })

  it('distinguishes an unreachable API from one that answered', () => {
    expect(new ApiError('/me', { status: 0 }).isUnreachable).toBe(true)
    expect(new ApiError('/me', { status: 503 }).isUnreachable).toBe(false)
  })

  it('is an Error, so an unhandled one still behaves like one', () => {
    expect(new ApiError('/x', { status: 1 })).toBeInstanceOf(Error)
  })
})
