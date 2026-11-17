<script setup lang="ts">
import { reactive, watch } from 'vue'
import { useWorkbench } from '../stores/workbench'

const wb = useWorkbench()
const famName = (f: string) => (f === 'can' ? 'CAN (ISO 15765-4)' : f === 'kline' ? 'K-line (ISO 9141-2 / ISO 14230-4)' : f)
const form = reactive<Record<string, string>>({ model: '', engine: '', ecu: '', model_year: '' })
watch(() => wb.vehicle, (v) => v && Object.assign(form, v.confirmed), { immediate: true })
function save() {
  if (wb.vehicle) return wb.saveVehicle({ ...wb.vehicle, confirmed: { ...form } })
}
</script>

<template>
  <div class="grid gap-3">
    <section class="panel">
      <h2 class="h2">Vehicle profile</h2>
      <div class="grid gap-2 md:grid-cols-2" data-test="profile-cards">
        <button
          v-for="p in wb.profiles" :key="p.id" class="rounded-lg border p-3 text-left" :data-test="`profile-${p.id}`"
          :class="p.id === wb.activeProfileId ? 'border-ink' : 'border-line'" :disabled="!!wb.recording" @click="wb.setProfile(p.id)"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="font-semibold">{{ p.name }}</span>
            <span class="badge">{{ p.id === wb.activeProfileId ? 'selected' : 'select' }}</span>
          </div>
          <div class="text-sm text-ink-2">{{ p.engine }} · expected protocol: {{ p.expected_protocol_families.map(famName).join(' / ') }}</div>
          <div class="text-xs text-ink-2">Profile status: {{ p.status === 'complete' ? 'complete' : 'in progress (added incrementally)' }}</div>
        </button>
      </div>
    </section>

    <template v-if="wb.profile">
      <section class="panel text-sm">
        <h2 class="h2">Identity</h2>
        <p data-test="identity-note">{{ wb.profile.identity_note }}</p>
        <p class="mt-1 text-ink-2">{{ wb.profile.protocol_note }}</p>
        <div v-for="e in wb.profile.ecus" :key="e.id" class="mt-2">
          <span class="label">ECU definition</span>
          <div>{{ e.name }} <span class="badge">{{ e.variant_confirmed ? 'variant confirmed' : 'variant unconfirmed' }}</span></div>
          <div class="text-ink-2">{{ e.variant_note }}</div>
        </div>
        <h3 class="label mt-3">Confirmed by you (nothing here is inferred)</h3>
        <div class="mt-1 flex flex-wrap items-end gap-2">
          <label v-for="k in ['model', 'engine', 'ecu', 'model_year']" :key="k">
            <span class="label block">{{ k.replace('_', ' ') }}</span>
            <input v-model="form[k]" class="field w-40" :data-test="`confirm-${k}`" placeholder="not confirmed" />
          </label>
          <button class="btn" data-test="save-vehicle" @click="save">Save</button>
        </div>
        <p v-if="wb.lastScan?.meta.vehicle_info" class="mt-2 text-ink-2">
          Read from the car in the last scan: calibration ID
          {{ wb.lastScan.meta.vehicle_info.calibration_ids.map((c) => c.text).join(', ') || 'not reported' }}; VIN
          {{ wb.lastScan.meta.vehicle_info.vin ?? `not shown (${wb.lastScan.meta.vehicle_info.vin_status})` }}.
        </p>
      </section>

      <section v-if="wb.vehicle" class="panel text-sm">
        <h2 class="h2">Owner-reported history (not measured by this tool)</h2>
        <p v-if="!wb.vehicle.owner_reported.symptoms.length && !wb.vehicle.owner_reported.repairs.length" class="text-ink-2">Nothing recorded for this vehicle.</p>
        <ul class="list-disc pl-5">
          <li v-for="s in wb.vehicle.owner_reported.symptoms" :key="s">Symptom: {{ s }}</li>
          <li v-for="c in wb.vehicle.owner_reported.manufacturer_fault_codes" :key="c.code">{{ c.system }} fault {{ c.code }} ({{ c.source }})</li>
          <li v-for="r in wb.vehicle.owner_reported.repairs" :key="r.description">
            {{ r.description }}: {{ r.verified ? 'verified' : 'owner-reported, not independently verified' }}
          </li>
        </ul>
        <p class="mt-1 text-ink-2">{{ wb.vehicle.owner_reported.notes }}</p>
      </section>

      <section v-for="ext in wb.profile.manufacturer" :key="ext.id" class="panel text-sm" data-test="manufacturer">
        <h2 class="h2">Manufacturer-specific data: {{ ext.enabled ? 'enabled' : 'not available' }}</h2>
        <p class="text-ink-2">{{ ext.access_requirements }}</p>
        <table class="mt-2 w-full text-left">
          <thead><tr class="label"><th>Parameter</th><th>Value</th></tr></thead>
          <tbody>
            <tr v-for="p in ext.parameters" :key="p.key"><td>{{ p.name }}</td><td class="text-ink-2">not supported / not measured</td></tr>
          </tbody>
        </table>
      </section>

      <section class="panel text-sm">
        <h2 class="h2">First-run checklist</h2>
        <ol class="list-decimal space-y-1 pl-5"><li v-for="c in wb.profile.first_run_checklist" :key="c">{{ c }}</li></ol>
      </section>
    </template>
  </div>
</template>
