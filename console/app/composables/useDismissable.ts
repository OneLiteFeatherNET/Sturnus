/**
 * The one thing every control with a popup needs a document for.
 *
 * Four of the six controls in `components/ui` open something over the
 * page — a listbox, a filtered listbox, a calendar, a list of tag
 * suggestions — and all four owe the reader the same two courtesies:
 * pressing anywhere else puts it away, and putting it away leaves focus
 * somewhere sensible rather than on an element that has just been removed
 * from the document.
 *
 * That is deliberately **all** this composable does. It was tempting to
 * make it own the open state as well, and then the keyboard, and then the
 * markup — at which point it would be a control framework, and the seventh
 * control would spend its life arguing with it. The open state lives in
 * each control's own reducer, where it is testable without a DOM; this
 * holds two element references and a pair of listeners.
 *
 * `ProfileMenu` has carried its own copy of this since before there was a
 * second popup in the console. It is deliberately not migrated here —
 * moving working code is a separate change from adding new code, and
 * mixing the two makes both unreviewable.
 */
import type { Ref } from 'vue'

export interface Dismissable {
  /** Wrap the trigger *and* the popup. Anything inside is "not outside". */
  root: Ref<HTMLElement | null>
  /** The element focus goes back to. */
  trigger: Ref<HTMLElement | null>
  returnFocus: () => void
}

export function useDismissable(open: Ref<boolean>, dismiss: () => void): Dismissable {
  const root = ref<HTMLElement | null>(null)
  const trigger = ref<HTMLElement | null>(null)

  function onOutside(event: Event) {
    if (!open.value) return
    if (root.value?.contains(event.target as Node)) return
    dismiss()
  }

  /**
   * `pointerdown` rather than `click`, and in the capture phase.
   *
   * A click fires after the button under it has already been pressed, so
   * dismissing on `click` means the popup is still open while the page
   * beneath it is handling the press — and a popup that overlaps that
   * button swallows it. Listening on the document rather than laying an
   * invisible full-screen overlay is the same argument one level up: an
   * overlay eats the click that dismissed it, so dismissing a menu and
   * pressing the button underneath takes two presses and nobody ever
   * works out why.
   *
   * `focusin` is the keyboard's half of the same rule. Tabbing out of a
   * popup that stays open leaves an open listbox behind a reader who has
   * moved on, and `aria-expanded` on an unfocused control is a lie a
   * screen reader repeats.
   */
  onMounted(() => {
    document.addEventListener('pointerdown', onOutside, true)
    document.addEventListener('focusin', onOutside, true)
  })

  onBeforeUnmount(() => {
    document.removeEventListener('pointerdown', onOutside, true)
    document.removeEventListener('focusin', onOutside, true)
  })

  function returnFocus() {
    trigger.value?.focus()
  }

  return { root, trigger, returnFocus }
}
