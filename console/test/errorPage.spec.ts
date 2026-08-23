/**
 * What the error page says when the thing that failed is i18n itself.
 *
 * Every other page in the console may assume its locale file loaded,
 * because a page whose assets did not load is a page nobody is looking at:
 * they are looking at this one. `error.vue` is where that assumption stops
 * being safe. Locale messages are fetched lazily, so the outage that brings
 * somebody here is perfectly capable of being the outage that leaves this
 * page with no messages in any language -- and the page whose only job is
 * to explain a failure in plain words would then render
 * `error.unreachableHeading` at somebody who arrived confused.
 *
 * So: readable English, always, whatever happened to the translations.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { mount } from '@vue/test-utils'
import { computed } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ErrorPage from '../app/error.vue'

function load(locale: string) {
  return JSON.parse(readFileSync(resolve(process.cwd(), `i18n/locales/${locale}.json`), 'utf8'))
}

/**
 * Mount the error page with a given translator.
 *
 * `translator` stands in for `useI18n()`: a function for a working one, and
 * `null` for the case where the plugin never installed and `useI18n` throws.
 */
function render(
  error: { statusCode?: number, message?: string },
  translator: ((key: string, named?: Record<string, unknown>) => string) | null,
) {
  vi.stubGlobal('computed', computed)
  vi.stubGlobal('useHead', () => {})
  vi.stubGlobal('clearError', () => {})
  vi.stubGlobal('useI18n', () => {
    if (!translator) throw new Error('i18n plugin not installed')
    return { t: translator }
  })
  return mount(ErrorPage, {
    props: { error },
    global: {
      stubs: {
        NuxtLink: { template: '<a><slot /></a>' },
        SturnusMark: { template: '<svg />' },
      },
    },
  })
}

/** vue-i18n's own behaviour for a key it has no message for: hand it back. */
const NO_MESSAGES = (key: string) => key

/** A translator holding the real German file. */
function german(): (key: string, named?: Record<string, unknown>) => string {
  const messages = load('de')
  return (key, named) => {
    const [namespace, name] = key.split('.')
    const value = messages[namespace!]?.[name!]
    if (typeof value !== 'string') return key
    return value.replace(/\{(\w+)\}/g, (_, placeholder: string) => String(named?.[placeholder]))
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('the error page when no translations loaded', () => {
  it('explains an unreachable service in English rather than in keys', () => {
    const page = render({ statusCode: 500, message: 'GET /me failed with status 0' }, NO_MESSAGES)
    expect(page.text()).toContain('Sturnus is not answering')
    expect(page.text()).toContain('This is not a sign-in problem')
    expect(page.text()).not.toContain('error.')
  })

  it('explains a missing page in English rather than in keys', () => {
    const page = render({ statusCode: 404, message: 'Page not found' }, NO_MESSAGES)
    expect(page.text()).toContain('That page does not exist')
    expect(page.text()).not.toContain('error.')
  })

  it('still labels both of the things it offers', () => {
    // A button rendering `error.retry` is worse than a button rendering
    // nothing: it looks like a label, so nobody reports it.
    const page = render({ statusCode: 500, message: 'boom' }, NO_MESSAGES)
    expect(page.text()).toContain('Try again')
    expect(page.text()).toContain('Go to sign-in')
    expect(page.text()).toContain('Status 500')
  })

  it('survives i18n not being there at all', () => {
    // `useI18n` throws rather than returning null when the plugin failed to
    // install -- which is one of the disasters this page renders for.
    const page = render({ statusCode: 500, message: 'boom' }, null)
    expect(page.text()).toContain('Something went wrong')
    expect(page.text()).toContain('Try again')
  })
})

describe('the error page when translations did load', () => {
  it('uses them', () => {
    // The English in the source is a floor, not a ceiling. A page that
    // ignored a loaded German message would be the one page of the console
    // permanently in English.
    const page = render({ statusCode: 404, message: 'Page not found' }, german())
    expect(page.text()).toContain('Diese Seite gibt es nicht')
    expect(page.text()).toContain('Erneut versuchen')
    expect(page.text()).toContain('Zur Anmeldung')
  })

  it('keeps the status number in the translated sentence', () => {
    const page = render({ statusCode: 503, message: 'boom' }, german())
    expect(page.text()).toContain('Status 503')
  })
})
