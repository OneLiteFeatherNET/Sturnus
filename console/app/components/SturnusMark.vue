<script setup lang="ts">
/**
 * The Sturnus mark: a starling in profile, drawn as one closed silhouette.
 *
 * The gradient is the point, and it is not decoration. OneLiteFeather's own
 * mark is a feather in a full spectrum, and a starling's plumage is
 * iridescent -- black that throws green, violet and blue depending on the
 * light. The house palette and the species are the same idea, so one
 * gradient does both jobs. Colours are taken from the organisation mark
 * rather than invented; see `docs/brand/README.md`.
 *
 * Two sound arcs, never three: three merge into a smudge at the sizes this
 * is actually rendered at.
 */
withDefaults(defineProps<{ size?: number }>(), { size: 28 })
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 64 64"
    role="img"
    aria-label="Sturnus"
    class="shrink-0"
  >
    <defs>
      <linearGradient id="sturnus-plumage" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="var(--color-brand-green)" />
        <stop offset="28%" stop-color="var(--color-brand-cyan)" />
        <stop offset="55%" stop-color="var(--color-brand-blue)" />
        <stop offset="80%" stop-color="var(--color-brand-magenta)" />
        <stop offset="100%" stop-color="var(--color-brand-red)" />
      </linearGradient>
    </defs>
    <circle cx="32" cy="32" r="30" fill="var(--color-brand-field)" />
    <path
      d="M20 40c-3-7-1-16 6-20 6-4 14-3 18 2l8-3-5 7c2 6 1 13-4 17-6 5-16 4-23-3z"
      fill="url(#sturnus-plumage)"
    />
    <path d="M44 22l10-3-7 6z" fill="var(--color-brand-yellow)" />
    <circle cx="28" cy="26" r="1.6" fill="var(--color-brand-field)" />
    <circle cx="24" cy="34" r="1.1" fill="#ffffff" opacity="0.75" />
    <circle cx="31" cy="39" r="1.1" fill="#ffffff" opacity="0.75" />
    <circle cx="37" cy="33" r="1.1" fill="#ffffff" opacity="0.75" />
    <path
      d="M52 34a9 9 0 0 1-3 6"
      stroke="var(--color-brand-cyan)"
      stroke-width="2.5"
      stroke-linecap="round"
      fill="none"
    />
    <path
      d="M57 32a15 15 0 0 1-5 10"
      stroke="var(--color-brand-cyan)"
      stroke-width="2.5"
      stroke-linecap="round"
      fill="none"
      opacity="0.6"
    />
  </svg>
</template>
