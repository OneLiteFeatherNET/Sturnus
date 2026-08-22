<script setup lang="ts">
useHead({ title: 'Dashboard' })
const session = useSession()
</script>

<template>
  <div>
    <h1 class="mb-1 text-2xl font-semibold">Welcome</h1>
    <p class="mb-8 text-sm" :style="{ color: 'var(--text-muted)' }">
      Signed in as {{ session?.discord_user_id }}.
    </p>
    <p :style="{ color: 'var(--text-muted)' }">Statistics arrive with the next change.</p>
  </div>
</template>
