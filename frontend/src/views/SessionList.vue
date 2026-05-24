<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { fetchSessions } from '../api'
import type { Session } from '../api/types'

const router = useRouter()
const sessions = ref<Session[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

onMounted(async () => {
  try {
    sessions.value = await fetchSessions()
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
})

function open(id: string) {
  router.push(`/sessions/${id}`)
}

function formatDate(iso: string | null) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}
</script>

<template>
  <div class="container" style="padding-top:2rem">
    <h1 style="margin-bottom:0.25rem">Coding Agent Safety Monitor</h1>
    <p style="color:var(--text-muted);margin-top:0;margin-bottom:1.5rem">
      {{ sessions.length }} session{{ sessions.length !== 1 ? 's' : '' }} analysed
    </p>

    <div v-if="loading" style="color:var(--text-muted)">Loading…</div>
    <div v-else-if="error" style="color:var(--fail)">{{ error }}</div>

    <div v-else class="session-list">
      <div
        v-for="s in sessions"
        :key="s.id"
        class="card session-row"
        @click="open(s.id)"
      >
        <div class="session-meta">
          <code class="session-id">{{ s.id.slice(0, 8) }}…</code>
          <span class="muted">{{ formatDate(s.ran_at) }}</span>
          <span v-if="s.git_branch" class="branch">⎇ {{ s.git_branch }}</span>
        </div>
        <div v-if="s.cwd" class="muted">{{ s.cwd }}</div>
        <div class="session-status">
          <span :class="['badge', s.total_failures === 0 ? 'badge-pass' : 'badge-fail']">
            {{ s.total_failures === 0 ? '✓ Safe' : `✗ ${s.total_failures} failure${s.total_failures !== 1 ? 's' : ''}` }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.session-list { display: flex; flex-direction: column; gap: 0.75rem; }
.session-row {
  cursor: pointer;
  transition: border-color 0.15s;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.session-row:hover { border-color: var(--accent); }
.session-meta { display: flex; align-items: center; gap: 1rem; }
.session-id { font-size: 0.85rem; color: var(--text-muted); }
.muted { color: var(--text-muted); font-size: 0.85rem; }
.branch { font-size: 0.8rem; color: var(--accent); }
.session-status { margin-top: 0.25rem; }
</style>
