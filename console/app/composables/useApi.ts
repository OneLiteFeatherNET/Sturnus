/**
 * The one place the console talks to its API.
 *
 * Every call goes through here rather than each page reaching for `$fetch`
 * itself, for three reasons, none of them stylistic:
 *
 * - **The base differs between server and browser.** A server-side render
 *   reaches the API through the Kubernetes Service; the browser reaches it
 *   through a relative path on the same origin. Routing a server-side call
 *   back out through Cloudflare and in again would double the latency and
 *   fail entirely when the tunnel is the thing that is down.
 * - **The session cookie has to be forwarded on the server.** During SSR
 *   there is no browser to attach it, so the incoming request's cookie
 *   header is passed along by hand. Forget that in one page and it renders
 *   as signed-out for somebody who is signed in.
 * - **`$fetch`'s errors leak the internal hostname**, and the leak is
 *   invisible. See `ApiError` below.
 */
import { ApiError, sanitiseFetchError } from '~/utils/apiError'

export function useApi() {
  const config = useRuntimeConfig()
  const headers = import.meta.server ? useRequestHeaders(['cookie']) : undefined
  const base = import.meta.server ? `${config.apiInternalBase}/api` : config.public.apiBase

  return async <T>(path: string, options: Record<string, unknown> = {}): Promise<T> => {
    try {
      return await $fetch<T>(`${base}${path}`, {
        // Sends the cookie on same-origin browser requests. Without it the
        // API sees an anonymous caller and every page is a login screen.
        credentials: 'include',
        headers,
        ...options,
      })
    } catch (cause) {
      // Never rethrow what `$fetch` threw. `ofetch` puts the URL it called
      // into the error, and during SSR that URL is the in-cluster Service
      // address -- which Nuxt then serialises into the hydration payload
      // whether or not any page ever displays it. The page renders clean
      // text and ships `http://sturnus-api:8080/...` inside
      // `window.__NUXT__` regardless.
      //
      // Found on the dashboard, where a version-skew 404 put the internal
      // hostname into the HTML of a page whose visible text said nothing
      // of the sort. Fixing it per page would have meant remembering it on
      // every page ever added, so it is fixed here instead: the only error
      // this function can throw is one this module constructed.
      throw new ApiError(path, sanitiseFetchError(cause))
    }
  }
}
