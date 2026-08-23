/**
 * The console's front door.
 *
 * Global rather than per-page: a route that forgot to opt in would be a
 * page that renders somebody's data to nobody in particular. The allowlist
 * below is short and explicit, so adding a public page is a deliberate act
 * rather than an omission.
 *
 * This is a redirect, not a security boundary. Every endpoint the pages
 * call checks the session itself -- what this does is keep a signed-out
 * person from watching a dashboard fail to load, one panel at a time.
 */
import { isGuildSignInPath } from '~/utils/oauthClient'

const PUBLIC_ROUTES = new Set(['/sign-in'])

/**
 * Whether this page exists before a session does.
 *
 * Two answers, and the second one is a family rather than a path. A guild's
 * sign-in link is `/g/{slug}/sign-in` and the slug is whatever that guild
 * registered, so the allowlist cannot enumerate them — and it must not try
 * to, in either direction. It does not ask which slugs are registered,
 * because nothing here may; and it does not ask whether the slug is even
 * spelled like one, because a middleware that sent a malformed name to the
 * ordinary sign-in page and a registered name to the guild page would be a
 * one-request oracle for which organisations use this service, which is the
 * exact disclosure §2.2 exists to prevent. Every slug reaches the same
 * page, which hands them all to an endpoint that answers them all alike.
 */
function isPublic(path: string): boolean {
  return PUBLIC_ROUTES.has(path) || isGuildSignInPath(path)
}

export default defineNuxtRouteMiddleware(async (to) => {
  if (isPublic(to.path)) return

  // Only a 401 sends somebody to the sign-in page. An API that is down or
  // erroring is a different failure with a different remedy, and dressing
  // it up as "you are signed out" would send the person to log in again,
  // where it would work, and leave them with no idea what happened.
  // `loadSession` rethrows those, and Nuxt renders them as the error they
  // are.
  const user = await loadSession()
  if (!user) {
    return navigateTo('/sign-in')
  }
})
