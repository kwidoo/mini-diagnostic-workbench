import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DdeMonitorPanel from '../components/monitoring/DdeMonitorPanel.vue'
import { useWorkbench } from '../stores/workbench'
import type { DdeMonitoringSummary, MonitorDde } from '../types'

const live: MonitorDde = {
  enabled: true, synthetic: true, catalog_version: 'dde-catalog/synthetic-1', unsupported_adapter: null, scale: 2.5,
  regeneration: { state: 'unknown', source: 'bmw_dde' },
  signals: {
    dpf_soot_mass: { title: 'Soot', tier: 'slow', interval_s: 25, value: 18.4, text: null, unit: 'g', quality: 'ok', detail: null, age_s: 3, stale: false, suspended: false },
    egr_actual: { title: 'EGR actual', tier: 'medium', interval_s: 5, value: null, text: null, unit: '%', quality: 'no_data', detail: null, age_s: 40, stale: true, suspended: true },
  },
}
const summary: DdeMonitoringSummary = {
  status: 'polling', synthetic: true, catalog_version: 'x', confirmed_variant: 'SYNTHETIC-DDE-0',
  signals: [{ signal_key: 'dpf_soot_mass', title: 'Soot', provenance: 'bmw_dde', unit: 'g', tier: 'slow', samples_ok: 4, failed: {}, status: 'measured', stats: { n: 4, min: 18, max: 19, median: 18.5, first: 18, last: 19 } }],
  regeneration: { signal_key: 'dpf_regeneration_active', status: 'measured', unknown_gaps: [], confirmed: [
    { type: 'regeneration', origin: 'measured', status: 'confirmed', source: 'bmw_dde', t_start_rel_s: 515, t_end_rel_s: 700, duration_s: 185, samples: 4, ended: 'flag inactive', synthetic: true, text: 'SYNTHETIC: DPF regeneration confirmed' },
  ] },
  commanded_vs_actual: {}, limitations: ['sequential polling'],
}

describe('BMW DDE in monitoring (DDE-06)', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('live: source, quality, age / staleness; unknown regeneration is never shown as inactive', () => {
    const w = mount(DdeMonitorPanel, { props: { live } })
    expect(w.text()).toContain('bmw_dde')
    expect(w.get('[data-test=dde-regen-live]').text()).toContain('unknown (no fresh DDE flag)')
    expect(w.get('[data-test=dde-sig-dpf_soot_mass]').text()).toContain('18.4 g')
    const egr = w.get('[data-test=dde-sig-egr_actual]').text()
    expect(egr).toContain('not measured')
    expect(egr).toContain('suspended')
    expect(egr).toContain('(stale)')
    expect(egr).not.toContain('0 %')
  })

  it('live without DDE explains why, no fake values', () => {
    useWorkbench().ddeStatus = { vehicle_profile: 'mini', supported: true, settings: { opt_in: false, confirmed_variant: null } }
    const w = mount(DdeMonitorPanel, { props: { live: null } })
    expect(w.get('[data-test=dde-off]').text()).toContain('opt in on the Vehicle tab')
  })

  it('session: confirmed by DDE kept separate from inferred generic-OBD regenerations', () => {
    const w = mount(DdeMonitorPanel, { props: { summary, inferredRegenerations: 2 } })
    expect(w.get('[data-test=regen-confirmed]').text()).toContain('confirmed by DDE')
    expect(w.get('[data-test=regen-inferred-note]').text()).toContain('inferred from generic OBD')
    expect(w.text()).toContain('SYNTHETIC')
  })
})

describe('live DDE freshness without new events (review fix 4)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.setSystemTime(new Date(1_800_000_000_000))
  })
  afterEach(() => vi.useRealTimers())

  const now = 1_800_000_000
  const fresh = (): MonitorDde => ({
    ...live,
    regeneration: { state: 'active', source: 'bmw_dde', measured_at_wall: now - 2, fresh_until_wall: now + 10, freshness_window_s: 12 },
    signals: {
      dpf_soot_mass: { ...live.signals.dpf_soot_mass, t_wall: now - 2, fresh_until_wall: now + 10, age_s: 2, stale: false },
    },
  })

  it('an active flag turns unknown at its deadline and the value is marked stale, with no new snapshot', async () => {
    const w = mount(DdeMonitorPanel, { props: { live: fresh(), linkState: 'running' } })
    expect(w.get('[data-test=dde-regen-live]').text()).toContain('ACTIVE (confirmed by DDE)')
    expect(w.get('[data-test=dde-sig-dpf_soot_mass]').text()).toContain('18.4 g')
    expect(w.get('[data-test=dde-sig-dpf_soot_mass]').text()).not.toContain('stale')
    vi.advanceTimersByTime(9_000)
    await w.vm.$nextTick()
    expect(w.get('[data-test=dde-regen-live]').text()).toContain('ACTIVE')
    expect(w.get('[data-test=dde-age]').text()).toContain('11 s')
    vi.advanceTimersByTime(3_000) // past fresh_until_wall: no new event arrived
    await w.vm.$nextTick()
    expect(w.get('[data-test=dde-regen-live]').text()).toContain('unknown (no fresh DDE flag)')
    const row = w.get('[data-test=dde-sig-dpf_soot_mass]').text()
    expect(row).toContain('(stale)')
    expect(row).toContain('not current')
  })

  it('ECU unresponsive / adapter unreachable: unknown and not current immediately', async () => {
    for (const state of ['ecu_unresponsive', 'adapter_unreachable']) {
      const w = mount(DdeMonitorPanel, { props: { live: fresh(), linkState: state } })
      expect(w.get('[data-test=dde-regen-live]').text()).toContain('unknown')
      expect(w.get('[data-test=dde-link-paused]').text()).toContain(state)
      expect(w.get('[data-test=dde-sig-dpf_soot_mass]').text()).toContain('not current')
    }
  })

  it('session summaries do not depend on the live clock', async () => {
    const w = mount(DdeMonitorPanel, { props: { summary, inferredRegenerations: 0 } })
    vi.advanceTimersByTime(3_600_000)
    await w.vm.$nextTick()
    expect(w.get('[data-test=regen-confirmed]').text()).toContain('confirmed by DDE')
  })
})

describe('DDE scheduling shares and regeneration start bounds (PR #10 P2 follow-up)', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('labels the measured share as wall-clock link time and the busy-time share separately', () => {
    const scheduling = {
      target_share: 0.35, allowed_share: 0.35, projected_share: 0.1, measured_share: 0.1,
      busy_time_share: 0.857, window_s: 30, transaction_s: 1, target_exceeded: false,
    }
    const text = mount(DdeMonitorPanel, { props: { live: { ...live, scheduling } } }).get('[data-test=dde-scheduling]').text()
    expect(text).toContain('target 35 % of wall-clock link time')
    expect(text).toContain('measured 10 %') // same basis as the target, never the busy-time share
    expect(text).toContain('(86 % of busy link time)')
    expect(text).not.toContain('measured 86 %')
    expect(text).not.toContain('target exceeded')
  })

  it('older snapshots without busy_time_share show no busy-time figure', () => {
    const scheduling = { target_share: 0.35, allowed_share: 0.2, projected_share: 0.4, measured_share: 0.38, transaction_s: 1.2, target_exceeded: true }
    const text = mount(DdeMonitorPanel, { props: { live: { ...live, scheduling } } }).get('[data-test=dde-scheduling]').text()
    expect(text).toContain('measured 38 %')
    expect(text).not.toContain('busy link time')
    expect(text).toContain('target exceeded: slow adapter')
  })

  it('a regeneration active before an unknown gap is shown without a claimed start', () => {
    const resumed = {
      ...summary.regeneration!.confirmed[0], t_start_rel_s: 100, duration_s: 0, samples: 1,
      started: 'active at the first fresh reading after an unknown gap; also active before the gap, so it may have continued through it (start not observed, no known lower bound)',
      started_between_rel_s: [null, 100],
      text: 'SYNTHETIC: DPF regeneration confirmed by the DDE regeneration flag over a measured span of 0 s (1 active reading(s), first to last; unknown gaps are never included). Start: active at the first fresh reading after an unknown gap; also active before the gap, so it may have continued through it (start not observed, no known lower bound); end: still active at the end.',
    }
    const s: DdeMonitoringSummary = { ...summary, regeneration: { ...summary.regeneration!, confirmed: [resumed] } }
    const row = mount(DdeMonitorPanel, { props: { summary: s } }).get('[data-test=regen-confirmed]').text()
    expect(row).toContain('may have continued through it')
    expect(row).toContain('no known lower bound')
  })
})
