/**
 * What the left navigation offers, and to whom.
 *
 * A module rather than a list inside the component, because which sections
 * exist -- and which are hidden from a non-administrator -- is a decision,
 * and a decision embedded in a template can only be tested by rendering
 * one.
 *
 * **Hiding is a courtesy, never a control.** Every settings endpoint checks
 * administrator status itself. If this list and the API ever disagree, the
 * API is right, and the worst this can do is show somebody a section that
 * then refuses them.
 */

export interface NavEntry {
  to: string
  label: string
  /** An SVG path. Inline rather than an icon dependency: four glyphs do not
   *  justify a package, and a self-contained path cannot go missing. */
  icon: string
  adminOnly?: boolean
}

export const NAV_ENTRIES: readonly NavEntry[] = [
  {
    to: '/',
    label: 'Dashboard',
    icon: 'M4 13h7V4H4v9Zm0 7h7v-5H4v5Zm9 0h7V11h-7v9Zm0-16v5h7V4h-7Z',
  },
  {
    to: '/recordings',
    label: 'Recordings',
    icon: 'M12 3a3 3 0 0 1 3 3v6a3 3 0 1 1-6 0V6a3 3 0 0 1 3-3Zm7 9a7 7 0 0 1-6 6.93V21h-2v-2.07A7 7 0 0 1 5 12h2a5 5 0 0 0 10 0h2Z',
  },
  {
    to: '/calendar',
    label: 'Calendar',
    icon: 'M7 2v2h10V2h2v2h1a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h1V2h2ZM4 9v11h16V9H4Z',
  },
  {
    to: '/settings',
    label: 'Settings',
    icon: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm9.4 4a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2-1.2L16.5 3h-4l-.4 2.6c-.7.3-1.4.7-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1c.6.5 1.3.9 2 1.2l.4 2.6h4l.4-2.6c.7-.3 1.4-.7 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2Z',
    adminOnly: true,
  },
]

/** The entries this viewer should see. Nobody signed in sees no admin-only
 *  sections, which is also what an anonymous render gets. */
export function visibleEntries(viewer: { is_admin: boolean } | null): NavEntry[] {
  return NAV_ENTRIES.filter((entry) => !entry.adminOnly || Boolean(viewer?.is_admin))
}
