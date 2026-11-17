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
    <p class="mt-2 text-xs text-ink-2">
      Generic OBD codes only. Manufacturer-specific fault memory is not readable with this tool. Nothing was cleared.
    </p>
  </div>
</template>
