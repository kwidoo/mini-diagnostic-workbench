<script setup lang="ts">
import type { DtcScan } from '../types'
defineProps<{ scan: DtcScan }>()
const kinds = ['stored', 'pending', 'permanent']
const when = (t: number) => new Date(t * 1000).toLocaleString()
</script>

<template>
  <div class="panel" data-test="scan">
    <div class="mb-2 flex flex-wrap items-center gap-2">
      <span class="text-sm font-semibold capitalize">{{ scan.phase }} scan</span>
      <span class="text-xs text-ink-2">{{ when(scan.t_wall) }}</span>
      <span class="badge" :class="scan.mil_on ? 'text-crit' : ''">MIL: {{ scan.mil_on === null ? 'not reported' : scan.mil_on ? 'ON' : 'off' }}</span>
    </div>
    <div class="grid gap-3 md:grid-cols-3">
      <div v-for="k in kinds" :key="k" :data-test="`dtc-${k}`">
        <div class="label">{{ k }}</div>
        <template v-if="scan.mode_status[k] === 'supported'">
          <ul v-if="scan.dtcs.some((d) => d.kind === k)" class="text-sm">
            <li v-for="d in scan.dtcs.filter((q) => q.kind === k)" :key="d.code + d.ecu">
              <b>{{ d.code }}</b> <span class="text-ink-2">raw {{ d.raw_hex }} · ECU {{ d.ecu ?? 'unknown' }}</span>
            </li>
          </ul>
          <p v-else class="text-sm text-ink-2">None stored.</p>
        </template>
        <p v-else class="text-sm text-ink-2">Not supported / not read ({{ scan.mode_status[k] ?? 'not requested' }}).</p>
      </div>
    </div>
    <div v-if="scan.freeze_frame" class="mt-3" data-test="freeze">
      <div class="label">Freeze frame</div>
      <p v-if="scan.freeze_frame.status === 'present'" class="text-sm">
        Stored for <b>{{ scan.freeze_frame.dtc }}</b> (raw {{ scan.freeze_frame.dtc_raw_hex }}, ECU {{ scan.freeze_frame.ecu }}):
        <span v-for="r in scan.freeze_frame.readings.filter((q) => q.value !== null)" :key="r.pid" class="mr-3 whitespace-nowrap">
          {{ r.name }} <b>{{ r.value }} {{ r.unit }}</b>
        </span>
      </p>
      <p v-else class="text-sm text-ink-2">
        {{ scan.freeze_frame.status === 'none_stored' ? 'No freeze frame stored.' : `Not supported / not read (${scan.freeze_frame.status}).` }}
      </p>
    </div>
    <div v-if="scan.manufacturer" class="mt-3 border-t border-line pt-2" data-test="dde-faults">
      <div class="label">
        BMW DDE fault memory (measured, separate from generic codes)
        <span v-if="scan.manufacturer.synthetic" class="badge text-warn">SYNTHETIC</span>
      </div>
      <p class="text-sm" data-test="dde-fault-status">
        <span class="badge">{{ scan.manufacturer.status.replaceAll('_', ' ') }}</span>
        <span v-if="scan.manufacturer.status === 'no_faults'"> Complete read: no fault listed.</span>
        <span v-else-if="scan.manufacturer.detail" class="text-ink-2"> {{ scan.manufacturer.detail }}</span>
      </p>
      <ul v-if="scan.manufacturer.faults.length" class="text-sm">
        <li v-for="f in scan.manufacturer.faults" :key="f.raw_hex" data-test="dde-fault">
          <b>{{ f.code }}</b>
          <span class="text-ink-2">
            manufacturer code as returned · raw {{ f.raw_hex }} · status byte {{ f.status_raw ?? '-' }}
            <template v-if="f.status"> ({{ Object.entries(f.status).filter(([, v]) => v).map(([k]) => k).join(', ') || 'no status bit set' }})</template>
            · ECU {{ scan.manufacturer.ecu ?? 'unknown' }}
          </span>
        </li>
      </ul>
    </div>
    <p class="mt-2 text-xs text-ink-2">
      Generic OBD codes above. {{ scan.manufacturer ? 'BMW DDE fault memory is listed separately.' : 'Manufacturer-specific fault memory was not part of this scan.' }} Nothing was cleared.
    </p>
  </div>
</template>
