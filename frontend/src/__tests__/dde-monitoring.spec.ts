import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
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
