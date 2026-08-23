/**
 * Where the console is allowed to keep CSS.
 *
 * In one file, and that file is the design system: the brand palette, the
 * role tokens, and the rule that paints the page. Everything else is a
 * Tailwind utility or an inline binding of a token, which is how all but
 * two of this console's components were already written.
 *
 * The two that were not are the reason for the check rather than an
 * argument against it. A `<style scoped>` block is the cheapest thing in
 * the world to add and the most expensive to find later: it is invisible
 * from the template that depends on it, it duplicates decisions the tokens
 * already made, and — because it is scoped — it cannot be reused by the
 * next component that wants the same thing, so the next component writes
 * its own. Two blocks is how a codebase gets twenty.
 *
 * So the rule is mechanical: no `<style>` in any component, and no
 * component styling in `main.css`. Both halves matter. Banning the blocks
 * alone would just move the `.timeline-bar` rules into the global sheet,
 * which is worse — the same coupling, now with nothing scoping it.
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const APP = resolve(process.cwd(), 'app')
const CSS = readFileSync(resolve(process.cwd(), 'app/assets/css/main.css'), 'utf8')

function vueFiles(dir: string): string[] {
  const found: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) found.push(...vueFiles(path))
    else if (entry.name.endsWith('.vue')) found.push(path)
  }
  return found.sort()
}

const FILES = vueFiles(APP).map((path) => [
  relative(APP, path).replaceAll('\\', '/'),
  readFileSync(path, 'utf8'),
] as const)

describe('the components', () => {
  it('there is at least one of them to check', () => {
    // A directory walk that silently found nothing would make every
    // assertion below pass for the wrong reason.
    expect(FILES.length).toBeGreaterThan(10)
  })

  // A whole block, opened and closed, or a self-closing one pointing at a
  // file. Not a bare `<style>` anywhere in the source: several of these
  // components explain in prose why they no longer have one, and a check
  // that fails on being described is a check people delete.
  const BLOCK = /<style\b[^>]*(?:\/>|>[\s\S]*?<\/style>)/

  it.each(FILES)('%s carries no stylesheet of its own', (name, source) => {
    expect(BLOCK.test(source), `${name} has a <style> block`).toBe(false)
  })
})

describe('the one stylesheet', () => {
  // Prose is most of this file, and a selector inside a comment is not a
  // selector.
  const RULES = CSS.replace(/\/\*[\s\S]*?\*\//g, '')

  it('styles no component', () => {
    // A class selector here is component styling that moved house rather
    // than a design decision. The tokens are custom properties on `:root`;
    // the utilities come from Tailwind.
    const selectors = [...RULES.matchAll(/(\.[A-Za-z][\w-]*)[^{};]*\{/g)].map((match) => match[1])
    expect(selectors, `class selectors in main.css: ${selectors.join(', ')}`).toEqual([])
  })

  it('holds the palette, the roles and the page, and nothing else', () => {
    // Read as a list of what a reader of this file should expect to find.
    // Anything else that grows a block here is a decision worth arguing
    // about in review rather than one that arrives quietly.
    const blocks = [...RULES.matchAll(/([^{};]*)\{/g)].map((match) => (match[1] ?? '').trim())
    expect(blocks).toEqual([
      '@theme',
      ':root',
      '@media (prefers-color-scheme: dark)',
      ':root',
      'body',
    ])
  })
})
