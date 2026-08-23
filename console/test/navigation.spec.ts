/**
 * Which navigation entries exist, who is offered which, and in which group.
 *
 * The list lives in its own module rather than inside the component for
 * exactly this reason: what belongs in the navigation is a decision, and a
 * decision embedded in a template can only be tested by rendering one.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  ADMIN_VIEW,
  NAV_ENTRIES,
  NAV_SECTIONS,
  USER_VIEW,
  visibleEntries,
  visibleSections,
} from '../app/utils/navigation'

/** Read rather than imported, the way `palette.spec.ts` reads the
 *  stylesheet: what ships is the file, so the file is what is checked. */
function messages(locale: 'en' | 'de'): Record<string, Record<string, string>> {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

const EN = messages('en')
const DE = messages('de')

const ADMIN = { is_admin: true }
const PARTICIPANT = { is_admin: false }

describe('the navigation', () => {
  it('separates what a person does with their own recordings from what an administrator does', () => {
    expect(NAV_SECTIONS.map((s) => s.labelKey)).toEqual(['nav.userView', 'nav.adminView'])
  })

  it('puts the personal sections first', () => {
    // A participant who administers nothing is the common case, and their
    // sections should not be below a heading they never see.
    expect(USER_VIEW.entries.map((e) => e.labelKey)).toEqual([
      'nav.dashboard',
      'nav.recordings',
      'nav.calendar',
    ])
  })

  it('every entry has an accessible name, collapsed or not', () => {
    // Collapsed, the sidebar is icons alone. An icon rail whose entries
    // announce nothing is a rail only its author can navigate.
    for (const entry of NAV_ENTRIES) {
      expect(entry.labelKey.trim()).not.toBe('')
      expect(entry.icon.trim()).not.toBe('')
    }
  })

  it('names every entry with a key rather than with English', () => {
    // The label a pure module returns is data, not words -- see the note at
    // the top of `navigation.ts`. A sentence that slipped back in here
    // would render identically in English and be untranslatable in German,
    // which is the failure that shows up only for a reader who does not
    // speak English.
    for (const named of [...NAV_ENTRIES, ...NAV_SECTIONS]) {
      expect(named.labelKey).toMatch(/^nav\.[a-z][A-Za-z]*$/)
    }
  })

  it('gives every entry and section a key of its own', () => {
    // Two entries sharing a key is one label that changes in two places at
    // once, which is how "Queue" ends up over the reporting page.
    const keys = [...NAV_ENTRIES, ...NAV_SECTIONS].map((named) => named.labelKey)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('has a translation for every key it names, in both languages', () => {
    // The whole point of a key is that something else turns it into words.
    // A key nothing translates renders as itself: `nav.reporting`, in the
    // sidebar, in every language.
    for (const messages of [EN, DE]) {
      for (const named of [...NAV_ENTRIES, ...NAV_SECTIONS]) {
        const [namespace, name] = named.labelKey.split('.')
        expect(messages[namespace!]?.[name!], `${named.labelKey} is untranslated`).toBeTruthy()
      }
    }
  })

  it('addresses every entry with a path of its own', () => {
    const paths = NAV_ENTRIES.map((e) => e.to)
    expect(new Set(paths).size).toBe(paths.length)
  })
})

describe('who is offered the Admin View', () => {
  it('marks every entry under it administrative, not just the section', () => {
    // The section flag hides the heading; the entry flags are what
    // `visibleEntries` filters on. A section marked administrative whose
    // entries are not would leak its entries to any caller that flattens
    // first and filters second.
    for (const entry of ADMIN_VIEW.entries) {
      expect(entry.adminOnly).toBe(true)
    }
  })

  it('offers the bot settings to an administrator', () => {
    expect(visibleEntries(ADMIN).map((e) => e.labelKey)).toContain('nav.botSettings')
  })

  it('offers the consent roster to an administrator', () => {
    expect(visibleEntries(ADMIN).map((e) => e.labelKey)).toContain('nav.consents')
  })

  it('names the consent roster for what it is rather than for whom it is about', () => {
    // It was `/admin/user-settings`, labelled "User Settings", which reads
    // as "settings for users" and is in fact a roster of other people's
    // consent -- and which since `/settings` became a real page competes
    // with the one address that *is* a person's own settings.
    const entry = ADMIN_VIEW.entries.find((e) => e.labelKey === 'nav.consents')!
    expect(entry.to).toBe('/admin/consents')
  })

  it('offers the destinations to an administrator', () => {
    expect(visibleEntries(ADMIN).map((e) => e.labelKey)).toContain('nav.destinations')
  })

  it('hides the destinations from somebody who administers nothing', () => {
    // Every route under `/api/guilds/{id}/export-targets` answers 404 to a
    // non-administrator — the same answer it gives for a guild that does
    // not exist — so an entry left visible would offer a page that can
    // only ever refuse them.
    expect(visibleEntries(PARTICIPANT).map((e) => e.labelKey)).not.toContain('nav.destinations')
    expect(visibleEntries(null).map((e) => e.labelKey)).not.toContain('nav.destinations')
  })

  it('offers the queue to an administrator', () => {
    expect(visibleEntries(ADMIN).map((e) => e.labelKey)).toContain('nav.queue')
  })

  it('offers the reporting to an administrator', () => {
    expect(visibleEntries(ADMIN).map((e) => e.labelKey)).toContain('nav.reporting')
  })

  it('hides the reporting from somebody who administers nothing', () => {
    // The report describes a whole server -- how much of it was recorded,
    // how much of that was written up -- which is a thing an administrator
    // is accountable for and a thing a participant has no standing to
    // read. The endpoint answers 404 to them, the same answer it gives for
    // a server that does not exist, so an entry left visible would offer a
    // page that can only ever refuse them.
    expect(visibleEntries(PARTICIPANT).map((e) => e.labelKey)).not.toContain('nav.reporting')
    expect(visibleEntries(null).map((e) => e.labelKey)).not.toContain('nav.reporting')
  })

  it('hides the queue from somebody who administers nothing', () => {
    // The queue endpoint answers 404 to a non-administrator -- the same
    // answer it gives for a guild that does not exist -- so an entry left
    // visible would offer a page that can only ever refuse them.
    expect(visibleEntries(PARTICIPANT).map((e) => e.labelKey)).not.toContain('nav.queue')
    expect(visibleEntries(null).map((e) => e.labelKey)).not.toContain('nav.queue')
  })

  it('lists the daily work first and the thing gone wrong last', () => {
    // The configuration is what an administrator comes to the Admin View
    // for daily; a member's consent is looked at when somebody asks about
    // that one member, which is rarer and always deliberate. The queue is
    // rarer still and is never scanned -- it is opened because a protocol
    // did not appear, which is a question somebody arrives holding.
    // Reporting is last because it is the only entry nothing is ever
    // wrong on: it is read on a schedule, or when somebody asks how much
    // this server actually uses Sturnus, and never in a hurry.
    //
    // Destinations sits second, next to the configuration it is part of:
    // where a guild's protocols go was a text field on Bot Settings until
    // it became a page, and `document_target` is still the fallback for a
    // guild that configures nothing here. Two adjacent entries is what
    // stops them reading as rival settings.
    //
    // Server Setup is the one entry that breaks the frequency rule, and it
    // is first. Everything below it is read by somebody who already has a
    // working server; that page is read by somebody who has none, and is
    // the prerequisite of every other entry here -- there is nothing to
    // configure, no consent to look at, no queue and no report until a
    // server has been set up. An entry needed exactly once, by the person
    // who knows this console least, cannot be the fifth thing in a list.
    // It sits beside Bot Settings for the same reason Destinations does:
    // finishing setup lands you there.
    expect(ADMIN_VIEW.entries.map((e) => e.labelKey)).toEqual([
      'nav.onboarding',
      'nav.botSettings',
      'nav.destinations',
      'nav.consents',
      'nav.queue',
      'nav.reporting',
    ])
  })

  it('hides it from somebody who administers nothing', () => {
    expect(visibleSections(PARTICIPANT).map((s) => s.labelKey)).toEqual(['nav.userView'])
  })

  it('hides it when nobody is signed in', () => {
    expect(visibleSections(null).map((s) => s.labelKey)).toEqual(['nav.userView'])
  })

  it('never renders a heading over an empty section', () => {
    // A visible "Admin View" with nothing under it would announce the
    // existence of a section to exactly the person who may not have it.
    for (const viewer of [ADMIN, PARTICIPANT, null]) {
      for (const section of visibleSections(viewer)) {
        expect(section.entries.length).toBeGreaterThan(0)
      }
    }
  })

  it('shows every non-administrative section to everyone', () => {
    // Hiding a section is a courtesy to the person looking at the screen,
    // never a control -- so it applies to exactly the entries whose
    // endpoints refuse a non-administrator, and to nothing else.
    const forEveryone = NAV_ENTRIES.filter((e) => !e.adminOnly).map((e) => e.labelKey)
    expect(visibleEntries(PARTICIPANT).map((e) => e.labelKey)).toEqual(forEveryone)
  })

  it('offers an administrator everything a participant is offered, and more', () => {
    const asParticipant = visibleEntries(PARTICIPANT).map((e) => e.labelKey)
    const asAdmin = visibleEntries(ADMIN).map((e) => e.labelKey)
    for (const label of asParticipant) {
      expect(asAdmin).toContain(label)
    }
    expect(asAdmin.length).toBeGreaterThan(asParticipant.length)
  })
})
