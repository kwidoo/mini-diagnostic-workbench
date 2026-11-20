<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useWorkbench } from '../../stores/workbench'
import type { DdeMonitoringSummary, MonitorDde, MonitorDdeSignal } from '../../types'

/** live: the running recording's DDE state (+ the link state); summary: a stored session's DDE section.
 *  Live freshness is re-evaluated on the panel's own clock: a measurement expires at its deadline even
 *  when no new monitoring event arrives (e.g. after the ECU went silent). Summaries are not affected. */
const props = defineProps<{
  live?: MonitorDde | null; linkState?: string | null; summary?: DdeMonitoringSummary | null; inferredRegenerations?: number
}>()
const wb = useWorkbench()
const nowS = ref(Date.now() / 1000)
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  if (props.live !== undefined) timer = setInterval(() => (nowS.value = Date.now() / 1000), 1000)
})
onUnmounted(() => clearInterval(timer))
const linkOk = computed(() => !props.linkState || props.linkState === 'running')
function sigAge(s: MonitorDdeSignal): number | null {
  if (s.t_wall === undefined || s.t_wall === null) return s.age_s
  return Math.max(0, Math.round((nowS.value - s.t_wall) * 10) / 10)
}
function sigStale(s: MonitorDdeSignal): boolean {
  if (!linkOk.value) return true
  if (s.fresh_until_wall === undefined || s.fresh_until_wall === null) return s.stale
  return nowS.value > s.fresh_until_wall
}
const regenState = computed(() => {
  const r = props.live?.regeneration
  if (!r || !linkOk.value) return 'unknown'
  if (r.fresh_until_wall === undefined) return r.state // older backend: server-side state only
  if (r.fresh_until_wall === null || nowS.value > r.fresh_until_wall) return 'unknown'
  return r.state
})
/** shares are fractions (0..1); scheduling shares are of wall-clock time, busy_time_share of link busy time */
const pct = (x: number) => Math.round(x * 100)
const t = (x: number | null | undefined) => (x === null || x === undefined ? 'n/a' : `${Math.round(x)} s`)
const fmt = (v: number | null, unit: string | null) => (v === null ? '-' : `${Math.round(v * 100) / 100} ${unit ?? ''}`.trim())
const regenText = computed(() => {
  const s = regenState.value
  if (s === 'active') return 'ACTIVE (confirmed by DDE)'
  if (s === 'inactive') return 'not active (confirmed by DDE)'
  return 'unknown (no fresh DDE flag)'
})
const offReason = computed(() => {
  const st = wb.ddeStatus
  if (!st?.supported) return 'not applicable to this vehicle'
  if (!st.settings?.opt_in) return 'manufacturer reads are off (opt in on the Vehicle tab)'
  const n = st.availability?.filter((a) => a.status === 'executable').length ?? 0
  return n ? 'not polled in this recording' : 'no verified DDE signal for this car / adapter: generic OBD only'
})
</script>

<template>
  <section class="panel text-sm" data-test="dde-monitor">
    <h2 class="h2">
      BMW DDE values (manufacturer, source <code>bmw_dde</code>)
      <span v-if="(live?.synthetic ?? summary?.synthetic)" class="badge text-warn">SYNTHETIC</span>
    </h2>

    <template v-if="live !== undefined">
      <p v-if="!live" class="text-ink-2" data-test="dde-off">Not read: {{ offReason }}.</p>
      <template v-else>
        <p data-test="dde-regen-live">
          DPF regeneration: <b>{{ regenText }}</b>
          <span v-if="live.unsupported_adapter" class="text-crit"> · adapter cannot provide the addressing: {{ live.unsupported_adapter }}</span>
        </p>
        <table class="mt-1 w-full text-left" data-test="dde-live-signals">
          <thead><tr class="label"><th>Signal</th><th>Value</th><th>Quality</th><th>Age</th><th>Interval</th></tr></thead>
          <tbody>
            <tr v-for="(s, key) in live.signals" :key="key" :data-test="`dde-sig-${key}`">
              <td>{{ s.title ?? key }}</td>
              <td :class="sigStale(s) ? 'text-ink-2' : ''">
                <template v-if="s.quality !== 'ok'">not measured</template>
                <template v-else-if="sigStale(s)">last {{ fmt(s.value, s.unit) }} (not current)</template>
                <template v-else>{{ fmt(s.value, s.unit) }}</template>
              </td>
              <td><span class="badge" :class="s.quality === 'ok' && !sigStale(s) ? '' : 'text-warn'">{{ s.suspended ? 'suspended' : s.quality ?? 'waiting' }}</span></td>
              <td :class="sigStale(s) ? 'text-warn' : ''" data-test="dde-age">{{ sigAge(s) === null ? '-' : `${sigAge(s)} s` }}{{ sigStale(s) ? ' (stale)' : '' }}</td>
              <td class="text-ink-2">{{ s.interval_s }} s</td>
            </tr>
          </tbody>
        </table>
        <p class="mt-1 text-xs text-ink-2" data-test="dde-scheduling">
          DDE intervals stretched ×{{ live.scale }} for this adapter<template v-if="live.scheduling">; target
          {{ pct(live.scheduling.target_share) }} % of wall-clock link time, measured {{ pct(live.scheduling.measured_share) }} %<template v-if="live.scheduling.busy_time_share != null"> ({{ pct(live.scheduling.busy_time_share) }} % of busy link time)</template><template v-if="live.scheduling.target_exceeded"> (target exceeded: slow adapter)</template></template>.
          Catalog {{ live.catalog_version }}.
        </p>
        <p v-if="!linkOk" class="text-xs text-warn" data-test="dde-link-paused">Vehicle polling paused ({{ linkState }}): values below are not current.</p>
      </template>
    </template>

    <template v-else-if="summary">
      <p v-if="summary.status === 'not_recorded'" class="text-ink-2" data-test="dde-not-recorded">{{ summary.reason }}</p>
      <template v-else>
        <p class="text-ink-2" data-test="dde-session-status">
          {{ summary.status.replaceAll('_', ' ') }}<template v-if="summary.reason"> - {{ summary.reason }}</template>
          · catalog {{ summary.catalog_version }} · variant {{ summary.confirmed_variant ?? '-' }}
        </p>
        <div class="mt-1" data-test="dde-regen-session">
          <div v-for="(r, i) in summary.regeneration?.confirmed ?? []" :key="i" data-test="regen-confirmed">
            <span class="badge text-good">confirmed by DDE</span> {{ t(r.t_start_rel_s) }}: {{ r.text }}
          </div>
          <p v-if="!(summary.regeneration?.confirmed ?? []).length" class="text-ink-2">
            No regeneration confirmed by the DDE{{ summary.regeneration?.status === 'measured' ? '' : ' (flag not measured)' }}.
          </p>
          <p v-if="inferredRegenerations" class="text-xs text-ink-2" data-test="regen-inferred-note">
            {{ inferredRegenerations }} possible regeneration(s) inferred from generic OBD (see DPF observations): a separate, heuristic signal.
          </p>
        </div>
        <table v-if="summary.signals?.length" class="mt-2 w-full text-left" data-test="dde-session-signals">
          <thead><tr class="label"><th>Signal</th><th>Status</th><th>ok</th><th>min / median / max</th></tr></thead>
          <tbody>
            <tr v-for="s in summary.signals" :key="s.signal_key">
              <td>{{ s.title }}</td><td>{{ s.status }}</td><td>{{ s.samples_ok }}</td>
              <td>{{ s.stats ? `${s.stats.min} / ${s.stats.median} / ${s.stats.max} ${s.unit ?? ''}` : '-' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-for="(v, k) in summary.commanded_vs_actual ?? {}" :key="k" class="text-xs">{{ k }}: commanded − actual mean {{ v.mean_delta }}, max |Δ| {{ v.max_abs_delta }} ({{ v.pairs }} pairs; {{ v.note }})</p>
        <p v-for="l in summary.limitations ?? []" :key="l" class="text-xs text-ink-2">{{ l }}</p>
      </template>
    </template>
  </section>
</template>
