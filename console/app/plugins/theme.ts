/**
 * Puts the chosen theme on `<html>` before anything is painted.
 *
 * A plugin rather than a component, and this is the whole reason: a plugin
 * runs during the server render, so `data-theme` is in the HTML that leaves
 * the server. Setting it from a component's `onMounted` would mean the first
 * paint of every server-rendered navigation is the *other* theme, for as
 * long as hydration takes -- a white flash on the way into a dark console,
 * in front of the person who asked for dark precisely because white at
 * midnight is unpleasant.
 *
 * The cookie is what makes that possible: it travels with the request, so
 * the server knows the answer before it renders. See `utils/theme.ts`.
 *
 * `useHead` with a computed value rather than a one-time `document`
 * assignment, so that choosing a theme on `/settings` changes the page under
 * the person who chose it, without a reload.
 */
export default defineNuxtPlugin(() => {
  const { attribute } = useThemePreference()
  useHead({ htmlAttrs: { 'data-theme': attribute } })
})
