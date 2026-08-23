<script setup lang="ts">
/**
 * Who is signed in, and the four things they can do about it.
 *
 * The entries themselves -- which exist, which work, what the initials of a
 * person are -- live in `~/utils/profileMenu` and are tested there. What is
 * left in this file is the part that genuinely needs a document: focus,
 * dismissal, and the keyboard.
 *
 * **Two of the four rows do nothing, visibly.** Two-factor and multi-factor
 * authentication are not built. They are rendered as inert rows carrying
 * "coming soon" rather than as links to nowhere or buttons that shrug,
 * because an interface that shows a control which silently does nothing
 * teaches people not to trust its controls -- and that lesson, once learned,
 * is applied to every other control on the page. A row that admits it is not
 * ready costs one line and teaches the opposite. The same two rows appear in
 * the Security section of `/settings`, from the same list, so the promise
 * made here has somewhere to land.
 *
 * **The keyboard is not an extra.** The trigger is a `button` that announces
 * `aria-haspopup` and its `aria-expanded` state; the menu is a `menu` whose
 * rows are `menuitem`s; Escape closes it and puts focus back where it came
 * from; the arrows walk the rows; a click anywhere else dismisses it. The
 * header already holds this standard for the sidebar toggle, and a control
 * that opens an overlay has more of an obligation to it, not less: a menu
 * that can be opened by keyboard and not closed by one is a trap.
 *
 * The arrows walk the *disabled* rows too. They are skipped by nothing,
 * because the whole reason they are on screen is to be read, and a keyboard
 * reader who is never taken through them is the one reader the promise never
 * reaches. They are out of the tab order like every other row in a menu --
 * `tabindex="-1"`, focus moved by this component -- and they refuse
 * activation.
 */
import {
  COMING_SOON_KEY,
  PROFILE_MENU_ITEMS,
  hasDisplayName,
  initialsFor,
} from '~/utils/profileMenu'

const session = useSession()
const api = useApi()

const open = ref(false)
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const menu = ref<HTMLElement | null>(null)

const displayName = computed(() => session.value?.display_name ?? null)
const named = computed(() => hasDisplayName(displayName.value))
const initials = computed(() => initialsFor(displayName.value))

/** The rows, as elements, in the order they are rendered. */
function rows(): HTMLElement[] {
  return Array.from(menu.value?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])
}

async function focusRow(index: number) {
  // After a tick, because the menu is `v-if` and on the opening keystroke
  // it does not exist yet.
  await nextTick()
  const all = rows()
  if (all.length === 0) return
  const at = ((index % all.length) + all.length) % all.length
  all[at]?.focus()
}

function openMenu(land: 'first' | 'last' | 'none') {
  open.value = true
  if (land === 'first') void focusRow(0)
  if (land === 'last') void focusRow(-1)
}

/**
 * Closes the menu, and by default hands focus back to the trigger.
 *
 * `returnFocus` is false where focus is already going somewhere better: a
 * row that navigates, and Tab, which is on its way to the next control and
 * would be dragged backwards by a focus call.
 */
function close(returnFocus = true) {
  if (!open.value) return
  open.value = false
  if (returnFocus) trigger.value?.focus()
}

function toggle() {
  if (open.value) close()
  else openMenu('none')
}

function onMenuKeydown(event: KeyboardEvent) {
  const all = rows()
  const at = all.indexOf(document.activeElement as HTMLElement)
  switch (event.key) {
    case 'Escape':
      event.preventDefault()
      close()
      break
    case 'ArrowDown':
      event.preventDefault()
      void focusRow(at + 1)
      break
    case 'ArrowUp':
      event.preventDefault()
      void focusRow(at - 1)
      break
    case 'Home':
      event.preventDefault()
      void focusRow(0)
      break
    case 'End':
      event.preventDefault()
      void focusRow(-1)
      break
    case 'Tab':
      // Not prevented: Tab means "leave", and the menu should be shut by
      // the time whatever is next has focus.
      close(false)
      break
  }
}

/**
 * A click anywhere outside dismisses the menu.
 *
 * On `document` rather than on a full-screen invisible overlay, because an
 * overlay would swallow the click that dismissed it -- so dismissing a menu
 * and pressing the button underneath would take two clicks, and nobody ever
 * finds out why.
 */
function onDocumentClick(event: MouseEvent) {
  if (!open.value) return
  if (root.value?.contains(event.target as Node)) return
  close(false)
}

// No `import.meta.client` guard: `onMounted` does not run on the server.
onMounted(() => document.addEventListener('click', onDocumentClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocumentClick))

async function signOut() {
  close(false)
  await api('/auth/logout', { method: 'POST' })
  // A full navigation rather than a client-side route change: the session
  // cookie is gone, and every piece of state in this tab was rendered for
  // somebody who is now signed out.
  window.location.href = '/'
}
</script>

<template>
  <div v-if="session" ref="root" class="relative">
    <button
      ref="trigger"
      type="button"
      class="flex max-w-[14rem] items-center gap-2 rounded-lg px-2 py-1.5 transition-colors hover:bg-[var(--surface-raised)]"
      aria-haspopup="menu"
      aria-controls="profile-menu"
      :aria-expanded="open"
      :aria-label="named ? $t('profile.menuFor', { name: displayName }) : $t('profile.menu')"
      @click="toggle"
      @keydown.down.prevent="openMenu('first')"
      @keydown.up.prevent="openMenu('last')"
    >
      <!-- Initials, and never an avatar: an avatar would have to come from
           Discord and this API holds no Discord token. Hidden from a screen
           reader because it says nothing the name beside it does not. -->
      <span
        aria-hidden="true"
        class="grid h-8 w-8 shrink-0 place-items-center rounded-full text-xs font-semibold"
        :style="{ background: 'var(--action)', color: 'var(--action-contrast)' }"
      >{{ initials }}</span>
      <span class="truncate text-sm">{{ named ? displayName : $t('profile.unnamed') }}</span>
      <svg
        aria-hidden="true"
        class="shrink-0"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="currentColor"
      >
        <path d="M12 15.5 5.5 9l1.4-1.4 5.1 5.1 5.1-5.1L18.5 9 12 15.5Z" />
      </svg>
    </button>

    <div
      v-if="open"
      id="profile-menu"
      ref="menu"
      role="menu"
      :aria-label="$t('profile.menu')"
      class="absolute right-0 z-20 mt-2 w-64 max-w-[calc(100vw-2rem)] rounded-xl border p-1 shadow-lg"
      :style="{ borderColor: 'var(--border)', background: 'var(--surface)' }"
      @keydown="onMenuKeydown"
    >
      <template v-for="item in PROFILE_MENU_ITEMS" :key="item.id">
        <NuxtLink
          v-if="item.kind === 'link'"
          :to="item.to"
          role="menuitem"
          tabindex="-1"
          class="block rounded-lg px-3 py-2 text-sm transition-colors hover:bg-[var(--surface-raised)] focus:bg-[var(--surface-raised)]"
          @click="close(false)"
        >
          {{ $t(item.labelKey) }}
        </NuxtLink>

        <button
          v-else-if="item.kind === 'action'"
          type="button"
          role="menuitem"
          tabindex="-1"
          class="block w-full rounded-lg px-3 py-2 text-left text-sm transition-colors hover:bg-[var(--surface-raised)] focus:bg-[var(--surface-raised)]"
          @click="signOut"
        >
          {{ $t(item.labelKey) }}
        </button>

        <!-- A row, deliberately: not a link, not a button, nothing that can
             be activated into a disappointment. -->
        <div
          v-else
          role="menuitem"
          aria-disabled="true"
          tabindex="-1"
          class="flex items-center justify-between gap-2 rounded-lg px-3 py-2 text-sm"
          :style="{ color: 'var(--text-muted)' }"
        >
          <span class="truncate">{{ $t(item.labelKey) }}</span>
          <span
            class="shrink-0 rounded-full border px-2 py-0.5 text-[0.65rem] uppercase tracking-wide"
            :style="{ borderColor: 'var(--control-border)' }"
          >{{ $t(item.noteKey ?? COMING_SOON_KEY) }}</span>
        </div>
      </template>
    </div>
  </div>
</template>
