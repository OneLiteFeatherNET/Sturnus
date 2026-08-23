import tailwindcss from '@tailwindcss/vite'

import { PAGE_TRANSITION } from './app/utils/motion'

// The console is served from the same origin as the API it calls
// (`sturnus.onelitefeather.dev`, with `/api/*` routed to the API process),
// which is what lets the session cookie be `SameSite=Lax` and `HttpOnly`:
// the browser attaches it to same-origin requests without any JavaScript
// ever being able to read it. A separate origin for the console would have
// forced either a cross-site cookie or a token in local storage, and both
// are worse.
export default defineNuxtConfig({
  compatibilityDate: '2026-08-21',

  // SSR on. The console's first paint needs a session decision -- signed in
  // or not -- and making that on the server avoids the flash of a login
  // screen for somebody who is already signed in.
  ssr: true,

  devtools: { enabled: false },

  modules: ['@nuxt/eslint', '@nuxtjs/i18n'],

  i18n: {
    // **No locale in the URL.** The console is one application behind one
    // session cookie, not a public site that a search engine needs to index
    // twice. `/de/recordings/...` would buy nothing anybody asked for and
    // would cost every link in the app and every bookmark anybody has: the
    // address of a recording would depend on the language its reader
    // happened to be using, so a link pasted into a chat would carry the
    // sender's language to whoever opened it.
    strategy: 'no_prefix',

    // English is the source language, and the one every key is written in
    // first. German is a translation of it -- see `i18n/README.md`.
    defaultLocale: 'en',
    locales: [
      { code: 'en', language: 'en-GB', name: 'English', file: 'en.json' },
      { code: 'de', language: 'de-DE', name: 'Deutsch', file: 'de.json' },
    ],

    detectBrowserLanguage: {
      // A stored choice wins; failing that the browser's `Accept-Language`;
      // failing that English.
      //
      // **The choice is a cookie, not `localStorage`.** This console renders
      // on the server, and the server is where the first paint is decided --
      // a cookie travels with the request, so the very first HTML is already
      // in the right language. A choice kept in `localStorage` is readable
      // only after hydration, which means every first paint of every visit
      // would render English and then swap to German in front of the reader.
      useCookie: true,
      cookieKey: 'sturnus_locale',
      // Honour the stored choice on every request rather than only the
      // first: with `no_prefix` there is no URL to infer a language from,
      // so the cookie is the only thing that remembers.
      alwaysRedirect: true,
      redirectOn: 'all',
      fallbackLocale: 'en',
    },
  },

  css: ['~/assets/css/main.css'],

  vite: {
    plugins: [tailwindcss()],
  },

  runtimeConfig: {
    // Where the server-side render reaches the API. Inside the cluster that
    // is the Service, not the public hostname: routing a server-side call
    // back out through Cloudflare and in again would double the latency and
    // fail entirely if the tunnel is the thing that is down.
    apiInternalBase: process.env.NUXT_API_INTERNAL_BASE || 'http://sturnus-api:8080',
    public: {
      // What the browser calls. Same origin, so a relative path -- an
      // absolute URL here would be one more thing to get wrong per
      // environment.
      apiBase: '/api',
    },
  },

  app: {
    head: {
      titleTemplate: '%s · Sturnus',
      link: [{ rel: 'icon', type: 'image/png', href: '/favicon.png' }],
    },

    // What a click looks like before the next page has anything to say.
    // Until now: nothing at all. The classes, and the reasoning about how
    // long a navigation is allowed to take to acknowledge itself, are in
    // `app/utils/motion.ts` -- they have to be under `app/` for Tailwind to
    // emit them, and that file says why.
    pageTransition: PAGE_TRANSITION,
  },

  // **`/settings` is a page again, and its redirect is gone.** It used to be
  // a permanent redirect to `/admin/bot-settings`, kept alive because the
  // bot's configuration once lived there and the address was in browser
  // histories. It is now the person's own settings -- their theme and their
  // language -- which is what anybody typing that address is looking for.
  //
  // This is a breaking change, and a 301 is a redirect a browser caches
  // indefinitely: somebody who followed the old one still lands on
  // `/admin/bot-settings` until they clear it. The Admin View entry in the
  // sidebar is the way there, and it has never moved.

  nitro: {
    // The container runs this as a plain Node server behind a Service.
    preset: 'node-server',
  },
})
