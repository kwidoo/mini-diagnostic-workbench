<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useWorkbench } from '../../stores/workbench'
import DataBadge from './DataBadge.vue'

const wb = useWorkbench()
const filter = ref('')
onMounted(() => wb.loadHistory())
const rows = computed(() => wb.history.filter((r) => !filter.value || (r.drive_type ?? 'unlabelled') === filter.value))
const when = (ts: number) => new Date(ts * 1000).toLocaleString()
const mean = (xs: number[]) => (xs.length ? Math.round(xs.reduce((a, b) => a + b, 0) / xs.length) : null)
/** per drive-type label: how often the fan ran after a stop, and how long (manual markers) */
const groups = computed(() => {
  const by: Record<string, typeof wb.history> = {}
  for (const r of wb.history) (by[r.drive_type ?? 'unlabelled'] ??= []).push(r)
  return Object.entries(by).map(([k, rs]) => ({
    type: k,
    sessions: rs.length,
    stops: rs.reduce((a, r) => a + r.engine_stops, 0),
    fanRuns: rs.reduce((a, r) => a + r.fan_runs_after_shutdown, 0),
    fanMean: mean(rs.flatMap((r) => r.fan_durations_s.filter((d): d is number => d !== null))),
    warnings: rs.reduce((a, r) => a + r.battery_warnings, 0),
    possible: rs.reduce((a, r) => a + r.possible_regenerations, 0),
    interrupted: rs.reduce((a, r) => a + r.possible_interrupted_regenerations, 0),
  }))
})
</script>

<template>
  <div class="grid gap-3" data-test="monitoring-history">
    <section class="panel text-sm">
      <h2 class="h2">Compare drives</h2>
      <div class="mb-2 flex flex-wrap items-center gap-2">
        <label>Drive type
          <select v-model="filter" class="field" data-test="history-filter">
            <option value="">all</option><option value="city">city</option><option value="highway">highway</option>
            <option value="mixed">mixed</option><option value="other">other</option><option value="unlabelled">unlabelled</option>
          </select>
        </label>
        <button class="btn" @click="wb.loadHistory()">Refresh</button>
      </div>
      <table v-if="groups.length" class="mb-3 w-full text-left" data-test="history-groups">
        <thead><tr class="label"><th>Drive type (your label)</th><th>Sessions</th><th>Engine stops</th><th>Fan runs after stop</th><th>Mean fan run</th>
          <th>Battery warnings</th><th>Possible regen.</th><th>Possibly interrupted</th></tr></thead>
        <tbody>
          <tr v-for="g in groups" :key="g.type" class="border-t border-line" :data-test="`group-${g.type}`">
            <td>{{ g.type }}</td><td>{{ g.sessions }}</td><td>{{ g.stops }}</td><td>{{ g.fanRuns }}</td>
            <td>{{ g.fanMean !== null ? `${g.fanMean} s` : 'n/a' }}</td><td>{{ g.warnings }}</td><td>{{ g.possible }}</td><td>{{ g.interrupted }}</td>
          </tr>
        </tbody>
      </table>
      <table v-if="rows.length" class="w-full text-left" data-test="history-table">
        <thead>
          <tr class="label">
            <th>Started</th><th>Label</th><th>Type</th><th>Running</th><th>Max km/h</th><th>Stops</th>
            <th>Fan after stop <DataBadge kind="manual" /></th><th>Battery warnings <DataBadge kind="manual" /></th>
            <th>Possible regen. <DataBadge kind="inferred" /></th><th>Suspected <DataBadge kind="manual" /></th>
            <th>Coolant max</th><th>ECU V / adapter V (running)</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id" class="border-t border-line" :data-test="`history-${r.id}`">
            <td>{{ when(r.started_at) }} <span v-if="r.synthetic" class="badge text-warn">SYNTHETIC</span><span v-if="r.aborted" class="badge text-crit">ended early</span></td>
            <td>{{ r.label }}</td><td>{{ r.drive_type ?? '-' }}</td><td>{{ r.running_min }} min</td><td>{{ r.max_speed_kmh ?? 'n/a' }}</td>
            <td>{{ r.engine_stops }}</td>
            <td>{{ r.fan_runs_after_shutdown }}<template v-if="r.fan_durations_s.length"> ({{ r.fan_durations_s.map((d) => (d === null ? 'open' : `${Math.round(d)} s`)).join(', ') }})</template></td>
            <td>{{ r.battery_warnings }}</td>
            <td>{{ r.possible_regenerations }}<template v-if="r.possible_interrupted_regenerations"> ({{ r.possible_interrupted_regenerations }} possibly interrupted)</template></td>
            <td>{{ r.user_suspected_regenerations }}</td>
            <td>{{ r.coolant_max_c ?? 'n/a' }}</td>
            <td>{{ r.ecu_voltage_running_median ?? 'n/a' }} / {{ r.adapter_voltage_running_median ?? 'n/a' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="text-ink-2">No monitoring sessions{{ filter ? ' with this label' : '' }} yet.</p>
      <p class="mt-2 text-xs text-ink-2">
        Fan runs and battery warnings come from your markers. Possible regenerations are a heuristic, relative to this car's own idle
        baseline, and are never confirmed. Mixed synthetic and real sessions must not be compared.
      </p>
    </section>
  </div>
</template>
