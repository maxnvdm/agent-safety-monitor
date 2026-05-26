<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { fetchSessions } from '../api'
import type { Session, SessionFilters } from '../api/types'

const router = useRouter()
const sessions = ref<Session[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

const SCORER_OPTIONS = [
  { value: '', label: 'Any scorer' },
  { value: 'secret_leakage', label: 'Secret Leakage' },
  { value: 'scope_creep', label: 'Scope Creep' },
  { value: 'exfiltration_attempt', label: 'Exfiltration Attempt' },
  { value: 'privilege_escalation', label: 'Privilege Escalation' },
  { value: 'deceptive_reasoning', label: 'Deceptive Reasoning' },
  { value: 'supply_chain_risk', label: 'Supply Chain Risk' },
]

const failedOnly = ref(false)
const scorerFilter = ref('')
const branchFilter = ref('')

async function load() {
  loading.value = true
  error.value = null
  try {
    const filters: SessionFilters = {}
    if (failedOnly.value) filters.failed_only = true
    if (scorerFilter.value) filters.scorer = scorerFilter.value
    if (branchFilter.value.trim()) filters.branch = branchFilter.value.trim()
    sessions.value = await fetchSessions(filters)
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch([failedOnly, scorerFilter, branchFilter], load)

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
      {{ sessions.length }} session{{ sessions.length !== 1 ? 's' : '' }} shown
    </p>

    <div class="filter-bar card" style="margin-bottom:1.25rem">
      <label class="filter-item">
        <input type="checkbox" v-model="failedOnly" />
        Failures only
      </label>
      <label class="filter-item">
        <span class="filter-label">Scorer</span>
        <select v-model="scorerFilter">
          <option v-for="o in SCORER_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span class="filter-label">Branch</span>
        <input
          type="text"
          v-model="branchFilter"
          placeholder="e.g. main"
          style="width:10rem"
        />
      </label>
    </div>

    <div v-if="loading" style="color:var(--text-muted)">Loading…</div>
    <div v-else-if="error" style="color:var(--fail)">{{ error }}</div>
    <div v-else-if="sessions.length === 0" style="color:var(--text-muted)">
      No sessions match the current filters.
    </div>

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
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
  align-items: center;
  padding: 0.75rem 1rem;
}
.filter-item {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.85rem;
  cursor: pointer;
}
.filter-label {
  color: var(--text-muted);
}
select, input[type="text"] {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: inherit;
  font-size: 0.85rem;
  padding: 0.2rem 0.4rem;
}
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
