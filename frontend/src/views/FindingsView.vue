<script setup lang="ts">
import { onMounted, ref } from 'vue'
import FindingCard from '../components/FindingCard.vue'
import { useWorkbench } from '../stores/workbench'

const wb = useWorkbench()
const sid = ref('')
onMounted(async () => {
  await wb.loadSessions()
  sid.value = wb.findingsFor ?? wb.current?.id ?? wb.sessions[0]?.id ?? ''
  if (sid.value && wb.findingsFor !== sid.value) await wb.loadFindings(sid.value)
})
const when = (t: number) => new Date(t * 1000).toLocaleString()
</script>

<template>
  <div class="grid gap-3">
    <section class="panel">
      <h2 class="h2">Findings</h2>
      <div class="flex flex-wrap items-end gap-2">
        <label class="text-sm"><span class="label block">Session</span>
          <select v-model="sid" class="field" data-test="findings-session">
            <option v-for="s in wb.sessions" :key="s.id" :value="s.id">{{ when(s.started_at) }} · {{ s.kind }} · {{ s.label || s.id }}{{ s.synthetic ? ' · SYNTHETIC' : '' }}</option>
          </select>
        </label>
        <button class="btn-primary" :disabled="!sid" @click="wb.loadFindings(sid)">Analyse</button>
      </div>
      <p class="mt-2 text-sm text-ink-2">
        Each finding is a hypothesis with the evidence for and against it. Evidence strength is a category (insufficient, suggestive,
        strong), not a probability. A generic trouble code names a monitored system, not a failed part. Suggested next steps are
        measurements and inspections, not parts to buy.
      </p>
    </section>
    <FindingCard v-for="f in wb.findings" :key="f.id" :finding="f" />
    <p v-if="!wb.findings.length" class="text-sm text-ink-2">No findings loaded. Choose a session and press Analyse.</p>
  </div>
</template>
