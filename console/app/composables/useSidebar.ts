import { readCollapsed, writeCollapsed } from '~/utils/preferences'

/**
 * Whether the sidebar shows labels or only icons.
 *
 * `useState` rather than a module-level ref: on the server one process
 * renders for many people at once, and a module-level value would be
 * shared between them.
 *
 * The storage handling lives in `~/utils/preferences` so it can be tested
 * without a Nuxt application -- see that module on why every accessor is
 * guarded.
 */
export function useSidebar() {
  const collapsed = useState<boolean>('sidebar-collapsed', () => false)

  function toggle() {
    collapsed.value = !collapsed.value
    if (import.meta.client) {
      writeCollapsed(window.localStorage, collapsed.value)
    }
  }

  function restore() {
    if (!import.meta.client) return
    collapsed.value = readCollapsed(window.localStorage)
  }

  return { collapsed, toggle, restore }
}
