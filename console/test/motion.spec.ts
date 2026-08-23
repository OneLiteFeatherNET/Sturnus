/**
 * Whether anything in this console moves when its reader has asked for
 * nothing to move.
 *
 * `prefers-reduced-motion: reduce` is not a preference about taste. It is
 * set by people for whom a sliding panel or a pulsing block produces
 * nausea, and this console is where somebody goes to re-listen to a
 * meeting they were in — often because something in it went wrong. Nobody
 * should have to brace for the navigation to find out.
 *
 * The rule is a rule rather than a habit because a habit does not survive
 * the next page. Motion is added one `animate-pulse` at a time, by whoever
 * is building the thing that needs a loading state, and each one of them
 * looks harmless on its own. So the check reads the source rather than the
 * render: every `.vue` file under `app/` that animates something has to say
 * what happens when animation is refused.
 *
 * **What counts as motion here.** An `animate-*` keyframe animation, and a
 * `transition-*` given an explicit `duration-*`, and a `<Transition>`
 * element. A bare `transition-colors` on a hover state is deliberately not
 * on the list: it fades a colour over 150 ms and moves nothing, and
 * `prefers-reduced-motion` is about movement. Sweeping it in would put a
 * `motion-reduce:` on two dozen buttons and teach everybody to add the
 * class without reading why.
 *
 * `palette.spec.ts` is the precedent: what ships is the file, so the file
 * is what is checked.
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

// Resolved from the working directory rather than from `import.meta.url`:
// the tests run under happy-dom, where `import.meta.url` is not a `file:`
// URL and `fileURLToPath` refuses it.
const APP = resolve(process.cwd(), 'app')
const MOTION = readFileSync(resolve(APP, 'utils/motion.ts'), 'utf8')

/**
 * Files that animate without a `motion-reduce:` answer, and why that is
 * allowed to stand.
 *
 * It lives here rather than in a config file so that growing it is a diff
 * a reviewer reads. An entry is a promise that somebody looked; the test
 * below refuses a stale one, so a file that stops animating cannot keep
 * its excuse.
 */
const EXEMPT: Record<string, string> = {
  'components/RequeuePanel.vue': 'owned by an open pull request',
}

function vueFiles(dir: string): string[] {
  const found: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) found.push(...vueFiles(path))
    else if (entry.name.endsWith('.vue')) found.push(path)
  }
  return found.sort()
}

const FILES = vueFiles(APP).map((path) => ({
  name: relative(APP, path).replaceAll('\\', '/'),
  source: readFileSync(path, 'utf8'),
}))

/**
 * Every class list in the file, static or bound.
 *
 * `class="…"`, `:class="…"` and `active-class="…"` all match, because all
 * three put class names on an element. A bound one may hold a ternary or
 * an object, which is why the checks below run regexes over the whole
 * attribute rather than over tokens: `collapsed ? 'w-16' : 'w-16 sm:w-56'`
 * does not split on whitespace into class names.
 */
function classLists(source: string): string[] {
  return [...source.matchAll(/\bclass="([^"]*)"/g)].map((match) => match[1] ?? '')
}

/** A keyframe animation — `animate-pulse`, `animate-spin`. Not `animate-none`. */
const ANIMATES = /\banimate-(?!none\b)[a-z0-9[\]_.-]+/
/** A transition given an explicit length, which is what makes it visible. */
const TRANSITIONS = /\btransition(?:-[a-z[\]_-]+)?\b/
const HAS_DURATION = /\bduration-(?:\d+|\[)/

const STOPS_ANIMATION = /\bmotion-reduce:animate-none\b/
const STOPS_TRANSITION = /\bmotion-reduce:(?:transition-none|duration-0)\b/

/** What is wrong with a class list, if anything. */
function unanswered(classList: string): string | null {
  if (ANIMATES.test(classList) && !STOPS_ANIMATION.test(classList)) {
    return `animates without motion-reduce:animate-none — ${classList}`
  }
  if (
    TRANSITIONS.test(classList)
    && HAS_DURATION.test(classList)
    && !STOPS_TRANSITION.test(classList)
  ) {
    return `transitions without motion-reduce:transition-none — ${classList}`
  }
  return null
}

/** Every complaint against one file. */
function complaints(source: string): string[] {
  const found = classLists(source).map(unanswered).filter((note): note is string => note !== null)
  // A `<Transition>` element is motion the class lists cannot describe:
  // its classes are named by convention or passed as props, so the file
  // is asked for a `motion-reduce:` somewhere rather than for a precise one.
  if (/<Transition[\s>]/.test(source) && !/\bmotion-reduce:/.test(source)) {
    found.push('renders a <Transition> and never mentions motion-reduce:')
  }
  return found
}

describe('every moving thing in the console', () => {
  it.each(FILES.filter((file) => !(file.name in EXEMPT)).map((file) => [file.name, file.source]))(
    '%s stops moving when the reader has asked it to',
    (name, source) => {
      expect(complaints(source), `${name}: ${complaints(source).join('; ')}`).toEqual([])
    },
  )
})

describe('the list of files allowed to move anyway', () => {
  it.each(Object.entries(EXEMPT))('%s still needs its exemption (%s)', (name) => {
    // An exemption nobody removed is an exemption nobody re-read. When the
    // file stops animating — or grows the variant — this fails and the
    // entry goes.
    const file = FILES.find((candidate) => candidate.name === name)
    expect(file, `${name} is on the exemption list but is not a file under app/`).toBeDefined()
    expect(
      complaints(file?.source ?? ''),
      `${name} no longer animates without an answer — take it off EXEMPT`,
    ).not.toEqual([])
  })

  it('is short enough that somebody reads it', () => {
    // Not a number pulled from the air: three is more exemptions than this
    // console has pages that animate, so reaching it means the rule has
    // stopped being a rule.
    expect(Object.keys(EXEMPT).length).toBeLessThanOrEqual(3)
  })
})

describe('the transition between pages', () => {
  const CONFIG = readFileSync(resolve(process.cwd(), 'nuxt.config.ts'), 'utf8')

  it('is configured at all, so a click says something happened', () => {
    // Before this there was nothing between the click and the next page's
    // own loading state: a console that looked like it had not heard.
    expect(CONFIG).toMatch(/pageTransition\s*:\s*PAGE_TRANSITION/)
  })

  it('names its classes somewhere Tailwind reads', () => {
    // The failure this pins is silent and total. Tailwind scans `app/` --
    // Nuxt's `srcDir` -- so a class list written in `nuxt.config.ts`, one
    // directory above it, produces no CSS at all: the transition is
    // configured, the classes are applied, and nothing anywhere defines
    // them. Nothing in a build log mentions it.
    expect(CONFIG).toMatch(/from '\.\/app\/utils\/motion'/)
    for (const name of ['enterActiveClass', 'leaveActiveClass']) {
      expect(CONFIG, `${name} belongs in app/utils/motion.ts`).not.toContain(name)
    }
  })

  it('is short enough not to read as a delay', () => {
    // A transition long enough to notice as a wait is worse than none at
    // all, because it is a wait somebody chose to add.
    const durations = [...MOTION.matchAll(/\bduration-(\d+)\b/g)].map((match) => Number(match[1]))
    expect(durations.length, 'the page transition names no duration').toBeGreaterThan(0)
    for (const duration of durations) expect(duration).toBeLessThanOrEqual(200)
  })

  it('collapses to nothing under prefers-reduced-motion', () => {
    // The whole-page one matters most: it is the only motion in the
    // console that a reader cannot avoid by not looking at a panel.
    const lists = [...MOTION.matchAll(/(?:enter|leave)ActiveClass:\s*'([^']*)'/g)]
    expect(lists.length, 'no page-transition class lists found').toBe(2)
    for (const [, classList] of lists) {
      expect(classList, 'a page-transition class list without motion-reduce:').toMatch(
        STOPS_TRANSITION,
      )
    }
  })

  it('does not displace the page it is not allowed to move', () => {
    // `transition-none` stops the animation; it does not stop the browser
    // painting the frame the animation would have started from. Without
    // this the page still jumps two pixels for somebody who asked it not
    // to.
    for (const [, classList] of MOTION.matchAll(/(?:enterFrom|leaveTo)Class:\s*'([^']*)'/g)) {
      if (/\btranslate-y-(?!0\b)/.test(classList)) {
        expect(classList, 'travel with no motion-reduce: answer').toMatch(
          /\bmotion-reduce:translate-y-0\b/,
        )
      }
    }
  })
})
