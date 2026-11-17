<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Point } from '../types'
import { coords, drawSegments, extent, nearest, niceTicks, timeDomain } from './chart'

export interface ChartSeries { label: string; color: string; points: Point[] }
const props = withDefaults(
  defineProps<{
    title: string
    unit?: string | null
    series: ChartSeries[]
    failed?: [number, string][]
    markers?: { t: number | null; label: string; origin?: string }[]
    /** shaded time ranges, e.g. an inferred 'possible regeneration' (drawn hatched, never as a value) */
    bands?: { from: number; to: number; label: string }[]
    /** shared x range (seconds) so stacked charts line up */
    domain?: [number, number] | null
    height?: number
  }>(),
  { unit: '', failed: () => [], markers: () => [], bands: () => [], domain: null, height: 150 },
)

const W = 720
const M = { l: 52, r: 12, t: 8, b: 22 }
const hoverT = ref<number | null>(null)

// Domains are computed iteratively over the ORIGINAL points (no array spreading: safe for
// hundreds of thousands of samples). Only the drawn path is decimated, never the data.
const empty = computed(() => props.series.every((s) => s.points.length === 0))
const xDomain = computed<[number, number]>(() => {
  if (props.domain) return props.domain
  function* times() {
    yield* coords(props.series.map((s) => s.points), 0)
    for (const f of props.failed) yield f[0]
  }
  return timeDomain(times()) ?? [0, 1]
})
const yDomain = computed<[number, number]>(() => {
  const e = extent(coords(props.series.map((s) => s.points), 1))
  if (!e) return [0, 1]
  const [lo, hi] = e
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.08
  return [lo - pad, hi + pad]
})
/** per-series min / max / last of the original data (data table) */
const stats = computed(() =>
  props.series
    .filter((s) => s.points.length)
    .map((s) => ({ label: s.label, n: s.points.length, range: extent(coords([s.points], 1)), last: s.points[s.points.length - 1][1] })),
)
const x = (t: number) => M.l + ((t - xDomain.value[0]) / (xDomain.value[1] - xDomain.value[0])) * (W - M.l - M.r)
const y = (v: number) => props.height - M.b - ((v - yDomain.value[0]) / (yDomain.value[1] - yDomain.value[0])) * (props.height - M.t - M.b)
const yTicks = computed(() => niceTicks(yDomain.value[0], yDomain.value[1], 3))
const xTicks = computed(() => niceTicks(xDomain.value[0], xDomain.value[1], 6))
/** drawn points per series: at most ~DRAW_BUDGET x 4 (deterministic min/max decimation, gaps kept) */
const DRAW_BUDGET = 1500
const paths = computed(() =>
  props.series.map((s) => {
    const segs = drawSegments(s.points, DRAW_BUDGET)
    return {
      ...s,
      d: segs.map((seg) => seg.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('')).join(''),
      dots: segs.filter((seg) => seg.length === 1).map((seg) => seg[0]),
      drawn: segs.reduce((n, seg) => n + seg.length, 0),
    }
  }),
)
const hover = computed(() => {
  if (hoverT.value === null || empty.value) return null
  const rows = props.series
    .map((s) => ({ s, p: nearest(s.points, hoverT.value as number) }))
    .filter((r): r is { s: ChartSeries; p: Point } => r.p !== null)
  return rows.length ? { t: rows[0].p[0], rows } : null
})
function onMove(e: MouseEvent) {
  const box = (e.currentTarget as SVGElement).getBoundingClientRect()
  const px = ((e.clientX - box.left) / (box.width || W)) * W
  hoverT.value = xDomain.value[0] + ((px - M.l) / (W - M.l - M.r)) * (xDomain.value[1] - xDomain.value[0])
}
const fmt = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) ? 'n/a' : (Math.abs(v) >= 1000 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2))
</script>

<template>
  <figure class="panel" data-test="chart">
    <figcaption class="mb-1 flex flex-wrap items-baseline justify-between gap-2">
      <span class="text-sm font-semibold">{{ title }} <span v-if="unit" class="font-normal text-ink-2">({{ unit }})</span></span>
      <span v-if="series.length > 1" class="flex gap-3 text-xs text-ink-2" data-test="legend">
        <span v-for="s in series" :key="s.label" class="inline-flex items-center gap-1">
          <span class="inline-block h-0.5 w-4" :style="{ background: s.color }" />{{ s.label }}
        </span>
      </span>
    </figcaption>
    <p v-if="empty" class="py-6 text-center text-sm text-ink-2" data-test="chart-empty">Not measured: no samples for this signal.</p>
    <svg v-else :viewBox="`0 0 ${W} ${height}`" class="w-full" role="img" :aria-label="`${title} over time`" @mousemove="onMove" @mouseleave="hoverT = null">
      <g v-for="t in yTicks" :key="`y${t}`">
        <line :x1="M.l" :x2="W - M.r" :y1="y(t)" :y2="y(t)" stroke="#e4e3df" stroke-width="1" />
        <text :x="M.l - 6" :y="y(t) + 3" text-anchor="end" font-size="10" fill="#52514e">{{ fmt(t) }}</text>
      </g>
      <text v-for="t in xTicks" :key="`x${t}`" :x="x(t)" :y="height - 6" text-anchor="middle" font-size="10" fill="#52514e">{{ t }} s</text>
      <g v-for="(b, i) in bands" :key="`b${i}`" data-test="band">
        <rect :x="x(b.from)" :y="M.t" :width="Math.max(1, x(b.to) - x(b.from))" :height="height - M.t - M.b" fill="#b97700" opacity="0.12" />
        <text :x="x(b.from) + 3" :y="height - M.b - 4" font-size="9" fill="#b97700">{{ b.label }}</text>
      </g>
      <g v-for="(m, i) in markers.filter((mk) => mk.t !== null)" :key="`m${i}`" data-test="marker" :data-origin="m.origin ?? 'manual'">
        <line
          :x1="x(m.t as number)" :x2="x(m.t as number)" :y1="M.t" :y2="height - M.b" stroke-width="1"
          :stroke="m.origin === 'observed' ? '#2a78d6' : '#52514e'" :stroke-dasharray="m.origin === 'observed' ? '' : '3 3'"
        />
        <text
          :x="x(m.t as number) + (x(m.t as number) > W * 0.75 ? -3 : 3)" :y="M.t + 9" font-size="9" fill="#52514e"
          :text-anchor="x(m.t as number) > W * 0.75 ? 'end' : 'start'"
        >{{ m.label }}</text>
      </g>
      <g v-for="p in paths" :key="p.label">
        <path :d="p.d" fill="none" :stroke="p.color" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" data-test="line" :data-drawn="p.drawn" :data-samples="p.points.length" />
        <circle v-for="(d, i) in p.dots" :key="i" :cx="x(d[0])" :cy="y(d[1])" r="2.5" :fill="p.color" />
      </g>
      <g data-test="failed">
        <rect v-for="(f, i) in failed" :key="`f${i}`" :x="x(f[0]) - 1" :y="height - M.b - 6" width="2" height="6" fill="#c62f2e">
          <title>{{ f[1] }} at {{ f[0] }} s - no value</title>
        </rect>
      </g>
      <g v-if="hover">
        <line :x1="x(hover.t)" :x2="x(hover.t)" :y1="M.t" :y2="height - M.b" stroke="#0b0b0b" stroke-width="1" opacity="0.35" />
        <circle v-for="r in hover.rows" :key="r.s.label" :cx="x(r.p[0])" :cy="y(r.p[1])" r="4" :fill="r.s.color" stroke="#fcfcfb" stroke-width="2" />
      </g>
    </svg>
    <div class="flex min-h-5 flex-wrap gap-x-4 text-xs text-ink-2" data-test="tooltip">
      <template v-if="hover">
        <span v-for="r in hover.rows" :key="r.s.label">
          <span class="inline-block h-2 w-2 rounded-full" :style="{ background: r.s.color }" /> {{ r.s.label }}:
          <b class="text-ink">{{ fmt(r.p[1]) }} {{ unit }}</b> at {{ r.p[0].toFixed(1) }} s
        </span>
      </template>
      <span v-else-if="failed.length" class="text-crit">{{ failed.length }} failed read(s) shown as red ticks; gaps are left empty.</span>
    </div>
    <details v-if="!empty" class="text-xs text-ink-2">
      <summary class="cursor-pointer">Data table</summary>
      <table class="mt-1 w-full text-left">
        <thead><tr><th>Series</th><th>Samples</th><th>Min</th><th>Max</th><th>Last</th></tr></thead>
        <tbody>
          <tr v-for="s in stats" :key="s.label" data-test="stats-row">
            <td>{{ s.label }}</td><td>{{ s.n }}</td>
            <td>{{ fmt(s.range?.[0]) }}</td><td>{{ fmt(s.range?.[1]) }}</td>
            <td>{{ fmt(s.last) }}</td>
          </tr>
        </tbody>
      </table>
    </details>
  </figure>
</template>
