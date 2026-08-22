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
      <main class="flex-1 p-6">
        <slot />
      </main>
    </div>
  </div>
</template>
