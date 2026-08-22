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
 * The value a token has in one theme.
 *
 * The light theme is the bare `:root` block and the dark theme is the one
 * inside the media query, which overrides it — so a token the dark block
 * does not mention keeps its light value, exactly as the cascade says.
 */
function tokens(theme: 'light' | 'dark'): Record<string, string> {
  const darkBlock = CSS.slice(CSS.indexOf('@media (prefers-color-scheme: dark)'))
  const source = theme === 'light' ? CSS.slice(0, CSS.indexOf('@media')) : darkBlock
  const found: Record<string, string> = theme === 'light' ? {} : { ...tokens('light') }
  for (const [, name, value] of source.matchAll(/(--[a-z-]+):\s*(#[0-9a-f]{6});/gi)) {
    if (name && value) found[name] = value
  }
  return found
}

const SURFACES = ['--surface', '--surface-raised', '--surface-sunken']
const THEMES = ['light', 'dark'] as const

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

describe('the stylesheet', () => {
  it('defines every role token in both themes', () => {
    // A token defined only under `prefers-color-scheme: dark` renders as
    // nothing at all in light mode — an invisible button rather than an
    // unreadable one.
    const light = tokens('light')
    const dark = tokens('dark')
    for (const role of [
      '--action',
      '--action-contrast',
      '--danger',
      '--danger-contrast',
      '--positive',
      '--positive-contrast',
      '--control-border',
    ]) {
      expect(light[role], `${role} is missing from the light palette`).toBeDefined()
      expect(dark[role], `${role} is missing from the dark palette`).toBeDefined()
    }
  })
})
