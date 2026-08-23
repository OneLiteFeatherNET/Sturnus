/**
 * Whether the console's colours can actually be read.
 *
 * This test exists because the defect it pins survived for months in
 * plain sight: the brand cyan is 9.39:1 on the dark background and
 * 2.07:1 on the white one, and it was the colour of nearly every link
 * and every primary button. Whoever looked at it last was looking at the
 * theme where it works, which is the whole problem — a contrast failure
 * is invisible to the person who is not experiencing it.
 *
 * So the ratios are computed from the stylesheet itself rather than
 * asserted against hard-coded hexes. A future edit that darkens a surface
 * or brightens a role colour fails here rather than in somebody's eyes.
 *
 * The thresholds are WCAG 2.2 AA: 4.5:1 for text, 3:1 for the boundary of
 * a control when that boundary is the only thing that says a control is
 * there.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

// Resolved from the working directory rather than from `import.meta.url`:
// the tests run under happy-dom, where `import.meta.url` is not a `file:`
// URL and `fileURLToPath` refuses it.
const CSS = readFileSync(resolve(process.cwd(), 'app/assets/css/main.css'), 'utf8')

/** Relative luminance, WCAG 2.x. */
function luminance(hex: string): number {
  const value = hex.replace('#', '')
  const channels = [0, 2, 4].map((at) => Number.parseInt(value.slice(at, at + 2), 16) / 255)
  const linear = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
  return 0.2126 * (linear[0] ?? 0) + 0.7152 * (linear[1] ?? 0) + 0.0722 * (linear[2] ?? 0)
}

function contrast(a: string, b: string): number {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return ((high ?? 0) + 0.05) / ((low ?? 0) + 0.05)
}

/**
 * The three blocks the role tokens are written in, by the selector that
 * opens each.
 *
 * Since the console gained a theme chooser there is more than one way to be
 * dark, and both have to be checked. `:root` is light. The media block is
 * dark because the operating system is, and yields to somebody who asked
 * for light. The attribute block is dark because somebody asked for it,
 * under any operating system.
 *
 * `[data-theme="system"]` is deliberately absent: it means "let the media
 * query decide", so it matches no rule and has no palette of its own.
 */
const FORMS = {
  light: ':root {',
  'dark by system': ':root:not([data-theme="light"]) {',
  'dark by choice': ':root[data-theme="dark"] {',
} as const

type Form = keyof typeof FORMS

/**
 * The declarations one block makes, and only that block.
 *
 * Deliberately not "what a token resolves to": the point of parsing each
 * block on its own is to be able to see that a token is missing from one of
 * them, which is invisible the moment the cascade has filled it in.
 */
function declared(form: Form): Record<string, string> {
  const anchor = FORMS[form]
  const at = CSS.indexOf(anchor)
  if (at < 0) throw new Error(`main.css has no \`${anchor}\` block`)
  // None of these blocks nests, so the first closing brace ends it.
  const body = CSS.slice(at + anchor.length, CSS.indexOf('}', at))
  const found: Record<string, string> = {}
  for (const [, name, value] of body.matchAll(/(--[a-z-]+):\s*(#[0-9a-f]{6});/gi)) {
    if (name && value) found[name] = value
  }
  return found
}

/**
 * What a token actually resolves to in one form.
 *
 * Both dark forms sit on top of `:root`, so a token they do not mention
 * keeps its light value — exactly as the cascade says, and exactly the way
 * an unreadable control gets made.
 */
function tokens(form: Form): Record<string, string> {
  return form === 'light' ? declared('light') : { ...declared('light'), ...declared(form) }
}

const SURFACES = ['--surface', '--surface-raised', '--surface-sunken']
const THEMES = Object.keys(FORMS) as readonly Form[]

/** The AA floor for text, and for anything the size of body copy. */
const READABLE = 4.5
/** The AA floor for the boundary of a control. */
const DISCERNIBLE = 3

describe.each(THEMES)('the %s palette', (theme) => {
  const palette = tokens(theme)

  it.each(['--text', '--text-muted', '--action', '--danger'])(
    '%s is readable on every surface',
    (role) => {
      for (const surface of SURFACES) {
        const ratio = contrast(palette[role] ?? '', palette[surface] ?? '')
        expect(
          ratio,
          `${role} on ${surface} in ${theme} is ${ratio.toFixed(2)}:1`,
        ).toBeGreaterThanOrEqual(READABLE)
      }
    },
  )

  it.each([
    ['--action', '--action-contrast'],
    ['--danger', '--danger-contrast'],
    ['--positive', '--positive-contrast'],
  ])('a label on a %s fill is readable', (fill, label) => {
    // A filled button is two decisions, not one. Changing the fill and
    // leaving the label is how white text ends up on a colour it was
    // never checked against.
    const ratio = contrast(palette[fill] ?? '', palette[label] ?? '')
    expect(ratio, `${label} on ${fill} in ${theme} is ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
      READABLE,
    )
  })

  it('the edge of a control can be told from the surface behind it', () => {
    // `--border` is 1.36:1 in both themes, which is fine for a divider
    // between cards and not fine where a dashed outline is the entire
    // affordance of a button. That is what `--control-border` is for.
    for (const surface of SURFACES) {
      const ratio = contrast(palette['--control-border'] ?? '', palette[surface] ?? '')
      expect(
        ratio,
        `--control-border on ${surface} in ${theme} is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(DISCERNIBLE)
    }
  })
})

/** Every token the console draws itself with, as opposed to the brand
 *  colours in `@theme`, which are an identity rather than a role. */
const ROLE_TOKENS = [
  '--surface',
  '--surface-raised',
  '--surface-sunken',
  '--border',
  '--text',
  '--text-muted',
  '--action',
  '--action-contrast',
  '--danger',
  '--danger-contrast',
  '--positive',
  '--positive-contrast',
  '--control-border',
]

describe('the stylesheet', () => {
  it.each(THEMES)('defines every role token in the %s block itself', (form) => {
    // A token defined in one form and not the others is the failure this
    // whole file exists for. In its first shape it was a colour defined
    // only under `prefers-color-scheme: dark`, which renders as nothing at
    // all in light mode — an invisible button rather than an unreadable
    // one. Since the theme chooser it has a second shape: a token updated
    // in the media block and forgotten in the attribute block, so that
    // choosing dark explicitly gives a *different* dark from the one the
    // operating system gives.
    const block = declared(form)
    for (const role of ROLE_TOKENS) {
      expect(block[role], `${role} is missing from the ${form} block`).toBeDefined()
    }
  })

  it('gives the same dark to somebody who chose it as to somebody whose system did', () => {
    // The two dark blocks are two copies of the same eleven values, which
    // is a duplication somebody will eventually half-update. This is the
    // half-update failing.
    expect(declared('dark by choice')).toEqual(declared('dark by system'))
  })

  it('lets an explicit choice of dark outrank the system default', () => {
    // `:root[data-theme="dark"]` and `:root:not([data-theme="light"])`
    // have the same specificity, so which of them wins under a dark
    // operating system is decided by source order alone. Moving the
    // attribute block above the media query would leave a chosen dark that
    // works everywhere except where it is redundant — and would be
    // invisible to anybody testing on a dark machine.
    expect(CSS.indexOf(FORMS['dark by choice'])).toBeGreaterThan(
      CSS.indexOf(FORMS['dark by system']),
    )
  })

  it('defines no colour only inside a media query', () => {
    // A media query is a condition, not a definition. Anything whose only
    // value is inside one is a value that does not exist for the readers
    // the condition excludes.
    const outsideMedia = CSS.slice(0, CSS.indexOf('@media'))
    for (const role of ROLE_TOKENS) {
      expect(outsideMedia, `${role} is only defined inside a media query`).toContain(`${role}:`)
    }
  })
})
