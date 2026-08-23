<script setup lang="ts">
/**
 * The top bar: the product, and the control that collapses the sidebar.
 *
 * The burger is a `button` with `aria-expanded`, not a styled `div`. It
 * changes what is on screen, so it has to be reachable by keyboard and to
 * announce its state -- a toggle that looks like a toggle and announces
 * nothing is a control half the people using it cannot see.
 */
const { collapsed, toggle } = useSidebar()
const session = useSession()
const api = useApi()

async function signOut() {
  await api('/auth/logout', { method: 'POST' })
  // A full navigation rather than a client-side route change: the session
  // cookie is gone, and every piece of state in this tab was rendered for
  // somebody who is now signed out.
  window.location.href = '/'
}
</script>

<template>
  <header
    class="flex items-center gap-3 border-b px-4 py-3"
    :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
  >
    <button
      type="button"
      class="rounded-lg p-2 transition-colors hover:bg-[var(--surface-raised)]"
      :aria-expanded="!collapsed"
      aria-controls="sidebar"
      :aria-label="$t('nav.toggle')"
      @click="toggle"
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
        <path d="M3 6h18v2H3V6Zm0 5h18v2H3v-2Zm0 5h18v2H3v-2Z" />
      </svg>
    </button>

    <NuxtLink to="/" class="flex items-center gap-2">
      <SturnusMark :size="26" />
      <span class="text-base font-semibold tracking-tight">{{ $t('common.brand') }}</span>
    </NuxtLink>

    <div class="ml-auto flex items-center gap-3">
      <button
        v-if="session"
        type="button"
        class="rounded-lg px-3 py-1.5 text-sm transition-colors hover:bg-[var(--surface-raised)]"
        :style="{ color: 'var(--text-muted)' }"
        @click="signOut"
      >
        {{ $t('auth.signOut') }}
      </button>
    </div>
  </header>
</template>
