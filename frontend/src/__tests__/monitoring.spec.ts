import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MonitoringHistory from '../components/monitoring/MonitoringHistory.vue'
import MonitoringLive from '../components/monitoring/MonitoringLive.vue'
import MonitoringSession from '../components/monitoring/MonitoringSession.vue'
import { ADAPTER_VOLTAGE_KEY, STALE_AFTER_MS, useWorkbench } from '../stores/workbench'
import type { AirPath, MonitorState, MonitoringSummary, Status } from '../types'
import LiveView from '../views/LiveView.vue'
import MonitoringView from '../views/MonitoringView.vue'
import { baseRoutes, connected, miniProfile, mockFetch, porscheProfile } from './helpers'

const ref = (pid: number, hex: string, name: string, unit: string | null = null) => ({ pid, hex, name, unit })
const plan = {
  fast: [ref(0x0c, '0C', 'Engine speed', 'rpm'), ref(0x0d, '0D', 'Vehicle speed', 'km/h')],
  medium: [ref(0x05, '05', 'Engine coolant temperature', '°C'), ref(0x42, '42', 'Control module voltage', 'V')],
  slow: [ref(0x7a, '7A', 'Diesel particulate filter (DPF) bank 1')],
  unsupported: [ref(0x7b, '7B', 'Diesel particulate filter (DPF) bank 2'), ref(0x5c, '5C', 'Engine oil temperature')],
  tiers: { fast: 0.5, medium: 2, slow: 10 }, monitoring: true, adapter_voltage: 'polled', description: 'Runs until stopped.',
}
const monitorState = (over: Partial<MonitorState> = {}): MonitorState => ({
  state: 'running', engine: 'running', mil_on: false, base_intervals: { fast: 0.5, medium: 2, slow: 10 },
  effective_intervals: { fast: 0.5, medium: 2, slow: 10 }, capacity_rps: 18, suspended: [], resume_probe_s: null,
  items: { '01:0C': { tier: 'fast', pid: 12, interval_s: 0.5 }, '01:05': { tier: 'medium', pid: 5, interval_s: 2 }, 'adapter:ATRV': { tier: 'slow', pid: null, interval_s: 10 } },
  ...over,
})
const recording = (over: Partial<MonitorState> = {}) => ({
  session_id: 'm1', started_at: Date.now() / 1000 - 65, duration_s: null, fast: [12], medium: [5], slow: [], unsupported: [],
  until_stop: true, profile: 'mini_extended_monitoring', monitoring: true, monitor: monitorState(over),
})
const reading = (pid: number, value: number | null, mode = 1, t = 100) => ({
  type: 'reading', session_id: 'm1',
  reading: { mode, pid, ecu: mode === 0 ? null : '7E8', name: 'x', unit: 'V', value, text: null, raw_hex: '', quality: value === null ? 'no_data' : 'ok', detail: null, t_wall: 0, t_mono: t, latency_ms: 30 },
})

async function mountWith(component: any, status: Status, extra: Record<string, unknown> = {}) {
  vi.unstubAllGlobals()
  const calls = mockFetch({ ...baseRoutes(status), 'GET /api/recording/plan': plan, ...extra })
  const pinia = createPinia()
  setActivePinia(pinia)
  const wb = useWorkbench()
  wb.status = status
  wb.profiles = [miniProfile, porscheProfile]
  wb.activeProfileId = miniProfile.id
  wb.liveSessionId = status.recording?.session_id ?? null
  const w = mount(component, { global: { plugins: [pinia] } })
  await flushPromises()
  return { w, wb, calls }
}

describe('MINI monitoring: live', () => {
  beforeEach(() => vi.useRealTimers())

  it('starts a continuous recording with drive type and safety limit', async () => {
    const { w, calls } = await mountWith(MonitoringLive, connected, { 'POST /api/recording/start': { recording: recording() } })
    expect(w.get('[data-test=driver-notice]').text()).toContain('driver must not operate')
    await w.get('[data-test=drive-type]').setValue('city')
    await w.get('[data-test=max-hours]').setValue('2')
    await w.get('[data-test=start-monitoring]').trigger('click')
    await flushPromises()
    const start = calls.find((c) => c.url === '/api/recording/start')!
    expect(start.body).toMatchObject({ profile: 'mini_extended_monitoring', until_stop: true, max_duration_s: 7200, drive_type: 'city' })
  })

  it('shows measured, adapter, unavailable and raw-only data distinctly, never as 0', async () => {
    const { w, wb } = await mountWith(MonitoringLive, { ...connected, recording: recording() })
    wb.handleEvent({ type: 'batch', events: [reading(0x0c, 812), reading(0x42, 14.1), reading(0, 13.9, 0)] })
    await flushPromises()
    expect(w.get('[data-test=mtile-12]').text()).toContain('812')
    expect(w.get(`[data-test=mtile-${ADAPTER_VOLTAGE_KEY}]`).text()).toContain('13.9')
    expect(w.get(`[data-test=mtile-${ADAPTER_VOLTAGE_KEY}]`).text()).toContain('adapter (ATRV')
    expect(w.get('[data-test=mtile-66]').text()).toContain('ECU (PID 42')
    expect(w.get('[data-test=mtile-5]').text()).toContain('not measured')
    expect(w.get('[data-test=dpf-7A]').text()).toContain('raw only')
    expect(w.get('[data-test=dpf-7B]').text()).toContain('not available')
    expect(w.get('[data-test=dpf-live]').text()).toContain('never confirmed')
    expect(w.get('[data-test=elapsed]').text()).toMatch(/0:01:0[4-6]/)
  })

  it('uses each signal\'s own interval for staleness', async () => {
    const { w, wb } = await mountWith(MonitoringLive, { ...connected, recording: recording() })
    // coolant (medium, 2 s): 4 s old is fine (< 3 x 2 s); RPM (fast): 4 s old is stale
    wb.handleEvent(reading(0x05, 80), Date.now() - 4000)
    wb.handleEvent(reading(0x0c, 800), Date.now() - STALE_AFTER_MS - 1000)
    await flushPromises()
    expect(w.get('[data-test=mtile-5]').find('[data-test=stale]').exists()).toBe(false)
    expect(w.get('[data-test=mtile-12]').find('[data-test=stale]').exists()).toBe(true)
  })

  it('shows a Wi-Fi loss while ECU polling is paused: no resume offered, stop and save still possible', async () => {
    const st = { ...connected, recording: recording({ state: 'adapter_unreachable', engine: 'stopped', ecu_polling_paused: true, adapter_link: 'lost' }) }
    const { w } = await mountWith(MonitoringLive, st)
    expect(w.get('[data-test=link-text]').text()).toContain('no vehicle request')
    expect(w.get('[data-test=adapter-lost-paused]').text()).toContain('stop and save')
    expect(w.find('[data-test=resume-ecu]').exists()).toBe(false)
  })

  it('warns that resuming after an adapter-only reconnect re-initialises the adapter', async () => {
    const st = { ...connected, recording: recording({ state: 'ecu_unresponsive', engine: 'stopped', ecu_polling_paused: true, resume_requires_reinit: true }) }
    const { w } = await mountWith(MonitoringLive, st)
    expect(w.get('[data-test=resume-reinit]').text()).toContain('ATZ')
  })

  it('offers fan / battery events after the engine stopped and while the ECU is silent, without claiming ignition off', async () => {
    const st = { ...connected, recording: recording({ state: 'ecu_unresponsive', engine: 'stopped' }) }
    const { w, calls } = await mountWith(MonitoringLive, st, {
      'POST /api/sessions/m1/annotations': { t_wall: 1, t_mono: 1, label: 'fan_running', note: '', event_type: 'fan_running', origin: 'manual' },
      'POST /api/recording/resume-ecu': { requested: true },
    })
    expect(w.get('[data-test=link-text]').text()).toContain('not proof that the ignition is off')
    expect(w.get('[data-test=engine-state]').text()).toContain('stopped')
    const fan = w.get('[data-test=event-fan_running]')
    expect(fan.attributes('disabled')).toBeUndefined()
    await fan.trigger('click')
    await flushPromises()
    const post = calls.find((c) => c.url === '/api/sessions/m1/annotations')!
    expect(post.body).toEqual({ event_type: 'fan_running', note: '' })
    expect(w.get('[data-test=live-events]').text()).toContain('fan_running')
    expect(w.get('[data-test=live-events]').text()).toContain('manual')
    expect(w.find('[data-test=event-battery_warning]').exists()).toBe(true)
    await w.get('[data-test=resume-ecu]').trigger('click')
    await flushPromises()
    expect(calls.some((c) => c.url === '/api/recording/resume-ecu' && c.method === 'POST')).toBe(true)
  })

  it('shows observed events from the recorder in the live log', async () => {
    const { w, wb } = await mountWith(MonitoringLive, { ...connected, recording: recording() })
    wb.handleEvent({ type: 'annotation', session_id: 'm1', annotation: { t_wall: 1, t_mono: 5, label: 'engine_stopped', note: 'rpm 0', event_type: 'engine_stopped', origin: 'observed' } })
    wb.handleEvent({ type: 'monitor', session_id: 'm1', monitor: monitorState({ state: 'ecu_unresponsive', engine: 'stopped' }) })
    await flushPromises()
    expect(w.get('[data-test=live-events]').text()).toContain('engine_stopped')
    expect(w.get('[data-test=live-events]').text()).toContain('observed')
    expect(w.get('[data-test=link-text]').text()).toContain('ECU not answering')
  })

  it('after Stop, events go to the last monitoring session', async () => {
    const sessions = [{ id: 'old', kind: 'record', vehicle_profile: miniProfile.id, source: 'real', synthetic: false, label: '', started_at: 1, ended_at: 2, monitoring: true }]
    const { w, wb } = await mountWith(MonitoringLive, connected, { 'GET /api/sessions': sessions })
    await wb.loadSessions()
    await flushPromises()
    expect(w.get('[data-test=event-target]').text()).toContain('last monitoring session old')
    expect(w.get('[data-test=event-fan_stopped]').attributes('disabled')).toBeUndefined()
  })

  it('the monitoring tab is MINI-only', async () => {
    const { w, wb } = await mountWith(MonitoringView, connected)
    expect(w.find('[data-test=monitoring-live]').exists()).toBe(true)
    wb.activeProfileId = porscheProfile.id
    await flushPromises()
    expect(w.get('[data-test=monitoring-not-for-profile]').text()).toContain('MINI profile only')
  })
})

describe('Live tab keeps the duration interface and adds until-stop', () => {
  it('posts until_stop only when the box is ticked', async () => {
    const rec = { session_id: 's1', started_at: Date.now() / 1000, duration_s: null, fast: [12], slow: [], unsupported: [], until_stop: true }
    const { w, calls } = await mountWith(LiveView, connected, { 'POST /api/recording/start': { recording: rec } })
    await w.get('[data-test=start]').trigger('click')
    await flushPromises()
    expect(calls.find((c) => c.url === '/api/recording/start')!.body).toEqual({ duration_s: 120, profile: 'idle', label: '' })
    await w.get('[data-test=until-stop]').setValue(true)
    await w.get('[data-test=max-duration]').setValue('3600')
    await w.get('[data-test=start]').trigger('click')
    await flushPromises()
    expect(calls.filter((c) => c.url === '/api/recording/start').at(-1)!.body).toEqual({
      duration_s: 120, profile: 'idle', label: '', until_stop: true, max_duration_s: 3600,
    })
  })
})

const summary: MonitoringSummary = {
  session_id: 'm1', is_monitoring_session: true, synthetic: true,
  drive: { running_s: 600, moving_share: 0.6, max_speed_kmh: 52, mean_speed_moving_kmh: 30, drive_type_user_label: 'city' },
  engine_runs: [{ t_start_rel_s: 5, t_end_rel_s: 605, duration_s: 600, start_observed: true, stop_observed: true }],
  shutdowns: [{ t_rel_s: 605, origin: 'observed', basis: 'measured RPM fell to ~0' }],
  fan: { runs: [{ t_start_rel_s: 610, t_end_rel_s: 850, duration_s: 240, complete: true, after_shutdown: true, seconds_after_shutdown: 5, shutdown_origin: 'observed', marked_unusually_loud: false }],
    fan_stopped_without_running: [], source: 'manual annotations' },
  voltage: {
    caveat: 'Voltage is not battery state of charge.',
    ecu: { status: 'measured', provenance: 'ECU (PID 42 control module voltage)', at_start: { value: 12.5, t_rel_s: 0 }, engine_running: { n: 9, min: 11.9, median: 14.1, max: 14.3 },
      engine_off: null, low_while_running: { screen_v: 12.2, samples: 3, share: 0.1, min_v: 11.9 }, drops: [], shutdowns: [] },
    adapter: { status: 'not_measured', provenance: 'adapter (ATRV)' },
    battery_warnings: [{ event: 'battery_warning', t_rel_s: 200, origin: 'manual', engine_running: true, nearby: { ecu_voltage: { min: 11.9, max: 12.1, n: 4 }, adapter_voltage: null }, note: '' }],
  },
  coolant: { status: 'measured', max_running: 88, usual_warm_c: null, usual_warm_sessions: 0, first: { value: 20, t_rel_s: 0 }, detected: [], limitations: ['No usual warm temperature yet'] },
  dpf: {
    availability: [
      { group: 'wanted', items: [
        { key: 'soot_mass', name: 'Calculated soot mass', status: 'not_available', via: 'manufacturer-only' },
        { key: 'differential_pressure', name: 'DPF differential pressure', status: 'raw_only', via: 'PID 7A' },
      ] },
      { group: 'standard_pids', items: [{ hex: '7A', name: 'DPF bank 1', status: 'raw_only' }] },
      { group: 'manufacturer', items: [] },
    ],
    possible_regenerations: [{ type: 'possible_dpf_regeneration', origin: 'inferred', status: 'possible', confidence: 'moderate', text: 'Possible DPF regeneration (3 indirect indicators).', t_start_rel_s: 420, t_end_rel_s: 600 }],
    possible_interrupted_regenerations: [], user_suspected: [], limitations: [], confirmed_regenerations: [],
  },
  detected_events: [{ type: 'possible_dpf_regeneration', origin: 'inferred', status: 'possible', confidence: 'moderate', text: 'Possible DPF regeneration (3 indirect indicators).', t_start_rel_s: 420 }],
  events: [
    { t_rel_s: 605, label: 'engine_stopped', event_type: 'engine_stopped', origin: 'observed', confidence: 'measured', note: '' },
    { t_rel_s: 610, label: 'fan_running', event_type: 'fan_running', origin: 'manual', confidence: null, note: '' },
  ],
  link: [{ type: 'ecu_communication_lost', t_rel_s: 640, gap_s: null, note: '' }],
  signals: [{ pid: 0x5c, hex: '5C', name: 'Engine oil temperature', tier: null, status: 'not_available', provenance: 'ECU', ok: 0, raw: 0, failed: 0 }],
  sampling: { base_intervals: { fast: 0.5, medium: 2, slow: 10 }, final_effective_intervals: { fast: 0.5, medium: 2, slow: 10 }, adjustments: [], time_in_state_s: null,
    signals: [{ pid: 12, name: 'Engine speed', attempts: 10, ok: 10, dropped: 0, error_rate: 0, actual_hz: 2, latency_ms_mean: 30, latency_ms_p95: 40, latency_ms_max: 50, max_gap_s: 0.6, gaps: 0, tier: 'fast', target_hz_base: 2, target_hz_final: 2 }] },
  observed_capabilities: { ecu_answers_after_engine_stop: 40, ecu_answered_for_s_after_engine_stop: 25, adapter_voltage_reads_while_ecu_unresponsive: 30 },
}

describe('MINI monitoring: session details and history', () => {
  it('differentiates measured, observed, manual, inferred and unavailable data; fan duration from markers', async () => {
    const sessions = [{ id: 'm1', kind: 'record', vehicle_profile: miniProfile.id, source: 'simulator', synthetic: true, label: 'run', started_at: 1, ended_at: 2, monitoring: true, drive_type: 'city' }]
    const series = { session_id: 'm1', signals: [
      { pid: 12, hex: '0C', name: 'Engine speed', unit: 'rpm', decoded: true, provenance: 'ecu', points: [[0, 800], [1, 810]], failed: [] },
      { pid: -1, hex: 'ATRV', name: 'Adapter supply voltage', unit: 'V', decoded: true, provenance: 'adapter', points: [[0, 12.4]], failed: [] },
    ], annotations: [{ t: 605, label: 'engine_stopped', note: '', event_type: 'engine_stopped', origin: 'observed', t_wall: 1, t_mono: 1 }] }
    const { w, wb } = await mountWith(MonitoringSession, connected, {
      'GET /api/sessions': sessions, 'GET /api/sessions/m1/monitoring': summary, 'GET /api/sessions/m1/samples': series,
    })
    await wb.loadSessions()
    await flushPromises()
    const text = w.text()
    expect(w.get('[data-test=fan-row]').text()).toContain('240 s')
    const kinds = w.findAll('[data-test=data-badge]').map((b) => b.attributes('data-kind'))
    for (const k of ['observed', 'manual', 'inferred', 'not_available', 'raw_only', 'not_measured']) expect(kinds).toContain(k)
    expect(w.get('[data-test=dpf-want-soot_mass]').text()).toContain('not available')
    expect(w.get('[data-test=regen]').text()).toContain('inferred · possible')
    expect(text).not.toMatch(/confirmed regeneration[^s]/i)
    expect(text).toContain('Confirmed regenerations: none can be confirmed')
    expect(w.get('[data-test=battery-warning]').text()).toContain('11.9-12.1 V')
    expect(w.get('[data-test=observed-capabilities]').text()).toContain('40 request(s)')
    expect(w.findAll('[data-test=band]').length).toBeGreaterThan(0) // inferred interval shaded, not plotted as a value
    expect(w.findAll('[data-test=marker][data-origin=observed]').length).toBeGreaterThan(0)
  })

  it('never groups real and synthetic sessions together and flags recovered sessions', async () => {
    const row = (id: string, synthetic: boolean, fan: number[], interrupted = false) => ({
      id, started_at: 1, label: id, synthetic, baseline_origin: synthetic ? 'synthetic' : 'real', aborted: interrupted ? 'interrupted: ...' : null,
      interrupted, drive_type: 'city', running_min: 12, max_speed_kmh: 50, engine_stops: 1, fan_runs_after_shutdown: fan.length, fan_durations_s: fan,
      battery_warnings: 0, possible_regenerations: 0, possible_interrupted_regenerations: 0, user_suspected_regenerations: 0, coolant_max_c: 88,
      ecu_voltage_running_median: 14.1, adapter_voltage_running_median: 13.9, link_events: 0,
    })
    const hist = { sessions: [row('r1', false, [100], true), row('s1', true, [400]), row('s2', true, [600])] }
    const { w } = await mountWith(MonitoringHistory, connected, { 'GET /api/monitoring/history': hist })
    expect(w.get('[data-test=group-real-city]').text()).toContain('100 s')
    expect(w.get('[data-test=group-synthetic-city]').text()).toContain('500 s')
    expect(w.get('[data-test=history-r1]').find('[data-test=interrupted]').exists()).toBe(true)
  })

  it('compares drives grouped by the user drive-type label', async () => {
    const row = (id: string, t: string, fan: number[]) => ({
      id, started_at: 1, label: id, synthetic: false, aborted: null, drive_type: t, running_min: 12, max_speed_kmh: 50, engine_stops: 1,
      fan_runs_after_shutdown: fan.length, fan_durations_s: fan, battery_warnings: t === 'city' ? 1 : 0, possible_regenerations: 1,
      possible_interrupted_regenerations: t === 'city' ? 1 : 0, user_suspected_regenerations: 0, coolant_max_c: 88,
      ecu_voltage_running_median: 14.1, adapter_voltage_running_median: 13.9, link_events: 0,
    })
    const hist = { sessions: [row('a', 'city', [240]), row('b', 'city', [180]), row('c', 'highway', [])] }
    const { w } = await mountWith(MonitoringHistory, connected, { 'GET /api/monitoring/history': hist })
    expect(w.get('[data-test=group-real-city]').text()).toContain('210 s')
    expect(w.get('[data-test=group-real-highway]').text()).toContain('n/a')
    expect(w.findAll('[data-test^=history-]').filter((x) => x.element.tagName === 'TR').length).toBe(3)
    await w.get('[data-test=history-filter]').setValue('highway')
    expect(w.find('[data-test=history-a]').exists()).toBe(false)
    expect(w.find('[data-test=history-c]').exists()).toBe(true)
  })
})

const held = (value: number, age_s = 0.2, fresh = true) => ({ value, age_s, fresh })
const airPath: AirPath = {
  status: 'evaluated',
  signals: {
    maf: { name: 'MAF air flow rate (10)', hex: '10', status: 'measured', samples: 900, failed_reads: 11, reason: null },
    egr_error: { name: 'EGR error (2D)', hex: '2D', status: 'measured', samples: 200, failed_reads: 0, reason: null },
  },
  running_s: 430,
  maf: {
    samples_ok: 900, failed_reads: { n: 11, by_quality: { no_data: 11 } },
    running_state: { running_confirmed: 880, engine_not_running: 9, unknown: 11, note: '' },
    zero_running: 18, low_running: 21, zero_per_1k_running: 20.45, low_per_1k_running: 23.86, zero_engine_not_running: 9, low_running_state_unknown: 1,
    episodes: [{
      type: 'maf_zero_while_running', origin: 'inferred', status: 'candidate', confidence: 'moderate', text: 'MAF 0.00 g/s ...',
      t_start_rel_s: 69, t_end_rel_s: 70, duration_s: 1, samples: 3, zero_samples: 3, min_maf_gs: 0, pattern: 'persistent', repeated: true, cluster: 0,
      motion: 'idle', temperature_state: 'cold', ecu: '7E8', context: { map: held(101), egr_error: held(30, 9, false) },
      rpm: { unstable: false, window: { n: 20, min: 790, max: 820, std: 8 } },
      evidence: [{ t_rel_s: 69, value_gs: 0, raw_hex: '0000', response: '41 10 00 00', ecu: '7E8' }], limitations: [],
    }],
    episode_count: 1, episode_total_s: 1, persistent_episodes: 1, transient_episodes: 0, repeated_clusters: 1, dips: [],
    by_temperature: { cold: { running_samples: 500, zero: 18, low: 21, episodes: 1, low_per_1k: 42, zero_per_1k: 36 },
      warm: { running_samples: 380, zero: 0, low: 0, episodes: 0, low_per_1k: 0, zero_per_1k: 0 }, unknown: { running_samples: 0, zero: 0, low: 0, episodes: 0, low_per_1k: null, zero_per_1k: null } },
  },
  egr_error: { samples: 200, count: 2, per_running_h: 16.7, max_abs_pct: 35, screen_pct: 15, excursions: [] },
  correlations: { supporting: ['1 of 1 MAF episode(s) had an EGR-error excursion'], contradicting: ['MAP stayed within +/-5 kPa during 1 MAF episode(s).'],
    note: 'associations in time, not causal relationships', symptom_markers: [] },
  dtcs: {
    scans: [{ phase: 'pre', t_rel_s: 0, mil_on: true }], codes: ['P115D'],
    by_code: [{ code: 'P115D', family: 'manufacturer_defined', air_path_relevance: 'unknown', description: null, description_status: 'not_verified',
      note: 'manufacturer-defined code range', ecus: ['7E8'], statuses: ['stored'], scan_phases: ['pre'], timing: 'present in the pre-scan; when it was set is unknown' }],
    mil_on: true, owner_reported_manufacturer_codes: [{ code: '4862', system: 'BMW DDE', source: 'owner report' }], owner_reported_note: 'not read by this tool', notes: [],
  },
  limitations: ['Signals are polled one after another.'],
  heuristics: { maf_low_gs: 1, airpath_running_rpm: 600, egr_error_screen_pct: 15 },
  history: { other_sessions: 0, medians: null, note: 'Needs at least 2 other sessions' },
}

describe('MINI monitoring: air path', () => {
  const sessions = [{ id: 'm1', kind: 'record', vehicle_profile: miniProfile.id, source: 'simulator', synthetic: true, label: 'run', started_at: 1, ended_at: 2, monitoring: true }]
  const series = { session_id: 'm1', signals: [
    { pid: 16, hex: '10', name: 'MAF', unit: 'g/s', decoded: true, provenance: 'ecu', points: [[0, 9.5], [69, 0], [71, 9.4]], failed: [] },
  ], annotations: [] }

  it('shows MAF candidates with raw evidence, stale context, P115D without a guessed meaning, and owner codes apart', async () => {
    const { w, wb } = await mountWith(MonitoringSession, connected, {
      'GET /api/sessions': sessions, 'GET /api/sessions/m1/monitoring': { ...summary, air_path: airPath }, 'GET /api/sessions/m1/samples': series,
    })
    await wb.loadSessions()
    await flushPromises()
    const panel = w.get('[data-test=air-path-panel]')
    expect(panel.get('[data-test=air-path-maf-counts]').text()).toContain('18 at 0.00 g/s')
    const ep = panel.get('[data-test=maf-episode]').text()
    expect(ep).toContain('41 10 00 00')
    expect(ep).toContain('inferred · candidate')
    expect(ep).toContain('EGR error stale (9 s old)')
    expect(panel.get('[data-test=dtc-P115D]').text()).toContain('description not verified')
    expect(panel.get('[data-test=owner-codes]').text()).toContain('not read by this tool')
    expect(panel.get('[data-test=air-path-correlations]').text()).toContain('MAP stayed')
    expect(w.findAll('[data-test=band]').some((b) => b.text().includes('MAF 0 (candidate)'))).toBe(true)
  })

  it('shows unavailable MAF as not available, never as zero, and tolerates older servers without air_path', async () => {
    const unavailable: AirPath = {
      ...airPath, status: 'maf_not_available',
      signals: { maf: { ...airPath.signals.maf, status: 'not_available', samples: 0 }, egr_error: { ...airPath.signals.egr_error, status: 'not_available', samples: 0 } },
      maf: { samples_ok: 0, failed_reads: { n: 0, by_quality: {} }, episodes: [], dips: [] },
      egr_error: { samples: 0, count: null, per_running_h: null, excursions: [] },
    }
    const { w, wb } = await mountWith(MonitoringSession, connected, {
      'GET /api/sessions': sessions, 'GET /api/sessions/m1/monitoring': { ...summary, air_path: unavailable }, 'GET /api/sessions/m1/samples': series,
    })
    await wb.loadSessions()
    await flushPromises()
    expect(w.get('[data-test=air-path-maf-unavailable]').text()).toContain('not zero')
    expect(w.get('[data-test=air-path-egr]').text()).toContain('not available')
    expect(w.find('[data-test=air-path-maf-counts]').exists()).toBe(false)

    const old = await mountWith(MonitoringSession, connected, {
      'GET /api/sessions': sessions, 'GET /api/sessions/m1/monitoring': summary, 'GET /api/sessions/m1/samples': series,
    })
    await old.wb.loadSessions()
    await flushPromises()
    expect(old.w.find('[data-test=air-path-panel]').exists()).toBe(false)
    expect(old.w.find('[data-test=dpf-panel]').exists()).toBe(true)
  })

  it('compares drives with normalised air-path rates and a per-origin trend', async () => {
    const base = {
      started_at: 1, synthetic: false, baseline_origin: 'real', aborted: null, drive_type: 'city', running_min: 12, max_speed_kmh: 50, engine_stops: 0,
      fan_runs_after_shutdown: 0, fan_durations_s: [], battery_warnings: 0, possible_regenerations: 0, possible_interrupted_regenerations: 0,
      user_suspected_regenerations: 0, coolant_max_c: 73, ecu_voltage_running_median: 14.1, adapter_voltage_running_median: 13.9, link_events: 1,
    }
    const ap = { maf_status: 'measured', maf_low_per_1k: 23.86, maf_zero_per_1k: 20.45, maf_episodes: 7, maf_episodes_per_h: 58.6, egr_excursions_per_h: 16.7,
      dtcs: ['P115D'], mil_on: true }
    const trend = {
      cohort: { vehicle_profile: miniProfile.id, origin: 'real', vehicle_identity: null }, sessions: 2, sessions_with_maf: 1, sessions_scanned: 2, series: [],
      medians: { maf_low_per_1k: 23.86, maf_zero_per_1k: 20.45, maf_episodes_per_h: 58.6, egr_excursions_per_h: 16.7 },
      by_drive_type: { city: { sessions: 1, maf_low_per_1k: 23.86, maf_zero_per_1k: 20.45, maf_episodes_per_h: 58.6, egr_excursions_per_h: 16.7 } },
      cold: null, warm: null, idle: null, moving: null, dtc_recurrence: [{ code: 'P115D', sessions: 2, share_of_scanned_sessions: 1, first_seen: 1, last_seen: 2 }],
      mil_on_sessions: 2, note: 'Rates are normalised',
    }
    const hist = { sessions: [
      { ...base, id: 'a', label: 'a', air_path: ap },
      { ...base, id: 'b', label: 'b', air_path: { ...ap, maf_status: 'not_available', maf_low_per_1k: null, maf_zero_per_1k: null, maf_episodes: null, maf_episodes_per_h: null } },
      { ...base, id: 'old', label: 'old' },
    ], air_path_trends: [trend] }
    const { w } = await mountWith(MonitoringHistory, connected, { 'GET /api/monitoring/history': hist })
    expect(w.get('[data-test=airpath-maf-a]').text()).toBe('23.86 / 20.45')
    expect(w.get('[data-test=airpath-maf-b]').text()).toBe('n/a / n/a')
    expect(w.get('[data-test=airpath-maf-old]').text()).toBe('n/a')
    expect(w.get('[data-test=history-a]').text()).toContain('P115D · MIL on')
    expect(w.get('[data-test=airpath-dtc-recurrence-real]').text()).toContain('P115D in 2 (100 %)')
    expect(w.findAll('[data-test^=history-]').filter((x) => x.element.tagName === 'TR').length).toBe(3)
  })
})
