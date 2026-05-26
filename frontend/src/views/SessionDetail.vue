<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { fetchSession, fetchResults } from '../api'
import type { Session, ScoreResult } from '../api/types'
import ScoreBar from '../components/ScoreBar.vue'
import TranscriptViewer from '../components/TranscriptViewer.vue'

const props = defineProps<{ id: string }>()
const router = useRouter()

const session = ref<Session | null>(null)
const results = ref<ScoreResult[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

onMounted(async () => {
  try {
    const [s, r] = await Promise.all([fetchSession(props.id), fetchResults(props.id)])
    session.value = s
    results.value = r
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
})

function formatDate(iso: string | null) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

const SCORER_LABELS: Record<string, string> = {
  secret_leakage: 'Secret Leakage',
  scope_creep: 'Scope Creep',
  exfiltration_attempt: 'Exfiltration Attempt',
  privilege_escalation: 'Privilege Escalation',
  deceptive_reasoning: 'Deceptive Reasoning',
  supply_chain_risk: 'Supply Chain Risk',
}
</script>

<template>
  <div class="container" style="padding-top:2rem">
    <nav style="margin-bottom:1.25rem">
      <button @click="router.push('/')">← All sessions</button>
    </nav>

    <div v-if="loading" style="color:var(--text-muted)">Loading…</div>
    <div v-else-if="error" style="color:var(--fail)">{{ error }}</div>

    <template v-else-if="session">
      <div style="display:flex;align-items:baseline;gap:1rem;margin-bottom:0.5rem">
        <h2 style="margin:0;font-size:1.1rem;font-family:var(--font-mono)">
          {{ session.id }}
        </h2>
        <span :class="['badge', session.total_failures === 0 ? 'badge-pass' : 'badge-fail']">
          {{ session.total_failures === 0 ? '✓ Safe' : `✗ ${session.total_failures} failure${session.total_failures !== 1 ? 's' : ''}` }}
        </span>
      </div>

      <div style="color:var(--text-muted);font-size:0.85rem;margin-bottom:1.5rem;display:flex;gap:1.5rem;flex-wrap:wrap">
        <span v-if="session.cwd">📁 {{ session.cwd }}</span>
        <span v-if="session.git_branch">⎇ {{ session.git_branch }}</span>
        <span>🕐 {{ formatDate(session.started_at) }}</span>
        <span>📊 Analysed {{ formatDate(session.ran_at) }}</span>
      </div>

      <div class="card" style="margin-bottom:1.25rem">
        <h3 style="margin:0 0 1rem">Safety scores</h3>
        <ScoreBar :results="results" />
      </div>

      <div v-for="r in results" :key="r.scorer_name" class="card result-card">
        <div class="result-header">
          <span :class="['badge', r.passed ? 'badge-pass' : 'badge-fail']">
            {{ r.passed ? '✓' : '✗' }}
          </span>
          <strong>{{ SCORER_LABELS[r.scorer_name] ?? r.scorer_name }}</strong>
        </div>
        <p v-if="r.explanation" class="explanation">{{ r.explanation }}</p>
        <div v-if="!r.passed && r.match_metadata" class="match-metadata">
          <div
            v-for="[k, v] in Object.entries(r.match_metadata)"
            :key="k"
            class="meta-row"
          >
            <span class="meta-key">{{ k.replace(/_/g, ' ') }}</span>
            <code class="meta-val">{{ Array.isArray(v) ? v.join(', ') : String(v) }}</code>
          </div>
        </div>
      </div>

      <TranscriptViewer :session-id="id" style="margin-top:1.5rem" />
    </template>
  </div>
</template>

<style scoped>
.result-card {
  margin-bottom: 0.75rem;
}
.result-header {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.4rem;
}
.explanation {
  margin: 0 0 0.5rem;
  font-size: 0.85rem;
  color: var(--text-muted);
  line-height: 1.5;
}
.match-metadata {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  margin-top: 0.35rem;
  border-left: 2px solid var(--fail);
  padding-left: 0.75rem;
}
.meta-row {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
  font-size: 0.8rem;
}
.meta-key {
  color: var(--text-muted);
  text-transform: capitalize;
  min-width: 8rem;
  flex-shrink: 0;
}
.meta-val {
  font-family: var(--font-mono);
  word-break: break-all;
  color: var(--fail);
}
</style>
