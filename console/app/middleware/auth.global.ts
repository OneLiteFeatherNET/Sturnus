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
const PUBLIC_ROUTES = new Set(['/sign-in'])

export default defineNuxtRouteMiddleware(async (to) => {
  if (PUBLIC_ROUTES.has(to.path)) return

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
