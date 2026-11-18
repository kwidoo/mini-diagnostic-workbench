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
const na = (x: number | null | undefined) => (x === null || x === undefined ? 'n/a' : x)
const trends = computed(() => wb.airPathTrends.filter((tr) => tr.sessions))
const origin = (r: (typeof wb.history)[number]) => r.baseline_origin ?? (r.synthetic ? 'synthetic' : 'real')
/** per data origin (real / synthetic, never mixed) and drive-type label: how often the fan ran
 * after a stop, and how long (manual markers). Stops are shutdown episodes, not markers. */
const groups = computed(() => {
  const by: Record<string, typeof wb.history> = {}
  for (const r of wb.history) (by[`${origin(r)}|${r.drive_type ?? 'unlabelled'}`] ??= []).push(r)
  return Object.entries(by).map(([k, rs]) => ({
    key: k,
    origin: k.split('|')[0],
    type: k.split('|')[1],
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
        <thead><tr class="label"><th>Data</th><th>Drive type (your label)</th><th>Sessions</th><th>Engine stops</th><th>Fan runs after stop</th><th>Mean fan run</th>
          <th>Battery warnings</th><th>Possible regen.</th><th>Possibly interrupted</th></tr></thead>
        <tbody>
          <tr v-for="g in groups" :key="g.key" class="border-t border-line" :data-test="`group-${g.origin}-${g.type}`">
            <td><span v-if="g.origin === 'synthetic'" class="badge text-warn">SYNTHETIC</span><span v-else>real</span></td><td>{{ g.type }}</td><td>{{ g.sessions }}</td><td>{{ g.stops }}</td><td>{{ g.fanRuns }}</td>
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
            <th>MAF low / 0 per 1000 running <DataBadge kind="measured" /></th><th>MAF episodes (/h) <DataBadge kind="candidate" /></th>
            <th>EGR-error excursions / h</th><th>Generic DTCs / MIL</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id" class="border-t border-line" :data-test="`history-${r.id}`">
            <td>{{ when(r.started_at) }} <span v-if="r.synthetic" class="badge text-warn">SYNTHETIC</span><span v-if="r.interrupted" class="badge text-crit" data-test="interrupted">interrupted (recovered)</span><span v-else-if="r.aborted" class="badge text-crit">ended early</span></td>
            <td>{{ r.label }}</td><td>{{ r.drive_type ?? '-' }}</td><td>{{ r.running_min }} min</td><td>{{ r.max_speed_kmh ?? 'n/a' }}</td>
            <td>{{ r.engine_stops }}</td>
            <td>{{ r.fan_runs_after_shutdown }}<template v-if="r.fan_durations_s.length"> ({{ r.fan_durations_s.map((d) => (d === null ? 'open' : `${Math.round(d)} s`)).join(', ') }})</template></td>
            <td>{{ r.battery_warnings }}</td>
            <td>{{ r.possible_regenerations }}<template v-if="r.possible_interrupted_regenerations"> ({{ r.possible_interrupted_regenerations }} possibly interrupted)</template></td>
            <td>{{ r.user_suspected_regenerations }}</td>
            <td>{{ r.coolant_max_c ?? 'n/a' }}</td>
            <td>{{ r.ecu_voltage_running_median ?? 'n/a' }} / {{ r.adapter_voltage_running_median ?? 'n/a' }}</td>
            <td :data-test="`airpath-maf-${r.id}`">{{ r.air_path ? `${na(r.air_path.maf_low_per_1k)} / ${na(r.air_path.maf_zero_per_1k)}` : 'n/a' }}</td>
            <td>{{ r.air_path ? `${na(r.air_path.maf_episodes)} (${na(r.air_path.maf_episodes_per_h)})` : 'n/a' }}</td>
            <td>{{ r.air_path ? na(r.air_path.egr_excursions_per_h) : 'n/a' }}</td>
            <td>{{ r.air_path?.dtcs ? (r.air_path.dtcs.join(', ') || 'none') : 'not scanned' }}{{ r.air_path?.mil_on ? ' · MIL on' : '' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="text-ink-2">No monitoring sessions{{ filter ? ' with this label' : '' }} yet.</p>
      <div v-for="tr in trends" :key="`${tr.cohort?.origin}-${tr.cohort?.vehicle_identity}`" class="mt-3" :data-test="`airpath-trend-${tr.cohort?.origin}`">
        <h3 class="label">
          Air-path trend <span v-if="tr.cohort?.origin === 'synthetic'" class="badge text-warn">SYNTHETIC</span><span v-else>real</span>
          · {{ tr.sessions_with_maf }} of {{ tr.sessions }} session(s) with MAF
        </h3>
        <table class="w-full text-left">
          <thead><tr class="label"><th>Drive type</th><th>Sessions</th><th>MAF low / 1000 (median)</th><th>MAF 0 / 1000</th><th>Episodes / h</th><th>EGR excursions / h</th></tr></thead>
          <tbody>
            <tr class="border-t border-line"><td>all</td><td>{{ tr.sessions_with_maf }}</td><td>{{ na(tr.medians.maf_low_per_1k) }}</td><td>{{ na(tr.medians.maf_zero_per_1k) }}</td>
              <td>{{ na(tr.medians.maf_episodes_per_h) }}</td><td>{{ na(tr.medians.egr_excursions_per_h) }}</td></tr>
            <tr v-for="(g, k) in tr.by_drive_type" :key="k" class="border-t border-line">
              <td>{{ k }}</td><td>{{ g.sessions }}</td><td>{{ na(g.maf_low_per_1k) }}</td><td>{{ na(g.maf_zero_per_1k) }}</td><td>{{ na(g.maf_episodes_per_h) }}</td><td>{{ na(g.egr_excursions_per_h) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="text-xs">
          Cold: {{ tr.cold ? `${na(tr.cold.low_per_1k)} low / 1000 (${tr.cold.episodes} episodes)` : 'n/a' }} ·
          warm: {{ tr.warm ? `${na(tr.warm.low_per_1k)} low / 1000 (${tr.warm.episodes} episodes)` : 'n/a' }} ·
          idle: {{ tr.idle ? `${na(tr.idle.low_per_1k)}` : 'n/a' }} · moving: {{ tr.moving ? `${na(tr.moving.low_per_1k)}` : 'n/a' }}
        </p>
        <p class="text-xs" :data-test="`airpath-dtc-recurrence-${tr.cohort?.origin}`">
          Generic DTC recurrence ({{ tr.sessions_scanned }} scanned session(s)):
          <template v-if="tr.dtc_recurrence.length">
            <span v-for="d in tr.dtc_recurrence" :key="d.code" class="mr-2">{{ d.code }} in {{ d.sessions }} ({{ Math.round(100 * d.share_of_scanned_sessions) }} %)</span>
          </template>
          <template v-else>none</template> · MIL on in {{ tr.mil_on_sessions }} session(s)
        </p>
        <p class="text-xs text-ink-2">{{ tr.note }}</p>
      </div>
      <p class="mt-2 text-xs text-ink-2">
        Fan runs and battery warnings come from your markers. Possible regenerations are a heuristic, relative to this car's own idle
        baseline, and are never confirmed. Real and synthetic sessions are grouped and judged separately: a synthetic
        recording never feeds the real car's baseline. Engine stops count shutdown episodes (stop, ignition-off and ECU silence = one).
        Air-path rates are per 1000 MAF samples with the engine confirmed running, or per running hour; 'n/a' means the signal was not
        available in that session (never zero). Owner-reported BMW codes are not part of these counts.
      </p>
    </section>
  </div>
</template>
