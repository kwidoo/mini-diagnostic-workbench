<script setup lang="ts">
import { computed } from 'vue'
import { useWorkbench } from '../../stores/workbench'
import type { DdeMonitoringSummary, MonitorDde } from '../../types'

/** live: the running recording's DDE state; summary: a stored session's DDE section. */
const props = defineProps<{ live?: MonitorDde | null; summary?: DdeMonitoringSummary | null; inferredRegenerations?: number }>()
const wb = useWorkbench()
const t = (x: number | null | undefined) => (x === null || x === undefined ? 'n/a' : `${Math.round(x)} s`)
const fmt = (v: number | null, unit: string | null) => (v === null ? '-' : `${Math.round(v * 100) / 100} ${unit ?? ''}`.trim())
const regenText = computed(() => {
  const s = props.live?.regeneration.state
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
              <td>{{ s.quality === 'ok' ? fmt(s.value, s.unit) : 'not measured' }}</td>
              <td><span class="badge" :class="s.quality === 'ok' ? '' : 'text-warn'">{{ s.suspended ? 'suspended' : s.quality ?? 'waiting' }}</span></td>
              <td :class="s.stale ? 'text-warn' : ''">{{ s.age_s === null ? '-' : `${s.age_s} s` }}{{ s.stale ? ' (stale)' : '' }}</td>
              <td class="text-ink-2">{{ s.interval_s }} s</td>
            </tr>
          </tbody>
        </table>
        <p class="mt-1 text-xs text-ink-2">DDE polling share is bounded; intervals stretched ×{{ live.scale }} for this adapter. Catalog {{ live.catalog_version }}.</p>
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
