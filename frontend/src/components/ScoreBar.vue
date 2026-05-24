<script setup lang="ts">
import FailureBadge from './FailureBadge.vue'
import type { ScoreResult } from '../api/types'

defineProps<{ results: ScoreResult[] }>()

const SCORER_LABELS: Record<string, string> = {
  secret_leakage: 'Secret Leakage',
  scope_creep: 'Scope Creep',
  exfiltration_attempt: 'Exfiltration',
  privilege_escalation: 'Priv Escalation',
  deceptive_reasoning: 'Deceptive Reasoning',
  supply_chain_risk: 'Supply Chain',
}
</script>

<template>
  <div class="score-bar">
    <FailureBadge
      v-for="r in results"
      :key="r.scorer_name"
      :passed="r.passed"
      :label="SCORER_LABELS[r.scorer_name] ?? r.scorer_name"
    />
  </div>
</template>

<style scoped>
.score-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
</style>
