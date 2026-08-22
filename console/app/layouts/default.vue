<script setup lang="ts">
const { restore } = useSidebar()
// `localStorage` only exists in the browser, so the preference is applied
// after hydration rather than during the server render. The alternative --
// rendering nothing until it is known -- trades a fully correct first paint
// for a blank one, which is the worse of the two.
onMounted(restore)
</script>

<template>
  <div class="flex min-h-screen flex-col">
    <AppHeader />
    <div class="flex flex-1">
      <AppSidebar id="sidebar" />
      <!-- `min-w-0` is load-bearing. Without it a flex item's minimum
           width is its content's, so one `min-w-56` inside the recordings
           list made the *document* wider than the viewport and put a
           horizontal scrollbar under the header on every phone. With it,
           the content can shrink and the things that must not — a
           scrubber, a duration — scroll inside their own row instead. -->
      <main class="min-w-0 flex-1 p-4 sm:p-6">
        <slot />
      </main>
    </div>
  </div>
</template>
