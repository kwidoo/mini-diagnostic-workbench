import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import { decimate, drawSegments, extent, nearest, segments, timeDomain } from '../components/chart'
import LineChart from '../components/LineChart.vue'
import MonitoringSession from '../components/monitoring/MonitoringSession.vue'
import { useWorkbench } from '../stores/workbench'
import type { Point } from '../types'
import { baseRoutes, connected, miniProfile, mockFetch } from './helpers'

/** Long recordings: hundreds of thousands of samples must chart without RangeError, with correct
 * domains / min / max, every annotation kept, and the original data untouched. SYNTHETIC data. */

const N = 200_000
/** 0.5 s sampling from t=10 s; a spike at i=123457 and a dip at i=7; a 120 s gap at i=150000 */
function longSeries(): Point[] {
  const pts: Point[] = []
  for (let i = 0; i < N; i++) {
    const t = 10 + i * 0.5 + (i >= 150_000 ? 120 : 0)
    let v = 800 + 50 * Math.sin(i / 500)
    if (i === 123_457) v = 4321
    if (i === 7) v = -12
    pts.push([t, v])
  }
  return pts
}
const T_LAST = 10 + (N - 1) * 0.5 + 120

describe('chart maths on 200k points', () => {
  it('computes extents and time domains iteratively (no RangeError)', () => {
    const pts = longSeries()
    expect(() => Math.min(...pts.map((p) => p[1]))).toThrow(RangeError) // what the old code did
    expect(extent(pts.map((p) => p[1]))).toEqual([-12, 4321])
    expect(timeDomain(pts.map((p) => p[0]))).toEqual([10, T_LAST])
    expect(extent([])).toBeNull()
    expect(timeDomain([])).toBeNull()
    expect(extent([NaN, Infinity, null, undefined, 3, -Infinity, 1])).toEqual([1, 3])
    expect(timeDomain([5])).toEqual([5, 6])
  })

  it('decimates deterministically, keeps extremes and never bridges the gap', () => {
    const pts = longSeries()
    const copy = pts.map((p) => [p[0], p[1]])
    const segs = drawSegments(pts, 1500)
    expect(segs.length).toBe(segments(pts).length)
    expect(segs.length).toBe(2) // the 120 s gap stays a gap
    const drawn = segs.flat()
    expect(drawn.length).toBeLessThanOrEqual(1500 * 4 + 8)
    expect(drawn.some((p) => p[1] === 4321)).toBe(true)
    expect(drawn.some((p) => p[1] === -12)).toBe(true)
    expect(drawn[0]).toEqual([10, 800])
    expect(drawn.at(-1)![0]).toBe(T_LAST)
    expect(drawSegments(pts, 1500)).toEqual(segs) // deterministic
    expect(pts).toEqual(copy) // input untouched
    expect(decimate(pts.slice(0, 100), 50)).toEqual(pts.slice(0, 100)) // small data drawn as is
  })

  it('finds the nearest point by binary search', () => {
    const pts = longSeries()
    expect(nearest(pts, 10.2)).toEqual([10, 800])
    expect(nearest(pts, 10 + 123_457 * 0.5)![1]).toBe(4321)
    expect(nearest(pts, 1e12)![0]).toBe(T_LAST)
    expect(nearest([], 1)).toBeNull()
  })
})

describe('LineChart with a long recording', () => {
  it('renders 200k points with correct domains, stats and markers, drawing a decimated path', () => {
    const pts = longSeries()
    const failed: [number, string][] = [[T_LAST + 30, 'timeout']]
    const markers = Array.from({ length: 40 }, (_, i) => ({ t: 10 + i * 2_500, label: `m${i}`, origin: i % 2 ? 'manual' : 'observed' }))
    const w = mount(LineChart, {
      props: { title: 'Engine speed', unit: 'rpm', series: [{ label: 'rpm', color: '#000', points: pts }], failed, markers,
        bands: [{ from: 50_000, to: 51_000, label: 'possible regeneration (inferred)' }] },
    })
    const path = w.get('[data-test=line]')
    expect(path.attributes('data-samples')).toBe(String(N))
    expect(Number(path.attributes('data-drawn'))).toBeLessThanOrEqual(6008)
    expect((path.attributes('d') ?? '').split('M').length - 1).toBe(2) // two segments: gap kept
    expect(w.findAll('[data-test=marker]').length).toBe(40)
    expect(w.findAll('[data-test=band]').length).toBe(1)
    expect(w.findAll('[data-test=failed] rect').length).toBe(1)
    const row = w.get('[data-test=stats-row]').findAll('td').map((c) => c.text())
    expect(row[1]).toBe(String(N))
    expect(row[2]).toBe('-12.0')
    expect(row[3]).toBe('4321')
    expect(pts.length).toBe(N) // props not mutated
    // the failed read after the last sample widens the time domain: its tick is inside the plot
    const tick = w.get('[data-test=failed] rect')
    expect(Number(tick.attributes('x'))).toBeLessThanOrEqual(720)
  })

  it('handles empty and invalid data', () => {
    const empty = mount(LineChart, { props: { title: 'x', series: [{ label: 'a', color: '#000', points: [] }] } })
    expect(empty.find('[data-test=chart-empty]').exists()).toBe(true)
    const bad = mount(LineChart, {
      props: { title: 'x', series: [{ label: 'a', color: '#000', points: [[0, 1], [1, NaN], [2, 3], [NaN, 5]] as Point[] }] },
    })
    const d = bad.get('[data-test=line]').attributes('d') ?? ''
    expect(d).not.toContain('NaN')
    const row = bad.get('[data-test=stats-row]').findAll('td').map((c) => c.text())
    expect(row[2]).toBe('1.00')
    expect(row[3]).toBe('5.00')
  })
})

describe('MonitoringSession with a long recording', () => {
  it('keeps every annotation and inferred interval inside the shared time axis', async () => {
    vi.unstubAllGlobals()
    const rpm = longSeries()
    const volts: Point[] = rpm.filter((_, i) => i % 20 === 0).map((p) => [p[0], 12 + (p[1] % 3) / 10])
    const sessions = [{ id: 'm1', kind: 'record', vehicle_profile: miniProfile.id, source: 'simulator', synthetic: true, label: 'long', started_at: 1, ended_at: 2, monitoring: true, drive_type: null }]
    // the fan_stopped marker was set after the recording's last sample; one marker before the first
    const annotations = [
      { t: 5, label: 'note', note: '', event_type: null, origin: 'manual', t_wall: 1, t_mono: 1 },
      { t: 60_000, label: 'engine_stopped', note: '', event_type: 'engine_stopped', origin: 'observed', t_wall: 1, t_mono: 1 },
      { t: T_LAST + 400, label: 'fan_stopped', note: '', event_type: 'fan_stopped', origin: 'manual', t_wall: 1, t_mono: 1 },
      { t: null, label: 'untimed', note: '', event_type: null, origin: 'manual', t_wall: 1, t_mono: null },
    ]
    const series = { session_id: 'm1', signals: [
      { pid: 12, hex: '0C', name: 'Engine speed', unit: 'rpm', decoded: true, provenance: 'ecu', points: rpm, failed: [] },
      { pid: 66, hex: '42', name: 'Control module voltage', unit: 'V', decoded: true, provenance: 'ecu', points: volts, failed: [] },
    ], annotations }
    const summary = {
      session_id: 'm1', is_monitoring_session: true, synthetic: true,
      drive: { running_s: 1, moving_share: 0, max_speed_kmh: 0, mean_speed_moving_kmh: null, drive_type_user_label: null },
      engine_runs: [], shutdowns: [], fan: { runs: [], fan_stopped_without_running: [], source: '' },
      voltage: { caveat: '', ecu: { status: 'not_measured', provenance: 'ECU' }, adapter: { status: 'not_measured', provenance: 'adapter' }, battery_warnings: [] },
      coolant: { status: 'not_measured', detected: [] },
      dpf: { availability: [], possible_regenerations: [{ type: 'possible_dpf_regeneration', origin: 'inferred', status: 'possible', confidence: 'low', text: 'x', t_start_rel_s: T_LAST + 100, t_end_rel_s: T_LAST + 200 }],
        possible_interrupted_regenerations: [], user_suspected: [], limitations: [], confirmed_regenerations: [] },
      detected_events: [], events: [], link: [], signals: [],
      sampling: { base_intervals: null, final_effective_intervals: null, adjustments: [], time_in_state_s: null, signals: [] },
      observed_capabilities: null,
    }
    mockFetch({ ...baseRoutes(connected), 'GET /api/sessions': sessions, 'GET /api/sessions/m1/monitoring': summary, 'GET /api/sessions/m1/samples': series })
    const pinia = createPinia()
    setActivePinia(pinia)
    const wb = useWorkbench()
    wb.status = connected
    wb.profiles = [miniProfile]
    wb.activeProfileId = miniProfile.id
    const w = mount(MonitoringSession, { global: { plugins: [pinia] } })
    await wb.loadSessions()
    await flushPromises()

    const charts = w.findAll('[data-test=chart]')
    expect(charts.length).toBeGreaterThan(0)
    const rpmChart = charts.find((c) => c.text().includes('Engine speed'))!
    const lines = rpmChart.findAll('[data-test=marker] line')
    expect(lines.length).toBe(3) // every timed annotation drawn (the untimed one has no position)
    for (const l of lines) {
      const x = Number(l.attributes('x1'))
      expect(x).toBeGreaterThanOrEqual(52 - 0.01) // M.l
      expect(x).toBeLessThanOrEqual(720 - 12 + 0.01) // W - M.r
    }
    const band = rpmChart.get('[data-test=band] rect')
    expect(Number(band.attributes('x')) + Number(band.attributes('width'))).toBeLessThanOrEqual(708.01)
    expect(rpmChart.get('[data-test=line]').attributes('data-samples')).toBe(String(N))
    expect(series.signals[0].points.length).toBe(N) // stored data not reduced
  })
})
