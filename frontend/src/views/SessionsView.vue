<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import LineChart from '../components/LineChart.vue'
import ScanCard from '../components/ScanCard.vue'
import { useWorkbench } from '../stores/workbench'

const wb = useWorkbench()
const a = ref('')
const b = ref('')
const includeVin = ref(false)
const note = ref('')
const when = (t: number) => new Date(t * 1000).toLocaleString()
onMounted(() => Promise.all([wb.loadSessions(), wb.loadFixtures()]))

const overlay = computed(() => {
  const c = wb.comparison
  if (!c) return []
  return c.series_a.signals
    .map((sa) => ({ sa, sb: c.series_b.signals.find((s) => s.pid === sa.pid) }))
    .filter((p) => p.sb && p.sa.decoded && (p.sa.points.length || p.sb.points.length))
})
const onlyOne = computed(() => {
  const c = wb.comparison
  if (!c) return []
  const pa = new Set(c.series_a.signals.map((s) => s.pid))
  const pb = new Set(c.series_b.signals.map((s) => s.pid))
  return [
    ...c.series_a.signals.filter((s) => !pb.has(s.pid)).map((s) => `${s.name}: not measured in B`),
    ...c.series_b.signals.filter((s) => !pa.has(s.pid)).map((s) => `${s.name}: not measured in A`),
  ]
})
async function addNote() {
  if (wb.current && note.value.trim()) {
    await wb.annotate(wb.current.id, note.value.trim())
    note.value = ''
    await wb.openSession(wb.current.id)
  }
}
</script>

<template>
  <div class="grid gap-3">
    <section class="panel">
      <h2 class="h2">Sessions for {{ wb.profile?.name }}</h2>
      <table v-if="wb.sessions.length" class="w-full text-left text-sm" data-test="session-table">
        <thead><tr class="label"><th>Started</th><th>Kind</th><th>Source</th><th>Label</th><th>A</th><th>B</th><th /></tr></thead>
        <tbody>
          <tr v-for="s in wb.sessions" :key="s.id" class="border-t border-line">
            <td>{{ when(s.started_at) }}</td><td>{{ s.kind }}</td>
            <td>{{ s.source }} <span v-if="s.synthetic" class="badge text-warn">SYNTHETIC</span><span v-if="s.aborted" class="badge text-crit">ended early</span></td>
            <td>{{ s.label }}</td>
            <td><input v-model="a" type="radio" :value="s.id" :data-test="`a-${s.id}`" /></td>
            <td><input v-model="b" type="radio" :value="s.id" :data-test="`b-${s.id}`" /></td>
            <td><button class="btn" :data-test="`open-${s.id}`" @click="wb.openSession(s.id)">Open</button></td>
          </tr>
        </tbody>
      </table>
      <p v-else class="text-sm text-ink-2">No sessions stored for this vehicle yet.</p>
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <button class="btn-primary" data-test="compare" :disabled="!a || !b || a === b" @click="wb.compare(a, b)">Compare A with B</button>
        <span class="label ml-4">Replay a fixture</span>
        <button v-for="f in wb.fixtures" :key="f.name" class="btn" :data-test="`replay-${f.name}`" @click="wb.replay(f.name)">{{ f.name.replace('.jsonl', '') }}</button>
      </div>
      <p class="mt-1 text-xs text-ink-2">Fixtures are synthetic simulator transcripts. They are not measurements from a real vehicle.</p>
    </section>

    <section v-if="wb.comparison" class="grid gap-3" data-test="comparison">
      <div class="panel text-sm">
        <h2 class="h2">Compare: A {{ wb.comparison.a.id }} · B {{ wb.comparison.b.id }}</h2>
        <p v-if="wb.comparison.mixed_synthetic_and_real" class="text-warn">Warning: one session is synthetic and the other is real. Do not draw conclusions from this comparison.</p>
        <p v-if="!wb.comparison.same_vehicle_profile" class="text-warn">These sessions belong to different vehicle profiles.</p>
        <div class="grid gap-3 md:grid-cols-3" data-test="dtc-diff">
          <div><div class="label">Only in A</div><div>{{ wb.comparison.dtc_diff.only_a.join(', ') || 'none' }}</div></div>
          <div><div class="label">Only in B</div><div>{{ wb.comparison.dtc_diff.only_b.join(', ') || 'none' }}</div></div>
          <div><div class="label">In both</div><div>{{ wb.comparison.dtc_diff.both.join(', ') || 'none' }}</div></div>
        </div>
        <p class="mt-2 text-xs text-ink-2">Signals are overlaid on a shared timebase relative to each session's first sample. The two sessions were not recorded at the same time.</p>
        <ul v-if="onlyOne.length" class="mt-1 list-disc pl-5 text-xs text-ink-2"><li v-for="t in onlyOne" :key="t">{{ t }}</li></ul>
      </div>
      <LineChart
        v-for="p in overlay" :key="p.sa.pid" :title="p.sa.name" :unit="p.sa.unit"
        :series="[{ label: 'A', color: '#2a78d6', points: p.sa.points }, { label: 'B', color: '#eb6834', points: p.sb!.points }]"
      />
    </section>

    <section v-if="wb.current" class="grid gap-3" data-test="session-detail">
      <div class="panel text-sm">
        <h2 class="h2">
          Session {{ wb.current.id }}
          <span v-if="wb.current.synthetic" class="badge text-warn" data-test="detail-synthetic">SYNTHETIC: not real vehicle data</span>
        </h2>
        <p>{{ wb.current.kind }} · {{ wb.current.source }} · {{ when(wb.current.started_at) }} · {{ wb.current.label || 'no label' }}</p>
        <p v-if="wb.current.meta.aborted" class="text-crit">Ended early: {{ wb.current.meta.aborted }}</p>
        <div class="mt-2 flex flex-wrap items-center gap-3">
          <a class="btn-primary" data-test="export" :href="`/api/sessions/${wb.current.id}/export?include_vin=${includeVin}`">Export ZIP (JSON, CSV, raw trace, report)</a>
          <label><input v-model="includeVin" type="checkbox" data-test="include-vin" /> Include VIN in the export (default: redacted)</label>
          <button class="btn" data-test="analyse" @click="wb.loadFindings(wb.current.id)">Analyse</button>
        </div>
        <div class="mt-2 flex gap-2">
          <input v-model="note" class="field w-64" placeholder="add an observation to this session" />
          <button class="btn" @click="addNote">Add marker</button>
        </div>
        <ul v-if="wb.current.annotations.length" class="mt-2 list-disc pl-5">
          <li v-for="(m, i) in wb.current.annotations" :key="i">{{ when(m.t_wall) }}: {{ m.label }} {{ m.note }}</li>
        </ul>
      </div>
      <div v-if="wb.current.meta.metrics?.length" class="panel text-sm" data-test="metrics">
        <h2 class="h2">Sampling quality</h2>
        <table class="w-full text-left">
          <thead><tr class="label"><th>Signal</th><th>Actual Hz</th><th>OK / attempts</th><th>Dropped</th><th>Error rate</th><th>Latency mean / p95 (ms)</th><th>Max gap (s)</th></tr></thead>
          <tbody>
            <tr v-for="m in wb.current.meta.metrics" :key="m.pid" class="border-t border-line">
              <td>{{ m.name }}</td><td>{{ m.actual_hz }}</td><td>{{ m.ok }} / {{ m.attempts }}</td><td>{{ m.dropped }}</td>
              <td>{{ (m.error_rate * 100).toFixed(1) }} %</td><td>{{ m.latency_ms_mean ?? 'n/a' }} / {{ m.latency_ms_p95 ?? 'n/a' }}</td><td>{{ m.max_gap_s ?? 'n/a' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <ScanCard v-for="(sc, i) in wb.current.scans" :key="i" :scan="sc" />
      <template v-if="wb.currentSeries">
        <template v-for="s in wb.currentSeries.signals" :key="s.pid">
          <LineChart
            v-if="s.decoded" :title="s.name" :unit="s.unit" :failed="s.failed"
            :series="[{ label: s.name, color: '#2a78d6', points: s.points }]" :markers="wb.currentSeries.annotations"
          />
          <p v-else-if="s.pid !== 1" class="panel text-sm text-ink-2">{{ s.name }} ({{ s.hex }}): supported by the ECU, raw bytes stored, no verified decoder, so no value is shown.</p>
        </template>
      </template>
    </section>
  </div>
</template>
