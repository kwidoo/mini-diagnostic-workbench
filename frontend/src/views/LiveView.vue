<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import LineChart from '../components/LineChart.vue'
import EventMarkerBar from '../components/monitoring/EventMarkerBar.vue'
import { observedHz, staleAfterMs, useWorkbench } from '../stores/workbench'
import type { PidRef } from '../types'

const wb = useWorkbench()
const profileId = ref('idle')
const duration = ref(120)
const label = ref('')
const custom = ref('')
const untilStop = ref(false)
const maxDuration = ref<number | null>(null)
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined

onMounted(() => {
  timer = setInterval(() => (now.value = Date.now()), 500)
  if (wb.connected) void wb.loadPlan(profileId.value)
})
onUnmounted(() => clearInterval(timer))
watch([profileId, () => wb.connected, () => wb.activeProfileId], () => wb.connected && wb.loadPlan(profileId.value))

const planned = computed<PidRef[]>(() => (wb.plan ? [...wb.plan.fast, ...(wb.plan.medium ?? []), ...wb.plan.slow] : []))
const headline = computed(() => planned.value.find((p) => p.pid === 0x0c) ?? null)
const others = computed(() => planned.value.filter((p) => p.pid !== 0x0c && p.pid !== 0x01))

function tile(p: PidRef) {
  const sig = wb.live[p.pid]
  if (!sig) return { text: 'not measured', sub: wb.recording ? 'waiting for first sample' : 'no recording running', ok: false, stale: false, hz: 0 }
  const stale = now.value - sig.at > staleAfterMs(wb.monitor, p.pid)
  if (sig.value === null) return { text: 'not measured', sub: `last read failed (${sig.quality})`, ok: false, stale, hz: observedHz(sig) }
  return { text: String(Math.round(sig.value * 100) / 100), sub: `${sig.ok}/${sig.attempts} reads ok`, ok: true, stale, hz: observedHz(sig) }
}
const remaining = computed(() => {
  const r = wb.recording
  return r && r.duration_s !== null ? Math.max(0, Math.round(r.started_at + r.duration_s - now.value / 1000)) : 0
})
const elapsed = computed(() => (wb.recording ? Math.max(0, Math.round(now.value / 1000 - wb.recording.started_at)) : 0))
const stopText = computed(() => {
  const r = wb.recording
  if (!r) return ''
  if (r.until_stop) return `Stop (running ${elapsed.value} s${r.duration_s !== null ? `, limit ${Math.round(r.duration_s)} s` : ''})`
  return `Stop (${remaining.value} s left)`
})
function start() {
  void wb.startRecording(duration.value, profileId.value, label.value, untilStop.value ? { untilStop: true, maxDurationS: maxDuration.value } : {})
}
function mark(text: string) {
  const sid = wb.recording?.session_id
  if (sid && text.trim()) void wb.annotate(sid, text.trim())
}
</script>

<template>
  <div class="grid gap-3">
    <section class="panel">
      <h2 class="h2">Recording</h2>
      <p v-if="!wb.connected" class="text-sm text-ink-2" data-test="live-disconnected">Disconnected. Connect to an adapter or the simulator to record live data.</p>
      <div v-else class="flex flex-wrap items-end gap-2">
        <label class="text-sm"><span class="label block">Signal set</span>
          <select v-model="profileId" class="field" :disabled="!!wb.recording">
            <option v-for="p in wb.profile?.recording_profiles ?? []" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </label>
        <label class="text-sm"><span class="label block">Duration (s)</span><input v-model.number="duration" type="number" min="5" max="3600" class="field w-24" :disabled="!!wb.recording || untilStop" /></label>
        <label class="flex items-center gap-1 text-sm"><input v-model="untilStop" type="checkbox" data-test="until-stop" :disabled="!!wb.recording" /> Until I press Stop</label>
        <label v-if="untilStop" class="text-sm"><span class="label block">Safety limit (s, optional)</span>
          <input v-model.number="maxDuration" type="number" min="60" max="86400" class="field w-28" data-test="max-duration" :disabled="!!wb.recording" placeholder="none" />
        </label>
        <label class="text-sm"><span class="label block">Label</span><input v-model="label" class="field w-48" placeholder="e.g. cold start" :disabled="!!wb.recording" /></label>
        <button v-if="!wb.recording" class="btn-primary" data-test="start" :disabled="!!wb.busy || !planned.length" @click="start">Start recording</button>
        <button v-else class="btn" data-test="stop" @click="wb.stopRecording()">{{ stopText }}</button>
      </div>
      <p v-if="wb.plan?.unsupported.length" class="mt-2 text-sm text-ink-2" data-test="plan-unsupported">
        Not supported by this ECU, so not recorded:
        <span v-for="p in wb.plan.unsupported" :key="p.pid" class="mr-2">{{ p.name }} ({{ p.hex }})</span>
      </p>
      <p class="mt-2 text-xs text-ink-2">
        Signals are polled one after another; each has its own timestamps and its own observed rate. Fault scans run before and after the recording.
      </p>
    </section>

    <section v-if="wb.connected && wb.plan" class="grid gap-3 md:grid-cols-4" data-test="tiles">
      <div v-if="headline" class="panel md:col-span-2" data-test="tile-rpm">
        <div class="label">{{ headline.name }}</div>
        <div class="text-5xl font-semibold tabular-nums" :class="tile(headline).ok ? '' : 'text-2xl text-ink-2'">
          {{ tile(headline).text }} <span v-if="tile(headline).ok" class="text-base font-normal text-ink-2">rpm</span>
        </div>
        <div class="text-xs text-ink-2">
          {{ tile(headline).sub }} · observed {{ tile(headline).hz.toFixed(1) }} Hz
          <span v-if="tile(headline).stale && wb.live[headline.pid]" class="badge text-warn" data-test="stale">stale</span>
        </div>
      </div>
      <div v-else class="panel md:col-span-2 text-sm text-ink-2" data-test="tile-rpm">Engine speed: not supported by this ECU.</div>
      <div v-for="p in others" :key="p.pid" class="panel" :data-test="`tile-${p.hex}`">
        <div class="label">{{ p.name }}</div>
        <div class="text-2xl font-semibold tabular-nums" :class="tile(p).ok ? '' : 'text-base text-ink-2'">
          {{ tile(p).text }} <span v-if="tile(p).ok" class="text-sm font-normal text-ink-2">{{ p.unit }}</span>
        </div>
        <div class="text-xs text-ink-2">
          {{ tile(p).sub }} · {{ tile(p).hz.toFixed(1) }} Hz
          <span v-if="tile(p).stale && wb.live[p.pid]" class="badge text-warn" data-test="stale">stale</span>
        </div>
      </div>
      <div v-for="p in wb.plan.unsupported" :key="`u${p.pid}`" class="panel" :data-test="`tile-${p.hex}`">
        <div class="label">{{ p.name }}</div>
        <div class="text-base text-ink-2">not supported</div>
        <div class="text-xs text-ink-2">this ECU does not offer it via generic OBD</div>
      </div>
    </section>

    <section v-if="wb.recording" class="panel" data-test="markers">
      <h2 class="h2">Event markers</h2>
      <div class="flex flex-wrap gap-2">
        <button v-for="l in wb.profile?.event_labels ?? []" :key="l" class="btn" @click="mark(l)">{{ l }}</button>
        <input v-model="custom" class="field" placeholder="custom marker" @keyup.enter="mark(custom); custom = ''" />
      </div>
    </section>

    <section v-if="wb.recording && wb.profile?.monitoring_events?.length" class="panel" data-test="monitoring-markers">
      <h2 class="h2">MINI events</h2>
      <EventMarkerBar :session-id="wb.recording.session_id" context="Fan / battery events can still be marked after the engine stops, as long as the recording runs." />
    </section>

    <section v-if="wb.plan" class="grid gap-3">
      <LineChart
        v-for="p in wb.plan.fast" :key="p.pid" :title="p.name" :unit="p.unit"
        :series="[{ label: p.name, color: '#2a78d6', points: wb.live[p.pid]?.points ?? [] }]" :markers="wb.liveMarkers"
      />
    </section>
  </div>
</template>
