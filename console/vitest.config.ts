import { defineConfig, type Plugin } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

/**
 * Nuxt's `import.meta.client` and `import.meta.server`, for a run without Nuxt.
 *
 * Nuxt substitutes both at build time; vitest compiles the same files
 * without it, and an undefined `import.meta.client` makes every
 * browser-only branch read as dead code. A page whose watching happens
 * exclusively in the browser would then be untestable in the worst way:
 * its tests would *pass*, by never reaching the lines they are about.
 *
 * Vite's own `define` cannot do this — it does not substitute
 * `import.meta.*` in the serve pipeline vitest runs on — so the
 * substitution is done here, before `@vitejs/plugin-vue` splits an SFC
 * into blocks and while the file is still the text its author wrote.
 *
 * `client` is `true` because a test runs in a browser environment
 * (`happy-dom`). That is the truthful answer rather than a convenient one:
 * a server render is a distinct thing this suite does not perform.
 */
function nuxtImportMeta(): Plugin {
  return {
    name: 'sturnus:nuxt-import-meta',
    // Before the Vue plugin, so an SFC is still one file and one pass
    // covers its script, its template expressions and everything else.
    enforce: 'pre',
    transform(code, id) {
      if (!/\.(vue|ts)(\?|$)/.test(id)) return null
      if (!code.includes('import.meta.client') && !code.includes('import.meta.server')) return null
      return code
        .replaceAll('import.meta.client', 'true')
        .replaceAll('import.meta.server', 'false')
    },
  }
}

export default defineConfig({
  plugins: [nuxtImportMeta(), vue()],
  test: {
    environment: 'happy-dom',
    globals: true,
    include: ['test/**/*.spec.ts'],
  },
  resolve: {
    alias: {
      '~': fileURLToPath(new URL('./app', import.meta.url)),
      '@': fileURLToPath(new URL('./app', import.meta.url)),
    },
  },
})
