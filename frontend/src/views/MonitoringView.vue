<script setup lang="ts">
import { ref } from 'vue'
import MonitoringHistory from '../components/monitoring/MonitoringHistory.vue'
import MonitoringLive from '../components/monitoring/MonitoringLive.vue'
import MonitoringSession from '../components/monitoring/MonitoringSession.vue'
import { useWorkbench } from '../stores/workbench'

const wb = useWorkbench()
const sections = [
  { id: 'live', name: 'Live', view: MonitoringLive },
  { id: 'session', name: 'Session details', view: MonitoringSession },
  { id: 'history', name: 'Compare drives', view: MonitoringHistory },
]
const section = ref('live')
</script>

<template>
  <div class="grid gap-3" data-test="monitoring-view">
    <p v-if="!wb.profile?.monitoring_events?.length" class="panel text-sm text-ink-2" data-test="monitoring-not-for-profile">
      Continuous monitoring is available for the MINI profile only.
    </p>
    <template v-else>
      <nav class="flex gap-1">
        <button
          v-for="s in sections" :key="s.id" class="btn" :class="section === s.id ? 'border-ink font-semibold' : ''" :data-test="`msec-${s.id}`"
          @click="section = s.id"
        >{{ s.name }}</button>
      </nav>
      <component :is="sections.find((s) => s.id === section)!.view" />
    </template>
  </div>
</template>
