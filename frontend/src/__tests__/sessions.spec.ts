import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import FindingCard from '../components/FindingCard.vue'
import LineChart from '../components/LineChart.vue'
import ScanCard from '../components/ScanCard.vue'
import { segments } from '../components/chart'
import { useWorkbench } from '../stores/workbench'
import type { Finding, SessionDetail, Signal } from '../types'
import SessionsView from '../views/SessionsView.vue'
import { baseRoutes, miniProfile, mockFetch } from './helpers'

const sig = (pid: number, name: string, points: [number, number][], failed: [number, string][] = [], decoded = true): Signal =>
  ({ pid, hex: pid.toString(16).toUpperCase().padStart(2, '0'), name, unit: 'rpm', decoded, points, failed })
const session = (id: string, codes: string[]): SessionDetail => ({
  id, kind: 'record', vehicle_profile: miniProfile.id, source: 'replay', synthetic: true, label: id, started_at: 1700000000, ended_at: 1700000100,
  meta: { metrics: [{ pid: 12, name: 'Engine speed', attempts: 10, ok: 9, dropped: 1, error_rate: 0.1, actual_hz: 4.5, latency_ms_mean: 30, latency_ms_p95: 45, latency_ms_max: 50, max_gap_s: 0.6, gaps: 1 }] },
  scans: [{ t_wall: 1700000000, phase: 'pre', mil_on: false, dtc_count_reported: 0, freeze_frame: { status: 'none_stored', dtc: null, dtc_raw_hex: null, ecu: null, readings: [] },
    mode_status: { stored: 'supported', pending: 'supported', permanent: 'no_data' }, dtcs: codes.map((c) => ({ code: c, raw_hex: c.slice(1), kind: 'pending', ecu: '7E8' })) }],
  annotations: [],
})

async function setup(extra: Record<string, unknown> = {}) {
  vi.unstubAllGlobals()
  const list = [session('B', ['P0101']), session('A', [])].map(({ meta, scans, annotations, ...s }) => s)
  const calls = mockFetch({ ...baseRoutes(), 'GET /api/sessions': list, ...extra })
  const pinia = createPinia()
  setActivePinia(pinia)
  const wb = useWorkbench()
  wb.profiles = [miniProfile]
  wb.activeProfileId = miniProfile.id
  const w = mount(SessionsView, { global: { plugins: [pinia] } })
  await flushPromises()
  return { w, wb, calls }
}

describe('replay', () => {
  it('replays a fixture, opens it and labels it synthetic with quality metrics and absent data', async () => {
    const detail = session('R', ['P0101'])
    const { w, calls } = await setup({
      'POST /api/replay': detail,
      'GET /api/sessions/R': detail,
      'GET /api/sessions/R/samples': { session_id: 'R', annotations: [{ t: 2, label: 'vibration', note: '' }],
        signals: [sig(12, 'Engine speed', [[0, 800], [0.2, 805], [0.4, 790]], [[1.0, 'disconnected']]), sig(0x10, 'Mass air flow (MAF)', []), sig(0x7a, 'DPF bank 1', [], [], false)] },
    })
    await w.get('[data-test="replay-rough-idle.jsonl"]').trigger('click')
    await flushPromises()
    expect(calls.find((c) => c.method === 'POST')).toMatchObject({ url: '/api/replay', body: { fixture: 'rough-idle.jsonl' } })
    const d = w.get('[data-test=session-detail]')
    expect(d.get('[data-test=detail-synthetic]').text()).toContain('not real vehicle data')
    expect(d.get('[data-test=metrics]').text()).toContain('4.5')
    expect(d.get('[data-test=metrics]').text()).toContain('10.0 %')
    expect(d.get('[data-test=dtc-permanent]').text()).toContain('Not supported / not read (no_data)')
    expect(d.get('[data-test=dtc-pending]').text()).toContain('P0101')
    expect(d.get('[data-test=dtc-stored]').text()).toContain('None stored')
    expect(d.findAll('[data-test=chart]').length).toBe(2)
    expect(d.findAll('[data-test=chart-empty]').length).toBe(1) // MAF had no samples: "Not measured", not a flat zero line
    expect(d.text()).toContain('DPF bank 1 (7A): supported by the ECU, raw bytes stored, no verified decoder')
    expect(d.get('[data-test=export]').attributes('href')).toBe('/api/sessions/R/export?include_vin=false')
    await d.get('[data-test=include-vin]').setValue(true)
    expect(d.get('[data-test=export]').attributes('href')).toContain('include_vin=true')
  })
})

describe('session comparison', () => {
  it('shows DTC changes and overlays signals of both sessions on a shared relative timebase', async () => {
    const { w, calls } = await setup({
      'GET /api/compare': {
        a: session('A', []), b: session('B', ['P0101']), same_vehicle_profile: true, mixed_synthetic_and_real: false,
        dtc_diff: { only_a: [], only_b: ['P0101 (pending)'], both: [] },
        series_a: { session_id: 'A', annotations: [], signals: [sig(12, 'Engine speed', [[0, 800], [1, 801]]), sig(5, 'Coolant', [[0, 60]])] },
        series_b: { session_id: 'B', annotations: [], signals: [sig(12, 'Engine speed', [[0, 700], [1, 900]])] },
      },
    })
    expect(w.get('[data-test=compare]').attributes('disabled')).toBeDefined()
    await w.get('[data-test=a-A]').setValue(true)
    await w.get('[data-test=b-B]').setValue(true)
    await w.get('[data-test=compare]').trigger('click')
    await flushPromises()
    expect(calls.at(-1)?.url).toBe('/api/compare?a=A&b=B')
    const c = w.get('[data-test=comparison]')
    expect(c.get('[data-test=dtc-diff]').text()).toContain('Only in BP0101 (pending)')
    expect(c.get('[data-test=dtc-diff]').text()).toContain('Only in Anone')
    const charts = c.findAll('[data-test=chart]')
    expect(charts.length).toBe(1)
    expect(charts[0].get('[data-test=legend]').text().replace(/\s+/g, '')).toBe('AB')
    expect(charts[0].findAll('[data-test=line]').length).toBe(2)
    expect(c.text()).toContain('Coolant: not measured in B')
    expect(c.text()).toContain('not recorded at the same time')
  })
})

describe('chart and cards', () => {
  it('never draws a line across missing observations', () => {
    const pts: [number, number][] = [[0, 1], [0.2, 2], [0.4, 3], [5, 4], [5.2, 5]]
    expect(segments(pts).map((s) => s.length)).toEqual([3, 2])
    const w = mount(LineChart, { props: { title: 'RPM', unit: 'rpm', series: [{ label: 'RPM', color: '#2a78d6', points: pts }], failed: [[2, 'timeout'], [3, 'timeout']] } })
    expect(w.get('[data-test=line]').attributes('d')?.match(/M/g)?.length).toBe(2)
    expect(w.get('[data-test=failed]').findAll('rect').length).toBe(2)
    expect(w.find('[data-test=legend]').exists()).toBe(false) // single series: the title names it
    expect(w.get('[data-test=tooltip]').text()).toContain('2 failed read(s)')
  })

  it('finding card shows categorical evidence strength, both sides of the evidence and next steps', () => {
    const f: Finding = {
      id: 'x', title: 'Idle RPM stability', hypothesis: 'Engine speed is unstable.', explanation: 'Describes what was measured.', strength: 'suggestive',
      supporting: [{ text: '3 of 10 windows exceeded 100 rpm.', values: {} }], contradicting: [], caveats: ['Temporal association only.'],
      conditions: ['MAF (PID 10): not supported by this ECU via generic OBD.'], next_steps: ['Smoke test the intake.'],
    }
    const w = mount(FindingCard, { props: { finding: f } })
    expect(w.get('[data-test=strength]').text()).toBe('Evidence: suggestive')
    expect(w.text()).not.toMatch(/\d+\s?% (likely|probab)/)
    expect(w.text()).toContain('None recorded.')
    expect(w.text()).toContain('not supported by this ECU')
    expect(w.text()).toContain('Smoke test the intake.')
  })

  it('scan card keeps raw DTC bytes and unknown freeze-frame state explicit', () => {
    const s = session('A', ['P2453']).scans[0]
    s.mil_on = null
    s.freeze_frame = { status: 'no_data', dtc: null, dtc_raw_hex: null, ecu: null, readings: [] }
    const w = mount(ScanCard, { props: { scan: s } })
    expect(w.text()).toContain('raw 2453')
    expect(w.text()).toContain('MIL: not reported')
    expect(w.get('[data-test=freeze]').text()).toContain('Not supported / not read (no_data)')
    expect(w.text()).toContain('Nothing was cleared')
  })
})
