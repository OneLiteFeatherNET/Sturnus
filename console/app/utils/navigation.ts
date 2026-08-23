/**
 * What the left navigation offers, and to whom.
 *
 * A module rather than a list inside the component, because which sections
 * exist -- and which are hidden from a non-administrator -- is a decision,
 * and a decision embedded in a template can only be tested by rendering
 * one.
 *
 * **Two groups, because there are two jobs.** "User View" is what a person
 * does with their own recordings; "Admin View" is what somebody does to the
 * system on behalf of a guild. Those were one flat list until the admin
 * side grew past a single entry, and a flat list is where "Settings" sits
 * next to "Calendar" as though changing the bot's configuration and looking
 * at your own meetings were the same kind of act. The grouping says which
 * hat the reader is wearing before they click anything.
 *
 * **Hiding is a courtesy, never a control.** Every administrative endpoint
 * checks administrator status itself. If this list and the API ever
 * disagree, the API is right, and the worst this can do is show somebody a
 * section that then refuses them.
 *
 * **Names are keys, not words.** Every label here is a translation key that
 * the template resolves through `$t`, never a sentence in any language. This
 * module is pure -- that is why it exists apart from the component, and why
 * `navigation.spec.ts` can assert what the navigation offers without
 * rendering anything. Threading a translator through it to get German out
 * would undo exactly that: every one of these functions would need a Vue
 * application, or a fixture pretending to be one, before it could answer a
 * question about which entries an administrator is offered. A key is data,
 * and data is what a pure function should return; turning data into words is
 * the template's job. The same rule governs every other module in
 * `app/utils` -- see `i18n/README.md`.
 */

export interface NavEntry {
  to: string
  /** A translation key, resolved by whoever renders this. Named `…Key` so
   *  that nothing puts it on screen by mistake. */
  labelKey: string
  /** An SVG path. Inline rather than an icon dependency: a handful of
   *  glyphs do not justify a package, and a self-contained path cannot go
   *  missing. */
  icon: string
  adminOnly?: boolean
}

/**
 * A named run of entries.
 *
 * `adminOnly` sits on the section as well as on its entries, so a section
 * whose every entry is administrative is hidden whole -- heading included.
 * A visible "Admin View" heading with nothing under it would announce the
 * existence of a section to exactly the person who may not have it.
 */
export interface NavSection {
  /** A translation key, like `NavEntry.labelKey`. The heading over a run of
   *  translated entries has to be translated too -- German entries under an
   *  English heading would read as a bug in the page rather than as a
   *  section that nobody got round to. */
  labelKey: string
  entries: readonly NavEntry[]
  adminOnly?: boolean
}

export const USER_VIEW: NavSection = {
  labelKey: 'nav.userView',
  entries: [
    {
      to: '/',
      labelKey: 'nav.dashboard',
      icon: 'M4 13h7V4H4v9Zm0 7h7v-5H4v5Zm9 0h7V11h-7v9Zm0-16v5h7V4h-7Z',
    },
    {
      to: '/recordings',
      labelKey: 'nav.recordings',
      icon: 'M12 3a3 3 0 0 1 3 3v6a3 3 0 1 1-6 0V6a3 3 0 0 1 3-3Zm7 9a7 7 0 0 1-6 6.93V21h-2v-2.07A7 7 0 0 1 5 12h2a5 5 0 0 0 10 0h2Z',
    },
    {
      to: '/calendar',
      labelKey: 'nav.calendar',
      icon: 'M7 2v2h10V2h2v2h1a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h1V2h2ZM4 9v11h16V9H4Z',
    },
  ],
}

export const ADMIN_VIEW: NavSection = {
  labelKey: 'nav.adminView',
  adminOnly: true,
  entries: [
    {
      to: '/admin/bot-settings',
      labelKey: 'nav.botSettings',
      icon: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Zm9.4 4a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2-1.2L16.5 3h-4l-.4 2.6c-.7.3-1.4.7-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1c.6.5 1.3.9 2 1.2l.4 2.6h4l.4-2.6c.7-.3 1.4-.7 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2Z',
      adminOnly: true,
    },
    {
      // Renamed from `/admin/user-settings`, which read as "settings for
      // users" and is in fact a roster of other people's consent. The old
      // address stays alive as a permanent redirect in `nuxt.config.ts`,
      // because it is in browser histories and in at least one runbook.
      to: '/admin/consents',
      labelKey: 'nav.consents',
      // Two people rather than one: this page is about the members of a
      // server, never about the account of whoever is reading it. A single
      // silhouette would read as "your profile", which is the one thing
      // this page is not -- and since `/settings` became the page that *is*
      // that, the distinction matters more than it did.
      icon: 'M16 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm-8 0a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5Zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5Z',
      adminOnly: true,
    },
    {
      to: '/admin/queue',
      labelKey: 'nav.queue',
      // A stack of layers waiting their turn, which is what a queue of
      // transcription jobs is. Deliberately not a clock or an hourglass:
      // both would say "this takes time", and the reason anybody opens
      // this page is that something has stopped taking time and started
      // taking none.
      icon: 'M12 2 2 7l10 5 10-5-10-5Zm0 20 10-5-2.5-1.25L12 19.5 4.5 15.75 2 17l10 5Zm0-5.5 10-5-2.5-1.25L12 14 4.5 10.25 2 11.5l10 5Z',
      adminOnly: true,
    },
    {
      to: '/admin/reporting',
      labelKey: 'nav.reporting',
      // Three bars of different heights, which is what the page's
      // by-month breakdown actually looks like. Deliberately not a pie:
      // this report is a run of months, and a pie would promise a
      // breakdown of a whole into named parts -- which is precisely the
      // per-person readout the page refuses to be.
      icon: 'M4 20V10h4v10H4Zm6 0V4h4v16h-4Zm6 0v-7h4v7h-4Z',
      adminOnly: true,
    },
  ],
}

export const NAV_SECTIONS: readonly NavSection[] = [USER_VIEW, ADMIN_VIEW]

/**
 * Every entry, in the order the sidebar renders them.
 *
 * The flat view is kept because "what does this console have pages for" is
 * a question with one answer, and answering it by walking two levels at
 * every call site is how the two levels get walked slightly differently.
 */
export const NAV_ENTRIES: readonly NavEntry[] = NAV_SECTIONS.flatMap(
  (section) => section.entries,
)

/** Nobody signed in sees no admin-only sections, which is also what an
 *  anonymous render gets. */
function permitted(viewer: { is_admin: boolean } | null, adminOnly?: boolean): boolean {
  return !adminOnly || Boolean(viewer?.is_admin)
}

/**
 * The sections this viewer should see, each already filtered.
 *
 * A section left with no entries is dropped rather than rendered empty: a
 * heading over nothing is a heading that says a section exists and is
 * withheld, which is more than the person is owed and less than they can
 * use.
 */
export function visibleSections(viewer: { is_admin: boolean } | null): NavSection[] {
  return NAV_SECTIONS.filter((section) => permitted(viewer, section.adminOnly))
    .map((section) => ({
      ...section,
      entries: section.entries.filter((entry) => permitted(viewer, entry.adminOnly)),
    }))
    .filter((section) => section.entries.length > 0)
}

/** The entries this viewer should see, flattened out of their sections. */
export function visibleEntries(viewer: { is_admin: boolean } | null): NavEntry[] {
  return visibleSections(viewer).flatMap((section) => section.entries)
}
