<script setup lang="ts">
import { computed } from 'vue'
import type { AirPath, Held } from '../../types'
import DataBadge from './DataBadge.vue'

/** Air-path section of a monitoring session: MAF screening, EGR-error excursions, associations, DTCs.
 * Unavailable values are shown as 'n/a' / 'not available', never as 0. */
const props = defineProps<{ ap: AirPath }>()
const t = (x: number | null | undefined) => (x === null || x === undefined ? 'n/a' : `${Math.round(x)} s`)
const na = (x: number | null | undefined, unit = '') => (x === null || x === undefined ? 'n/a' : `${x}${unit}`)
const held = (h: Held | null | undefined, unit = '') =>
  !h ? 'not measured' : h.fresh ? `${h.value}${unit} (${h.age_s} s old)` : `stale (${h.age_s} s old)`
const maf = computed(() => props.ap.maf)
const measured = computed(() => props.ap.signals.maf?.status === 'measured')
const groups = computed(() =>
  [
    ...Object.entries(maf.value.by_temperature ?? {}),
    ...Object.entries(maf.value.by_motion ?? {}),
  ].filter(([, g]) => g.running_samples || g.episodes),
)
</script>

<template>
  <section class="panel text-sm" data-test="air-path-panel">
    <h2 class="h2">Air path (MAF / EGR / MAP / fault codes)</h2>
    <p class="text-xs text-ink-2">
      Derived automatically from this recording. MAF below {{ ap.heuristics.maf_low_gs }} g/s (or exactly 0) counts only while measured RPM was
      at least {{ ap.heuristics.airpath_running_rpm }} rpm right before and right after the sample. These are screening values of this tool, not
      manufacturer limits, and no component is diagnosed.
    </p>
    <div class="mt-2 flex flex-wrap gap-2 text-xs">
      <span v-for="(s, k) in ap.signals" :key="k">{{ s.name }} <DataBadge :kind="s.status" /></span>
    </div>

    <template v-if="measured">
      <table class="mt-2 w-full text-left" data-test="air-path-stats">
        <tbody>
          <tr class="border-t border-line"><td class="label">MAF samples</td>
            <td>{{ maf.samples_ok }} with a value · engine running {{ maf.running_state?.running_confirmed }} · not running {{ maf.running_state?.engine_not_running }} ·
              unknown {{ maf.running_state?.unknown }} · failed reads {{ maf.failed_reads.n }} (no value, not zero)</td></tr>
          <tr class="border-t border-line" data-test="air-path-maf-counts"><td class="label">While running <DataBadge kind="measured" /></td>
            <td>{{ maf.zero_running }} at 0.00 g/s · {{ maf.low_running }} below {{ ap.heuristics.maf_low_gs }} g/s
              (per 1000 running samples: {{ na(maf.zero_per_1k_running) }} / {{ na(maf.low_per_1k_running) }})</td></tr>
          <tr class="border-t border-line"><td class="label">Episodes <DataBadge kind="candidate" /></td>
            <td>{{ maf.episode_count }} ({{ maf.persistent_episodes }} persistent, {{ maf.transient_episodes }} single, {{ maf.repeated_clusters }} repeated cluster(s)),
              {{ maf.episode_total_s }} s in total · abrupt dips: {{ maf.dips.length }}</td></tr>
          <tr class="border-t border-line"><td class="label">Engine off</td><td>{{ maf.zero_engine_not_running }} sample(s) at 0 g/s with the engine measured off (expected, not counted)</td></tr>
        </tbody>
      </table>
      <table v-if="groups.length" class="mt-2 w-full text-left" data-test="air-path-groups">
        <thead><tr class="label"><th>Condition</th><th>Running samples</th><th>Low</th><th>Zero</th><th>Low / 1000</th><th>Episodes</th></tr></thead>
        <tbody>
          <tr v-for="[k, g] in groups" :key="k" class="border-t border-line">
            <td>{{ k }}</td><td>{{ g.running_samples }}</td><td>{{ g.low }}</td><td>{{ g.zero }}</td><td>{{ na(g.low_per_1k) }}</td><td>{{ g.episodes }}</td>
          </tr>
        </tbody>
      </table>
      <table v-if="maf.episodes.length" class="mt-2 w-full text-left" data-test="air-path-episodes">
        <thead><tr class="label"><th>Time</th><th>MAF</th><th>Pattern</th><th>Context</th><th>Raw answer</th></tr></thead>
        <tbody>
          <tr v-for="(e, i) in maf.episodes" :key="i" class="border-t border-line" data-test="maf-episode">
            <td>{{ t(e.t_start_rel_s) }} · {{ e.duration_s }} s</td>
            <td>min {{ e.min_maf_gs }} g/s ({{ e.samples }} sample(s)) <DataBadge kind="candidate" /></td>
            <td>{{ e.pattern.replace('_', ' ') }}{{ e.repeated ? ', repeated' : '' }} · {{ e.confidence }} confidence</td>
            <td class="text-xs">
              {{ e.motion }}, {{ e.temperature_state }} · MAP {{ held(e.context.map, ' kPa') }} · EGR error {{ held(e.context.egr_error, ' %') }}
              <template v-if="e.rpm.unstable"> · <span class="text-warn">idle RPM unstable</span></template>
            </td>
            <td class="font-mono text-xs">{{ e.evidence[0]?.response ?? 'n/a' }} (ECU {{ e.ecu ?? '?' }})</td>
          </tr>
        </tbody>
      </table>
    </template>
    <p v-else class="mt-2 text-ink-2" data-test="air-path-maf-unavailable">MAF statistics not available in this session (not zero).</p>

    <p class="mt-2" data-test="air-path-egr">
      EGR error excursions (|2D| ≥ {{ ap.heuristics.egr_error_screen_pct }} %):
      <template v-if="ap.egr_error.count !== null">{{ ap.egr_error.count }} ({{ na(ap.egr_error.per_running_h) }} per running hour, max {{ na(ap.egr_error.max_abs_pct) }} %)</template>
      <DataBadge v-else kind="not_available" />
    </p>

    <div class="mt-2 grid gap-2 md:grid-cols-2" data-test="air-path-correlations">
      <div>
        <div class="label">Associations in time, supporting</div>
        <ul class="text-xs"><li v-for="(x, i) in ap.correlations.supporting" :key="i">{{ x }}</li><li v-if="!ap.correlations.supporting.length" class="text-ink-2">none</li></ul>
      </div>
      <div>
        <div class="label">Contradicting</div>
        <ul class="text-xs"><li v-for="(x, i) in ap.correlations.contradicting" :key="i">{{ x }}</li><li v-if="!ap.correlations.contradicting.length" class="text-ink-2">none</li></ul>
      </div>
    </div>
    <p class="text-xs text-ink-2">{{ ap.correlations.note }}</p>

    <div class="mt-2" data-test="air-path-dtcs">
      <div class="label">Fault codes (generic OBD, read in this session's scans)</div>
      <p v-if="!ap.dtcs.scans.length" class="text-ink-2">No fault scan in this session: status unknown.</p>
      <p v-else-if="!ap.dtcs.codes.length">No generic DTC reported. MIL {{ ap.dtcs.mil_on ? 'ON' : ap.dtcs.mil_on === false ? 'off' : 'unknown' }}.</p>
      <ul v-else>
        <li v-for="d in ap.dtcs.by_code" :key="d.code" :data-test="`dtc-${d.code}`">
          <b>{{ d.code }}</b> · ECU {{ d.ecus.join(', ') }} · {{ d.statuses.join(', ') }} · {{ d.scan_phases.join(', ') }} scan ·
          {{ d.description ?? 'description not verified for this ECU variant' }}
          <div class="text-xs text-ink-2">{{ d.timing }}</div>
        </li>
        <li>MIL {{ ap.dtcs.mil_on ? 'ON' : ap.dtcs.mil_on === false ? 'off' : 'unknown' }}</li>
      </ul>
      <p v-if="ap.dtcs.owner_reported_manufacturer_codes.length" class="mt-1 text-xs" data-test="owner-codes">
        Owner-reported BMW codes (not read by this tool): {{ ap.dtcs.owner_reported_manufacturer_codes.map((c) => c.code).join(', ') }}.
        <span class="text-ink-2">{{ ap.dtcs.owner_reported_note }}</span>
      </p>
    </div>

    <p v-if="ap.history" class="mt-2 text-xs" data-test="air-path-vs-history">
      Compared with this car's other sessions ({{ ap.history.other_sessions }}):
      <template v-if="ap.history.medians">median MAF low / 1000 {{ na(ap.history.medians.maf_low_per_1k) }} · episodes / h {{ na(ap.history.medians.maf_episodes_per_h) }} ·
        EGR excursions / h {{ na(ap.history.medians.egr_excursions_per_h) }}</template>
      <span class="text-ink-2"> {{ ap.history.note }}</span>
    </p>
    <ul class="mt-2 text-xs text-ink-2"><li v-for="l in ap.limitations" :key="l">{{ l }}</li></ul>
  </section>
</template>
