<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ADAPTER_VOLTAGE_KEY, staleAfterMs, useWorkbench } from '../../stores/workbench'
import type { PidRef } from '../../types'
import LineChart from '../LineChart.vue'
import DataBadge from './DataBadge.vue'
import DdeMonitorPanel from './DdeMonitorPanel.vue'
import EventMarkerBar from './EventMarkerBar.vue'

const wb = useWorkbench()
const label = ref('')
const driveType = ref('')
const maxHours = ref<number | null>(null)
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined

const monitoringProfile = computed(() => wb.profile?.recording_profiles.find((p) => p.monitoring) ?? null)
onMounted(() => {
  timer = setInterval(() => (now.value = Date.now()), 500)
  void wb.loadDdeStatus()
  if (wb.connected && monitoringProfile.value) void wb.loadPlan(monitoringProfile.value.id)
})
onUnmounted(() => clearInterval(timer))
watch([() => wb.connected, () => wb.activeProfileId], () => wb.connected && monitoringProfile.value && wb.loadPlan(monitoringProfile.value.id))

const rec = computed(() => (wb.recording?.monitoring ? wb.recording : null))
const otherRecording = computed(() => !!wb.recording && !wb.recording.monitoring)
const mon = computed(() => wb.monitor)
const elapsed = computed(() => (rec.value ? Math.max(0, Math.round(now.value / 1000 - rec.value.started_at)) : 0))
const fmtDuration = (s: number) => `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`

/** where manual events go: the running recording, otherwise the latest monitoring session */
const lastMonitoringSession = computed(() => wb.sessions.find((s) => s.monitoring) ?? null)
const target = computed(() => rec.value?.session_id ?? lastMonitoringSession.value?.id ?? null)
const targetNote = computed(() => {
  if (rec.value) return `Events go to the running recording ${rec.value.session_id}.`
  if (lastMonitoringSession.value)
    return `No recording running: events go to the last monitoring session ${lastMonitoringSession.value.id}, stamped with the current time.`
  return 'Start a monitoring recording to mark events.'
})

const planned = computed<PidRef[]>(() => (wb.plan ? [...wb.plan.fast, ...(wb.plan.medium ?? []), ...wb.plan.slow] : []))
function availability(pid: number): 'polled' | 'not_available' | 'unknown' {
  if (!wb.plan) return 'unknown'
  if (pid === ADAPTER_VOLTAGE_KEY) return wb.plan.adapter_voltage === 'polled' ? 'polled' : 'not_available'
  if (planned.value.some((p) => p.pid === pid)) return 'polled'
  return wb.plan.unsupported.some((p) => p.pid === pid) ? 'not_available' : 'unknown'
}
const TILES = [
  { key: 0x0c, name: 'Engine speed', unit: 'rpm', provenance: 'ECU (PID 0C)' },
  { key: 0x0d, name: 'Vehicle speed', unit: 'km/h', provenance: 'ECU (PID 0D)' },
  { key: 0x05, name: 'Coolant temperature', unit: '°C', provenance: 'ECU (PID 05)' },
  { key: 0x42, name: 'ECU voltage', unit: 'V', provenance: 'ECU (PID 42 control module voltage)' },
  { key: ADAPTER_VOLTAGE_KEY, name: 'Adapter voltage', unit: 'V', provenance: 'adapter (ATRV, measured by the adapter)' },
]
function tile(key: number) {
  const avail = availability(key)
  if (avail === 'not_available') return { kind: 'not_available', text: 'not available', sub: key === ADAPTER_VOLTAGE_KEY ? wb.plan?.adapter_voltage ?? '' : 'this ECU does not offer it', stale: false }
  const sig = wb.live[key]
  if (!sig) return { kind: 'not_measured', text: 'not measured', sub: rec.value ? 'waiting for the first sample' : 'no recording running', stale: false }
  const stale = now.value - sig.at > staleAfterMs(mon.value, key)
  if (sig.value === null) return { kind: 'not_measured', text: 'not measured', sub: `last read failed (${sig.quality})`, stale }
  return { kind: 'measured', text: String(Math.round(sig.value * 100) / 100), sub: `${sig.ok}/${sig.attempts} reads ok`, stale }
}

const DPF_HEX = ['7A', '7B', '7C', '78', '79', '73', '8B', '86']
const dpf = computed(() => {
  if (!wb.plan) return []
  const polled = planned.value.filter((p) => DPF_HEX.includes(p.hex)).map((p) => ({ ...p, kind: 'raw_only' }))
  const missing = wb.plan.unsupported.filter((p) => DPF_HEX.includes(p.hex)).map((p) => ({ ...p, kind: 'not_available' }))
  return [...polled, ...missing]
})

const linkText = computed(() => {
  switch (mon.value?.state) {
    case 'running':
      return 'ECU answering'
    case 'ecu_unresponsive':
      return 'ECU not answering: vehicle polling paused (not proof that the ignition is off); adapter voltage only'
    case 'adapter_unreachable':
      return mon.value?.ecu_polling_paused
        ? 'Adapter unreachable while vehicle polling was paused: retrying the adapter only (no vehicle request); the gap is recorded'
        : 'Adapter unreachable: retrying with back-off; the gap is recorded'
    default:
      return 'n/a'
  }
})

function start() {
  if (!monitoringProfile.value) return
  void wb.startRecording(120, monitoringProfile.value.id, label.value, {
    untilStop: true,
    maxDurationS: maxHours.value ? Math.round(maxHours.value * 3600) : null,
    driveType: driveType.value || null,
  })
}
const chartPoints = (key: number) => wb.live[key]?.points ?? []
</script>

<template>
  <div class="grid gap-3" data-test="monitoring-live">
    <section class="panel">
      <h2 class="h2">Continuous monitoring</h2>
      <p class="mb-2 rounded-md border border-warn p-2 text-sm" data-test="driver-notice">
        <b>Passenger or stationary use only.</b> The driver must not operate this dashboard while driving. Once started, recording
        continues unattended until you press Stop; nothing needs to be touched during the drive.
      </p>
      <p v-if="!monitoringProfile" class="text-sm text-ink-2" data-test="monitoring-unsupported">
        The selected vehicle profile has no continuous-monitoring profile.
      </p>
      <p v-else-if="!wb.connected && !rec" class="text-sm text-ink-2" data-test="monitoring-disconnected">
        Disconnected. Connect to the adapter (or the simulator) on the Connection tab.
      </p>
      <div v-else-if="!rec" class="flex flex-wrap items-end gap-2">
        <label class="text-sm"><span class="label block">Drive type (your label)</span>
          <select v-model="driveType" class="field" data-test="drive-type">
            <option value="">not set</option><option value="city">city</option><option value="highway">highway</option>
            <option value="mixed">mixed</option><option value="other">other</option>
          </select>
        </label>
        <label class="text-sm"><span class="label block">Safety limit (h, optional)</span>
          <input v-model.number="maxHours" type="number" min="0.1" max="24" step="0.5" class="field w-24" data-test="max-hours" placeholder="none" />
        </label>
        <label class="text-sm"><span class="label block">Label</span><input v-model="label" class="field w-48" placeholder="e.g. school run" /></label>
        <button class="btn-primary" data-test="start-monitoring" :disabled="!!wb.busy || otherRecording || !wb.plan" @click="start">
          Start monitoring (until stopped)
        </button>
        <span v-if="otherRecording" class="text-xs text-ink-2">Another recording is running (Live tab).</span>
      </div>
      <div v-else class="flex flex-wrap items-center gap-3" data-test="monitoring-running">
        <span class="badge text-crit">recording</span>
        <span class="tabular-nums" data-test="elapsed">{{ fmtDuration(elapsed) }}</span>
        <span v-if="rec.duration_s" class="text-xs text-ink-2">safety limit {{ fmtDuration(Math.round(rec.duration_s)) }}</span>
        <button class="btn" data-test="stop-monitoring" @click="wb.stopRecording()">Stop and save</button>
      </div>
      <p v-if="monitoringProfile?.description" class="mt-2 text-xs text-ink-2">{{ monitoringProfile.description }}</p>
    </section>

    <section v-if="rec" class="panel text-sm" data-test="link-state">
      <div class="flex flex-wrap items-center gap-3">
        <span class="label">Adapter</span><span>{{ wb.connectionState }}</span>
        <span class="label">Link</span>
        <span :class="mon?.state === 'running' ? 'text-good' : 'text-warn'" data-test="link-text">{{ linkText }}</span>
        <span class="label">Engine</span><span data-test="engine-state">{{ mon?.engine ?? 'unknown' }} <DataBadge v-if="mon && mon.engine !== 'unknown'" kind="observed" /></span>
      </div>
      <p v-if="mon" class="mt-1 text-xs text-ink-2" data-test="intervals">
        Sampling every {{ mon.effective_intervals.fast }} s / {{ mon.effective_intervals.medium }} s / {{ mon.effective_intervals.slow }} s
        (fast / medium / slow; planned {{ mon.base_intervals.fast }} / {{ mon.base_intervals.medium }} / {{ mon.base_intervals.slow }} s)
        <template v-if="mon.capacity_rps">, adapter delivers ~{{ mon.capacity_rps }} requests/s</template>.
        <template v-if="mon.suspended.length"> Paused (keeps answering NO DATA): {{ mon.suspended.join(', ') }}.</template>
      </p>
      <div v-if="mon?.state === 'ecu_unresponsive'" class="mt-2 flex flex-wrap items-center gap-2" data-test="ecu-paused">
        <button class="btn" data-test="resume-ecu" @click="wb.resumeEcuPolling()">Check whether the ECU answers (one request)</button>
        <span class="text-xs text-ink-2">Use this after switching the ignition back on. Sleeping modules are not polled or woken otherwise.</span>
        <span v-if="mon?.resume_requires_reinit" class="text-xs text-warn" data-test="resume-reinit">
          The adapter connection was re-opened while polling was paused: checking re-initialises the adapter first (ATZ ... 0100).
        </span>
      </div>
      <div v-else-if="mon?.state === 'adapter_unreachable' && mon?.ecu_polling_paused" class="mt-2 text-xs text-warn" data-test="adapter-lost-paused">
        The adapter's Wi-Fi link is down. Vehicle polling stays paused and nothing is sent to the car; the recorder only tries to reach
        the adapter again. You can stop and save the recording at any time: everything captured so far is kept.
      </div>
    </section>

    <section v-if="monitoringProfile && (wb.connected || rec)" class="grid gap-3 md:grid-cols-5" data-test="monitoring-tiles">
      <div v-for="t in TILES" :key="t.key" class="panel" :data-test="`mtile-${t.key}`">
        <div class="label">{{ t.name }}</div>
        <div class="text-2xl font-semibold tabular-nums" :class="tile(t.key).kind === 'measured' ? '' : 'text-base text-ink-2'">
          {{ tile(t.key).text }} <span v-if="tile(t.key).kind === 'measured'" class="text-sm font-normal text-ink-2">{{ t.unit }}</span>
        </div>
        <div class="text-xs text-ink-2">{{ t.provenance }}</div>
        <div class="text-xs text-ink-2">
          {{ tile(t.key).sub }}
          <span v-if="tile(t.key).stale" class="badge text-warn" data-test="stale">stale</span>
        </div>
      </div>
    </section>

    <DdeMonitorPanel v-if="rec && wb.profile?.ecus.some((e) => e.id.startsWith('bmw-dde'))" :live="mon?.dde ?? null" :link-state="mon?.state ?? null" />

    <section v-if="monitoringProfile && wb.plan" class="panel text-sm" data-test="dpf-live">
      <h2 class="h2">DPF data availability</h2>
      <ul class="grid gap-1 md:grid-cols-2">
        <li v-for="p in dpf" :key="p.pid" :data-test="`dpf-${p.hex}`">{{ p.name }} ({{ p.hex }}) <DataBadge :kind="p.kind" /></li>
        <li>Soot load, regeneration state, regeneration history <DataBadge kind="not_available" /> <span class="text-xs text-ink-2">BMW DDE values; only with a verified DDE signal (see BMW DDE values)</span></li>
      </ul>
      <p class="mt-1 text-xs text-ink-2">
        Raw-only PIDs are stored as bytes and never shown as numbers. Possible regenerations are inferred afterwards (Session details) and are
        never confirmed.
      </p>
    </section>

    <section v-if="monitoringProfile" class="panel" data-test="monitoring-events">
      <h2 class="h2">Mark what you see or hear</h2>
      <EventMarkerBar :session-id="target" :context="targetNote" />
      <p class="mt-2 text-xs text-ink-2">
        Fan and battery-warning events can be marked after the engine is off: the session stays open until you press Stop, and a closed
        session still accepts events. The fan is never detected automatically.
      </p>
      <ul v-if="wb.liveEvents.length" class="mt-2 text-sm" data-test="live-events">
        <li v-for="(e, i) in wb.liveEvents.slice(0, 10)" :key="i">
          {{ new Date(e.at).toLocaleTimeString() }} · {{ e.label }} <DataBadge :kind="e.origin" /> <span class="text-ink-2">{{ e.note }}</span>
        </li>
      </ul>
    </section>

    <section v-if="rec" class="grid gap-3">
      <LineChart title="Engine speed" unit="rpm" :series="[{ label: 'RPM', color: '#2a78d6', points: chartPoints(0x0c) }]" :markers="wb.liveMarkers" :height="110" />
      <LineChart title="Coolant temperature" unit="°C" :series="[{ label: 'Coolant', color: '#2a78d6', points: chartPoints(0x05) }]" :markers="wb.liveMarkers" :height="110" />
      <LineChart
        title="Voltage: ECU (PID 42) vs adapter (ATRV)" unit="V" :markers="wb.liveMarkers" :height="110"
        :series="[{ label: 'ECU', color: '#2a78d6', points: chartPoints(0x42) }, { label: 'adapter', color: '#eb6834', points: chartPoints(ADAPTER_VOLTAGE_KEY) }]"
      />
    </section>
  </div>
</template>
