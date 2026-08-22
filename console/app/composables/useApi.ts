/**
 * The one place the console talks to its API.
 *
 * Every call goes through here rather than each page reaching for `$fetch`
 * itself, for two reasons that are not stylistic:
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
 */
export function useApi() {
  const config = useRuntimeConfig()
  const headers = import.meta.server ? useRequestHeaders(['cookie']) : undefined
  const base = import.meta.server ? `${config.apiInternalBase}/api` : config.public.apiBase

  return <T>(path: string, options: Record<string, unknown> = {}) =>
    $fetch<T>(`${base}${path}`, {
      // Sends the cookie on same-origin browser requests. Without it the
      // API sees an anonymous caller and every page is a login screen.
      credentials: 'include',
      headers,
      ...options,
    })
}
