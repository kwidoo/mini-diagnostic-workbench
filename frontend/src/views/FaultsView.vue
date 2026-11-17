<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import ScanCard from '../components/ScanCard.vue'
import { useWorkbench } from '../stores/workbench'
import type { SessionDetail } from '../types'

const wb = useWorkbench()
const readVin = ref(false)
const history = ref<SessionDetail[]>([])
const when = (t: number) => new Date(t * 1000).toLocaleString()

async function loadHistory() {
  const out: SessionDetail[] = []
  for (const s of wb.sessions.slice(0, 12)) {
    try {
      const d = await api.get<SessionDetail>(`/api/sessions/${s.id}`)
      if (d.scans.length) out.push(d)
    } catch {
      /* session list is refreshed elsewhere */
    }
  }
  history.value = out
}
async function scan() {
  await wb.scan(readVin.value)
  await loadHistory()
}
onMounted(loadHistory)
const inv = computed(() => wb.inventory)
</script>

<template>
  <div class="grid gap-3">
    <section class="panel">
      <h2 class="h2">Fault scan</h2>
      <p class="text-sm text-ink-2">Reads stored, pending and permanent generic OBD codes and the freeze frame. Nothing is cleared or reset.</p>
      <div class="mt-2 flex flex-wrap items-center gap-3">
        <button class="btn-primary" data-test="scan" :disabled="!wb.connected || !!wb.busy || !!wb.recording" @click="scan">Scan faults</button>
        <button class="btn" data-test="inventory" :disabled="!wb.connected || !!wb.busy || !!wb.recording" @click="wb.runInventory()">Read PID inventory</button>
        <label class="text-sm"><input v-model="readVin" type="checkbox" data-test="read-vin" /> Also read the VIN (stays on this Mac; redacted in exports unless you opt in)</label>
        <span v-if="!wb.connected" class="text-sm text-ink-2">Connect to an adapter first.</span>
      </div>
    </section>

    <section v-if="inv" class="panel text-sm" data-test="inventory-result">
      <h2 class="h2">PID inventory</h2>
      <table class="w-full text-left">
        <thead><tr class="label"><th>PID</th><th>ECU</th><th>Signal</th><th>Value</th><th>Raw</th></tr></thead>
        <tbody>
          <tr v-for="(r, i) in inv.readings" :key="i">
            <td class="font-mono">{{ r.pid.toString(16).toUpperCase().padStart(2, '0') }}</td><td>{{ r.ecu ?? '-' }}</td><td>{{ r.name }}</td>
            <td>
              <template v-if="r.text">{{ r.text }}</template>
              <template v-else-if="r.quality === 'ok' && r.value !== null">{{ r.value }} {{ r.unit }}</template>
              <span v-else class="text-ink-2">{{ r.quality === 'raw_only' ? 'supported, raw only (no verified decoder)' : `not measured (${r.quality})` }}</span>
            </td>
            <td class="font-mono text-ink-2">{{ r.raw_hex }}</td>
          </tr>
        </tbody>
      </table>
      <h3 class="label mt-3">Priority signals this ECU does not support via generic OBD</h3>
      <ul class="list-disc pl-5" data-test="unsupported-list">
        <li v-for="p in inv.unsupported_priority" :key="p.pid">{{ p.name }} ({{ p.hex }}): not supported</li>
        <li v-if="!inv.unsupported_priority.length" class="text-ink-2">All priority signals are supported.</li>
      </ul>
    </section>

    <section v-for="s in history" :key="s.id" class="grid gap-2" data-test="scan-group">
      <h3 class="text-sm font-semibold">
        Session {{ s.id }} · {{ when(s.started_at) }} · {{ s.kind }}
        <span v-if="s.synthetic" class="badge text-warn">SYNTHETIC</span>
      </h3>
      <ScanCard v-for="(sc, i) in s.scans" :key="i" :scan="sc" />
    </section>
    <p v-if="!history.length" class="text-sm text-ink-2">No fault scans stored for this vehicle yet.</p>
  </div>
</template>
