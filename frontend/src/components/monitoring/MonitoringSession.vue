<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useWorkbench } from '../../stores/workbench'
import type { Signal, VoltageSource } from '../../types'
import LineChart from '../LineChart.vue'
import { coords, timeDomain } from '../chart'
import AirPathPanel from './AirPathPanel.vue'
import DdeMonitorPanel from './DdeMonitorPanel.vue'
import DataBadge from './DataBadge.vue'
import DataLegend from './DataLegend.vue'
import EventMarkerBar from './EventMarkerBar.vue'

const wb = useWorkbench()
const selected = ref('')
const candidates = computed(() => wb.sessions.filter((s) => s.monitoring))
watch(
  candidates,
  (list) => {
    if (!selected.value && list.length) selected.value = list[0].id
  },
  { immediate: true },
)
watch(selected, (id) => id && wb.loadMonitoring(id), { immediate: true })

const m = computed(() => (wb.monitoring && wb.monitoring.session_id === selected.value ? wb.monitoring : null))
const series = computed(() => (wb.monitoringSeries?.session_id === selected.value ? wb.monitoringSeries : null))
const sig = (hex: string): Signal | undefined => series.value?.signals.find((s) => s.hex === hex)
const markers = computed(() =>
  (series.value?.annotations ?? []).map((a) => ({ t: a.t, label: a.event_type ?? a.label, origin: a.origin ?? 'manual' })),
)
const bands = computed(() => [
  ...(m.value?.dpf.possible_regenerations ?? [])
    .filter((r) => r.t_start_rel_s !== undefined && r.t_end_rel_s !== undefined)
    .map((r) => ({ from: r.t_start_rel_s as number, to: r.t_end_rel_s as number, label: 'possible regeneration (inferred)' })),
  // air-path candidates: low / zero MAF while running (a single sample is widened to 1 s so it stays visible)
  ...(m.value?.air_path?.maf.episodes ?? []).map((e) => ({
    from: e.t_start_rel_s - 0.5, to: e.t_end_rel_s + 0.5, label: e.type === 'maf_zero_while_running' ? 'MAF 0 (candidate)' : 'MAF low (candidate)',
  })),
])
/** shared x range: every sample, failed read, event marker and inferred interval stays in view.
 * Iterative (no spreading of large arrays into Math.min/max): safe for very long recordings. */
const domain = computed<[number, number] | null>(() => {
  const sigs = series.value?.signals ?? []
  function* times() {
    yield* coords(sigs.map((s) => s.points), 0)
    for (const s of sigs) for (const f of s.failed) yield f[0]
    for (const mk of markers.value) if (mk.t !== null) yield mk.t
    for (const b of bands.value) { yield b.from; yield b.to }
  }
  return timeDomain(times())
})
const CHARTS = [
  { hex: '0D', title: 'Vehicle speed' },
  { hex: '0C', title: 'Engine speed' },
  { hex: '05', title: 'Coolant temperature' },
  { hex: '2C', title: 'Commanded EGR' },
  { hex: '2D', title: 'EGR error' },
  { hex: '10', title: 'MAF' },
  { hex: '0B', title: 'Intake manifold pressure (MAP)' },
]
const wanted = computed(() => m.value?.dpf.availability.find((g) => g.group === 'wanted')?.items ?? [])
const standard = computed(() => m.value?.dpf.availability.find((g) => g.group === 'standard_pids')?.items ?? [])
const t = (x: number | null | undefined) => (x === null || x === undefined ? 'n/a' : `${Math.round(x)} s`)
const when = (ts: number) => new Date(ts * 1000).toLocaleString()
const driveLabel = computed({
  get: () => m.value?.drive.drive_type_user_label ?? '',
  set: (v: string) => void wb.setDriveType(selected.value, v || null),
})
const sources = computed<[string, VoltageSource][]>(() => (m.value ? [['ECU', m.value.voltage.ecu], ['Adapter', m.value.voltage.adapter]] : []))
</script>

<template>
  <div class="grid gap-3" data-test="monitoring-session">
    <section class="panel text-sm">
      <h2 class="h2">Session details</h2>
      <div class="flex flex-wrap items-center gap-2">
        <select v-model="selected" class="field" data-test="session-select">
          <option v-for="s in candidates" :key="s.id" :value="s.id">
            {{ when(s.started_at) }} · {{ s.label || s.id }}{{ s.drive_type ? ` · ${s.drive_type}` : '' }}{{ s.synthetic ? ' · SYNTHETIC' : '' }}
          </option>
        </select>
        <template v-if="m">
          <label class="text-sm">Drive type
            <select v-model="driveLabel" class="field" data-test="session-drive-type">
              <option value="">not set</option><option value="city">city</option><option value="highway">highway</option>
              <option value="mixed">mixed</option><option value="other">other</option>
            </select>
          </label>
          <a class="btn" :href="`/api/sessions/${m.session_id}/export`" data-test="monitoring-export">Export ZIP</a>
          <span v-if="m.synthetic" class="badge text-warn">SYNTHETIC: not real vehicle data</span>
        </template>
      </div>
      <p v-if="!candidates.length" class="mt-2 text-ink-2">No monitoring sessions for this vehicle yet.</p>
      <DataLegend class="mt-2" />
    </section>

    <template v-if="m">
      <section class="grid gap-2" data-test="combined-charts">
        <p class="text-xs text-ink-2">
          Shared time axis (seconds from the first sample). Dashed lines: manual markers; solid blue: observed events; shaded: inferred
          possible regeneration and air-path candidates (MAF low / 0 while running). Gaps are left empty.
        </p>
        <template v-for="c in CHARTS" :key="c.hex">
          <LineChart
            v-if="sig(c.hex)" :title="c.title" :unit="sig(c.hex)!.unit" :domain="domain" :height="110" :failed="sig(c.hex)!.failed"
            :series="[{ label: c.title, color: '#2a78d6', points: sig(c.hex)!.points }]" :markers="markers" :bands="bands"
          />
        </template>
        <LineChart
          title="Voltage: ECU (PID 42) vs adapter (ATRV)" unit="V" :domain="domain" :height="110" :markers="markers"
          :series="[{ label: 'ECU', color: '#2a78d6', points: sig('42')?.points ?? [] }, { label: 'adapter', color: '#eb6834', points: sig('ATRV')?.points ?? [] }]"
        />
      </section>

      <section class="panel text-sm" data-test="session-events">
        <h2 class="h2">Timeline events</h2>
        <table class="w-full text-left">
          <thead><tr class="label"><th>Time</th><th>Event</th><th>Origin</th><th>Note</th></tr></thead>
          <tbody>
            <tr v-for="(e, i) in m.events" :key="i" class="border-t border-line">
              <td>{{ t(e.t_rel_s) }}</td><td>{{ e.event_type ?? e.label }}</td><td><DataBadge :kind="e.origin" /></td><td class="text-ink-2">{{ e.note }}</td>
            </tr>
            <tr v-for="(d, i) in m.detected_events" :key="`d${i}`" class="border-t border-line" data-test="detected-row">
              <td>{{ t(d.t_start_rel_s ?? d.t_rel_s) }}</td><td>{{ d.type }} ({{ d.confidence }} confidence)</td><td><DataBadge :kind="d.status === 'candidate' ? 'candidate' : 'inferred'" /></td>
              <td class="text-ink-2">{{ d.text }}</td>
            </tr>
          </tbody>
        </table>
        <div class="mt-3"><EventMarkerBar :session-id="m.session_id" context="Add an event to this session now (stamped with the current time)." /></div>
      </section>

      <section class="grid gap-3 md:grid-cols-2">
        <div class="panel text-sm" data-test="shutdown-fan">
          <h2 class="h2">Engine stops and fan</h2>
          <ul>
            <li v-for="(s, i) in m.shutdowns" :key="i">Stop at {{ t(s.t_rel_s) }} <DataBadge :kind="s.origin" /> <span class="text-ink-2">{{ s.basis }}</span></li>
            <li v-if="!m.shutdowns.length" class="text-ink-2">No engine stop recorded.</li>
          </ul>
          <table class="mt-2 w-full text-left">
            <thead><tr class="label"><th>Fan from</th><th>Duration</th><th>After stop</th><th /></tr></thead>
            <tbody>
              <tr v-for="(f, i) in m.fan.runs" :key="i" class="border-t border-line" data-test="fan-row">
                <td>{{ t(f.t_start_rel_s) }}</td>
                <td>{{ f.duration_s !== null ? `${Math.round(f.duration_s)} s` : 'fan_stopped not marked' }}</td>
                <td>{{ f.after_shutdown ? `${Math.round(f.seconds_after_shutdown ?? 0)} s` : 'no stop before' }}</td>
                <td><DataBadge kind="manual" /><span v-if="f.marked_unusually_loud" class="badge text-warn">loud</span></td>
              </tr>
            </tbody>
          </table>
          <p class="mt-1 text-xs text-ink-2">{{ m.fan.source }}</p>
          <p v-if="m.observed_capabilities" class="mt-1 text-xs text-ink-2" data-test="observed-capabilities">
            Tested in this session: ECU answered {{ m.observed_capabilities.ecu_answers_after_engine_stop }} request(s) for
            {{ m.observed_capabilities.ecu_answered_for_s_after_engine_stop }} s after an engine stop; adapter voltage read
            {{ m.observed_capabilities.adapter_voltage_reads_while_ecu_unresponsive }} time(s) while the ECU was silent.
          </p>
        </div>

        <div class="panel text-sm" data-test="voltage-panel">
          <h2 class="h2">Voltage</h2>
          <div v-for="[name, v] in sources" :key="name" class="mb-2">
            <div class="label">{{ name }} <span class="normal-case">({{ v.provenance }})</span></div>
            <template v-if="v.status === 'measured'">
              <div>Start {{ v.at_start?.value }} V · running median {{ v.engine_running?.median ?? 'n/a' }} V (min {{ v.engine_running?.min ?? 'n/a' }})
                · engine off median {{ v.engine_off?.median ?? 'n/a' }} V</div>
              <div v-for="(s, i) in v.shutdowns ?? []" :key="i" class="text-xs">
                Stop at {{ t(s.t_rel_s) }}: last before {{ s.last_before?.value ?? 'n/a' }} V; after
                {{ s.after ? `${s.after.min}-${s.after.max} V (${s.after.n} reads)` : 'not readable' }}
                <template v-if="s.trend_v_per_h !== null">, trend {{ s.trend_v_per_h }} V/h</template>
              </div>
              <div v-if="v.low_while_running?.samples" class="text-xs text-warn">
                {{ v.low_while_running.samples }} running sample(s) below {{ v.low_while_running.screen_v }} V (screening value)
              </div>
              <div v-if="(v.drops ?? []).some((d) => d.context === null)" class="text-xs text-warn">
                {{ (v.drops ?? []).filter((d) => d.context === null).length }} large drop(s) outside engine start/stop
              </div>
            </template>
            <DataBadge v-else kind="not_measured" />
          </div>
          <ul>
            <li v-for="(w, i) in m.voltage.battery_warnings" :key="i" data-test="battery-warning">
              {{ w.event }} at {{ t(w.t_rel_s) }} <DataBadge :kind="w.origin" />
              <span class="text-ink-2"> nearby: ECU {{ w.nearby.ecu_voltage ? `${w.nearby.ecu_voltage.min}-${w.nearby.ecu_voltage.max} V` : 'n/a' }},
                adapter {{ w.nearby.adapter_voltage ? `${w.nearby.adapter_voltage.min}-${w.nearby.adapter_voltage.max} V` : 'n/a' }}</span>
            </li>
          </ul>
          <p class="mt-1 text-xs text-ink-2">{{ m.voltage.caveat }}</p>
        </div>
      </section>

      <AirPathPanel v-if="m.air_path" :ap="m.air_path" />

      <section class="panel text-sm" data-test="dpf-panel">
        <h2 class="h2">DPF observations</h2>
        <ul class="grid gap-1 md:grid-cols-2">
          <li v-for="i in wanted" :key="i.key" :data-test="`dpf-want-${i.key}`">{{ i.name }} <DataBadge :kind="i.status" /> <span class="text-xs text-ink-2">{{ i.via }}</span></li>
        </ul>
        <p class="mt-2 text-xs text-ink-2">
          Standard PIDs: <span v-for="i in standard" :key="i.hex" class="mr-2">{{ i.hex }} <DataBadge :kind="i.status" /></span>
        </p>
        <div class="mt-2">
          <div v-for="(r, i) in [...m.dpf.possible_regenerations, ...m.dpf.possible_interrupted_regenerations]" :key="i" class="mb-1" data-test="regen">
            <DataBadge kind="inferred" /> {{ r.text }}
          </div>
          <div v-for="(u, i) in m.dpf.user_suspected" :key="`u${i}`"><DataBadge kind="manual" /> suspected regeneration marked at {{ t(u.t_rel_s) }}</div>
          <p v-if="!m.dpf.possible_regenerations.length && !m.dpf.limitations.length" class="text-ink-2">No possible regeneration inferred.</p>
          <p v-for="l in m.dpf.limitations" :key="l" class="text-xs text-ink-2">{{ l }}</p>
          <p class="text-xs text-ink-2" data-test="confirmed-note">
            Confirmed regenerations: {{ m.dpf.confirmed_regenerations.length ? `${m.dpf.confirmed_regenerations.length}, confirmed by the DDE regeneration flag (see BMW DDE values)` : 'none (only a fresh, verified DDE flag can confirm one; generic OBD cannot)' }}.
          </p>
        </div>
      </section>

      <DdeMonitorPanel v-if="m.dde && m.dde.status !== 'not_recorded'" :summary="m.dde" :inferred-regenerations="m.dpf.possible_regenerations.length" />

      <section class="panel text-sm" data-test="coolant-panel">
        <h2 class="h2">Cooling</h2>
        <p v-if="m.coolant.status === 'measured'">
          First {{ m.coolant.first?.value }} °C · max while running {{ m.coolant.max_running }} °C · usual warm temperature for this car:
          {{ m.coolant.usual_warm_c !== null && m.coolant.usual_warm_c !== undefined ? `${m.coolant.usual_warm_c} °C (${m.coolant.usual_warm_sessions} session(s))` : 'not known yet' }}
        </p>
        <DataBadge v-else kind="not_measured" />
        <p v-for="(d, i) in m.coolant.detected" :key="i"><DataBadge kind="inferred" /> {{ d.text }}</p>
        <p v-for="l in m.coolant.limitations ?? []" :key="l" class="text-xs text-ink-2">{{ l }}</p>
      </section>

      <section class="panel text-sm" data-test="sampling-panel">
        <h2 class="h2">Actual sampling quality</h2>
        <p v-if="m.sampling.base_intervals">
          Planned every {{ m.sampling.base_intervals.fast }} / {{ m.sampling.base_intervals.medium }} / {{ m.sampling.base_intervals.slow }} s
          (fast / medium / slow);
          <template v-if="m.sampling.final_effective_intervals">at the end {{ m.sampling.final_effective_intervals.fast }} /
            {{ m.sampling.final_effective_intervals.medium }} / {{ m.sampling.final_effective_intervals.slow }} s;</template>
          {{ m.sampling.adjustments.length }} adjustment(s) to the adapter's throughput.
        </p>
        <table class="mt-2 w-full text-left">
          <thead><tr class="label"><th>Signal</th><th>Tier</th><th>Planned Hz</th><th>Actual Hz</th><th>OK / attempts</th><th>Max gap (s)</th></tr></thead>
          <tbody>
            <tr v-for="s in m.sampling.signals" :key="`${s.mode ?? 1}-${s.pid}`" class="border-t border-line">
              <td>{{ s.name }}</td><td>{{ s.tier ?? '-' }}</td><td>{{ s.target_hz_base ?? '-' }}</td><td>{{ s.actual_hz }}</td>
              <td>{{ s.ok }} / {{ s.attempts }}</td><td>{{ s.max_gap_s ?? 'n/a' }}</td>
            </tr>
          </tbody>
        </table>
        <ul class="mt-2 text-xs">
          <li v-for="(l, i) in m.link" :key="i">{{ t(l.t_rel_s) }}: {{ l.type }}<template v-if="l.gap_s"> (gap {{ l.gap_s }} s)</template></li>
        </ul>
        <p class="mt-1 text-xs text-ink-2">
          Signals not available in this session:
          <span v-for="s in m.signals.filter((x) => x.status !== 'measured' && x.status !== 'raw_only')" :key="s.hex" class="mr-2">{{ s.hex }} {{ s.name }}</span>
        </p>
      </section>
    </template>
  </div>
</template>
