/**
 * How the console moves between pages.
 *
 * ## Why there is a transition at all
 *
 * Every destination in this console fetches something, so until now a click
 * on the sidebar was followed by a still page for as long as the API took,
 * and only then by that page's own loading state. The complaint that
 * produces is "the navigation does not work", and the person making it is
 * right about what they saw: nothing acknowledged the click.
 *
 * ## Why it is this short
 *
 * A fade and two pixels of travel, 150 ms in and 75 ms out. The length is
 * the whole decision. A transition somebody can *notice* is a delay
 * somebody chose to add, and a quarter of a second of it on every
 * navigation is worse than the silence it replaces — this is a console
 * people page through, not a title sequence. `out-in` rather than
 * overlapping, because two pages laid over each other in normal document
 * flow is not a cross-fade, it is one page shoved down the screen by the
 * other.
 *
 * ## Why the classes live here rather than in `nuxt.config.ts`
 *
 * They are Tailwind utilities, and Tailwind scans `app/` — Nuxt 4's
 * `srcDir` — for the class names it needs to emit. A class list written in
 * `nuxt.config.ts` is one directory above that, so every rule in it would
 * be silently absent from the built stylesheet and the transition would do
 * nothing at all. Nothing about that failure is visible in a build log.
 *
 * They are utilities rather than a `.page-enter-active` rule for the reason
 * `test/stylesheets.spec.ts` states: `main.css` is the design system, and a
 * component — or a route — is not a design decision.
 *
 * ## `motion-reduce:` is not decoration here
 *
 * This is the one piece of motion in the console that a reader cannot avoid
 * by not looking at a panel: it happens on every navigation, to the whole
 * page. Somebody who has asked their system for reduced motion gets neither
 * the fade nor the travel — `transition-none` cancels the animation, and
 * `translate-y-0` makes sure the frame it would otherwise have started from
 * is not displaced either.
 */
export const PAGE_TRANSITION = {
  mode: 'out-in',
  enterActiveClass: 'transition duration-150 ease-out motion-reduce:transition-none',
  enterFromClass: 'opacity-0 translate-y-0.5 motion-reduce:translate-y-0',
  enterToClass: 'opacity-100 translate-y-0',
  leaveActiveClass: 'transition duration-75 ease-in motion-reduce:transition-none',
  leaveFromClass: 'opacity-100 translate-y-0',
  leaveToClass: 'opacity-0 translate-y-0.5 motion-reduce:translate-y-0',
} as const
