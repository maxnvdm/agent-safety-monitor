<script setup lang="ts">
import { ref, watch } from 'vue'
import { fetchTranscript } from '../api'

const props = defineProps<{ sessionId: string }>()

const transcript = ref<string | null>(null)
const loading = ref(false)
const open = ref(false)

async function load() {
  if (transcript.value !== null) return
  loading.value = true
  try {
    transcript.value = await fetchTranscript(props.sessionId)
  } finally {
    loading.value = false
  }
}

watch(open, (val) => { if (val) load() })
</script>

<template>
  <div class="transcript">
    <button @click="open = !open">
      {{ open ? '▲ Hide' : '▼ Show' }} transcript
    </button>
    <template v-if="open">
      <div v-if="loading" class="muted">Loading…</div>
      <pre v-else-if="transcript" class="transcript-body">{{ transcript }}</pre>
    </template>
  </div>
</template>

<style scoped>
.transcript { margin-top: 1rem; }
.transcript-body {
  margin-top: 0.75rem;
  padding: 1rem;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.78rem;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 500px;
  overflow-y: auto;
}
.muted { color: var(--text-muted); font-size: 0.875rem; margin-top: 0.5rem; }
</style>
