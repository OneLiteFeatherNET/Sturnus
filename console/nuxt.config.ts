import tailwindcss from '@tailwindcss/vite'

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

  modules: ['@nuxt/eslint'],

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
  },

  routeRules: {
    // The bot's configuration used to live at `/settings`, before there
    // was an Admin View for it to sit inside. The old address is kept as a
    // permanent redirect rather than deleted: it is in browser histories
    // and in whatever anybody pasted into a chat, and a 404 there teaches
    // people the console loses pages.
    '/settings': { redirect: { to: '/admin/bot-settings', statusCode: 301 } },
  },

  nitro: {
    // The container runs this as a plain Node server behind a Service.
    preset: 'node-server',
  },
})
