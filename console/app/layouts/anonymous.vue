<script setup lang="ts">
/**
 * The layout for the one page that exists before a session does.
 *
 * No navigation, deliberately: there is nothing to navigate to until
 * somebody is signed in, and offering a sidebar whose every destination
 * answers 401 would be an interface that lies about what it can do.
 */
</script>

<template>
  <div class="flex min-h-screen items-center justify-center p-6">
    <slot />
  </div>
</template>
