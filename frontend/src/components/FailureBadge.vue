<script setup lang="ts">
defineProps<{ passed: boolean; label: string }>()
</script>

<template>
  <span :class="['badge', passed ? 'badge-pass' : 'badge-fail']">
    {{ passed ? '✓' : '✗' }} {{ label }}
  </span>
</template>
