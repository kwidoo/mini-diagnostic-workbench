<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useWorkbench } from '../stores/workbench'
import type { CapField } from '../types'

const wb = useWorkbench()
const readVin = ref(false)
const variant = ref('')
onMounted(async () => {
  await Promise.all([wb.loadDdeCapability(), wb.loadDdeStatus()])
  variant.value = wb.ddeStatus?.settings?.confirmed_variant ?? ''
})
const dde = computed(() => wb.ddeStatus)
function toggleOptIn(e: Event) {
  return wb.saveDdeSettings({ opt_in: (e.target as HTMLInputElement).checked })
}
function saveVariant() {
  const v = variant.value.trim()
  return wb.saveDdeSettings(v ? { confirmed_variant: v } : { clear_variant: true })
}
const rep = computed(() => wb.ddeCapability)
const canRun = computed(() => wb.connected && !wb.status?.operation && !wb.busy)

function show(f: CapField): string {
  const v = f.value
  if (v === null || v === undefined) return '-'
  if (Array.isArray(v)) {
    return v
      .map((a: { ecu: string; text?: string | null; raw_hex?: string; length?: number }) =>
        `${a.ecu}: ${a.text ?? a.raw_hex ?? `VIN (${a.length} chars, not shown)`}`)
      .join('; ')
  }
  return String(v)
}
const rows = computed(() => {
  const r = rep.value
  if (!r) return []
  return [
    ...(['identity', 'device_description', 'claimed_elm_version', 'supply_voltage', 'chip'] as const).map((k) => [`adapter ${k.replaceAll('_', ' ')}`, r.adapter[k]] as const),
    ...(['number', 'name', 'can_11bit_500k'] as const).map((k) => [`protocol ${k.replaceAll('_', ' ')}`, r.protocol[k]] as const),
    ...Object.entries(r.identification).map(([k, f]) => [`service 09 ${k.replaceAll('_', ' ')}`, f] as const),
    ['ISO-TP multi-frame', r.isotp_multiframe] as const,
    ...(['variant', 'software_version', 'manufacturer_requests_sent'] as const).map((k) => [`DDE ${k.replaceAll('_', ' ')}`, r.dde[k]] as const),
  ]
})
const verdictClass = (v: string) =>
  v.endsWith('candidate') ? 'text-good' : v.endsWith('blocked') ? 'text-crit' : 'text-warn'
</script>

<template>
  <section class="panel text-sm" data-test="dde-capability">
    <h2 class="h2">DDE capability (generic OBD + adapter identity only)</h2>
    <p class="text-ink-2">
      Reads what the existing connection is already allowed to read (adapter identity, protocol, service 09 calibration ID / CVN /
      ECU name). It sends <b>no BMW-specific request</b> and does not prove that DDE access works.
    </p>
    <div class="mt-2 flex flex-wrap items-center gap-2">
      <button class="btn" data-test="run-dde-capability" :disabled="!canRun" @click="wb.runDdeCapability(readVin)">Run capability check</button>
      <label class="flex items-center gap-1"><input v-model="readVin" type="checkbox" data-test="dde-read-vin" /> also read VIN (never shown, redacted in exports)</label>
      <span v-if="!wb.connected" class="text-ink-2" data-test="dde-needs-connection">Connect first (ignition on, engine off).</span>
    </div>
    <template v-if="rep">
      <p v-if="rep.synthetic" class="mt-2 text-warn" data-test="dde-synthetic">SYNTHETIC simulator data: says nothing about the car or the adapter.</p>
      <div class="mt-2 flex flex-wrap gap-2">
        <span class="badge" data-test="dde-generic-verdict">{{ rep.verdicts.generic_obd.verdict }}</span>
        <span class="badge" :class="verdictClass(rep.verdicts.manufacturer_transport.verdict)" data-test="dde-mfr-verdict">
          {{ rep.verdicts.manufacturer_transport.verdict }}
        </span>
      </div>
      <p class="mt-1 text-ink-2">{{ rep.verdicts.manufacturer_transport.meaning }}</p>
      <ul class="list-disc pl-5" data-test="dde-reasons">
        <li v-for="b in rep.verdicts.manufacturer_transport.blockers" :key="b" class="text-crit">blocker: {{ b }}</li>
        <li v-for="b in rep.verdicts.manufacturer_transport.unverified" :key="b">unverified: {{ b }}</li>
      </ul>
      <table class="mt-2 w-full text-left" data-test="dde-fields">
        <thead><tr class="label"><th>Field</th><th>Value</th><th>Quality</th><th>Source</th></tr></thead>
        <tbody>
          <tr v-for="[name, f] in rows" :key="name">
            <td>{{ name }}</td><td>{{ show(f) }}</td><td><span class="badge">{{ f.quality }}</span></td><td class="text-ink-2">{{ f.source ?? '-' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="rep.session_id" class="mt-2">
        <a class="underline" data-test="dde-export" :href="`/api/sessions/${rep.session_id}/export`">Export this assessment for review (ZIP, VIN redacted)</a>
      </p>
    </template>
    <p v-else class="mt-2 text-ink-2" data-test="dde-none">No capability check stored for this vehicle yet.</p>

    <template v-if="dde?.supported && dde.settings">
      <h3 class="label mt-4">Manufacturer reads (gated, off by default)</h3>
      <p class="text-ink-2">
        A BMW request runs only when the catalog entry is vehicle-verified, you opted in, the DDE variant you confirmed matches,
        and the adapter verdict above is a candidate. Nothing is sent otherwise.
      </p>
      <div class="mt-1 flex flex-wrap items-end gap-3">
        <label class="flex items-center gap-1">
          <input type="checkbox" data-test="dde-opt-in" :checked="dde.settings.opt_in" @change="toggleOptIn" /> I opt in to read-only manufacturer reads
        </label>
        <label>
          <span class="label block">DDE variant confirmed by a BMW tester</span>
          <input v-model="variant" class="field w-56" data-test="dde-variant" placeholder="not confirmed" />
        </label>
        <button class="btn" data-test="dde-save-variant" @click="saveVariant">Save variant</button>
      </div>
      <p class="mt-1" data-test="dde-catalog-summary">
        Catalog {{ dde.version }}{{ dde.synthetic ? ' (SYNTHETIC, simulator only)' : '' }}:
        {{ dde.availability?.filter((m) => m.status === 'executable').length ?? 0 }} of {{ dde.availability?.length ?? 0 }} metrics executable.
      </p>
      <table class="mt-1 w-full text-left" data-test="dde-availability">
        <thead><tr class="label"><th>Metric</th><th>Status</th><th>Why</th></tr></thead>
        <tbody>
          <tr v-for="m in dde.availability" :key="m.metric">
            <td>{{ m.title ?? m.metric }}</td>
            <td><span class="badge" :class="m.status === 'executable' ? 'text-good' : 'text-warn'">{{ m.status }}</span></td>
            <td class="text-ink-2">{{ m.reasons[0] ?? '' }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>
