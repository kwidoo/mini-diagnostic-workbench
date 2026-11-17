<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useWorkbench } from './stores/workbench'
import ConnectionView from './views/ConnectionView.vue'
import FaultsView from './views/FaultsView.vue'
import FindingsView from './views/FindingsView.vue'
import LiveView from './views/LiveView.vue'
import SessionsView from './views/SessionsView.vue'
import VehicleView from './views/VehicleView.vue'

const wb = useWorkbench()
const tabs = [
  { id: 'connection', name: 'Connection', view: ConnectionView },
  { id: 'vehicle', name: 'Vehicle', view: VehicleView },
  { id: 'faults', name: 'Faults', view: FaultsView },
  { id: 'live', name: 'Live', view: LiveView },
  { id: 'sessions', name: 'Sessions & compare', view: SessionsView },
  { id: 'findings', name: 'Findings', view: FindingsView },
]
const tab = ref('connection')
onMounted(async () => {
  await wb.init()
  wb.openSocket()
})
</script>

<template>
  <div class="mx-auto max-w-6xl p-4">
    <header class="mb-3 flex flex-wrap items-center gap-3">
      <h1 class="text-lg font-semibold">Diagnostic Workbench</h1>
      <label class="flex items-center gap-2 text-sm">
        <span class="label">Vehicle</span>
        <select
          class="field" data-test="profile-select" :value="wb.activeProfileId" :disabled="!!wb.profileLockReason"
          :title="wb.profileLockReason ?? ''" @change="wb.setProfile(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="p in wb.profiles" :key="p.id" :value="p.id">{{ p.name }}{{ p.capabilities?.generic_obd === 'ready' ? '' : ' (in progress)' }}</option>
        </select>
      </label>
      <span class="badge" data-test="read-only" title="This tool cannot clear faults, reset adaptations, actuate components or trigger a DPF regeneration.">
        READ-ONLY
      </span>
      <span class="badge" data-test="conn-badge" :class="wb.connected ? 'text-good' : wb.connectionState === 'reconnecting' ? 'text-warn' : 'text-ink-2'">
        {{ wb.connectionState }}
      </span>
      <span v-if="wb.status?.synthetic" class="badge text-warn" data-test="synthetic-badge">SYNTHETIC simulator data</span>
      <span v-if="wb.recording" class="badge text-crit">recording</span>
      <span v-if="wb.busy" class="text-xs text-ink-2">{{ wb.busy }}…</span>
    </header>

    <aside class="panel mb-3 text-sm" data-test="safety">
      <b>Safety.</b> Test with the vehicle stationary, parked safely, outdoors or well ventilated. Road recording only with a passenger
      operating this software. Stop and seek professional inspection if the engine runs severely irregularly, you hear metallic knocking,
      it overheats, you smell exhaust or coolant, or the MIL flashes. This tool reads generic OBD data only; manufacturer-specific data is
      not available through it, and it does not certify roadworthiness.
    </aside>

    <p v-if="wb.error" class="panel mb-3 border-crit text-sm text-crit" role="alert" data-test="error">
      {{ wb.error }} <button class="btn ml-2" @click="wb.error = null">Dismiss</button>
    </p>

    <nav class="mb-3 flex flex-wrap gap-1 border-b border-line">
      <button
        v-for="t in tabs" :key="t.id" :data-test="`tab-${t.id}`"
        class="-mb-px border-b-2 px-3 py-2 text-sm" :class="tab === t.id ? 'border-ink font-semibold' : 'border-transparent text-ink-2'"
        @click="tab = t.id"
      >{{ t.name }}</button>
    </nav>

    <component :is="tabs.find((t) => t.id === tab)!.view" />
  </div>
</template>
