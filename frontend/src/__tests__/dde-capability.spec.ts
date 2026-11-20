import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import type { CapField, DdeCapabilityReport } from '../types'
import { baseRoutes, connected, mockFetch } from './helpers'

const f = (value: unknown, quality: CapField['quality'], source: string | null = null): CapField => ({ value, quality, source, detail: '' })

export const report: DdeCapabilityReport = {
  format: 'mini-obd-dde-capability/1', session_id: 'cap1', assessed_at: 0, source: 'real', synthetic: false,
  adapter: {
    identity: f('ELM327 v2.2', 'claimed', 'ATI'), device_description: f('OBDII to RS232 Interpreter', 'claimed', 'AT@1'),
    claimed_elm_version: f('2.2', 'claimed', 'ATI'), supply_voltage: f('13.5V', 'observed', 'ATRV'), chip: f(null, 'unknown'),
    notes: ['ATI / AT@1 are strings the adapter firmware reports about itself.'],
  },
  protocol: { number: f('A6', 'observed', 'ATDPN'), name: f('AUTO, ISO 15765-4 (CAN 11/500)', 'observed', 'ATDP'), can_11bit_500k: f(true, 'derived', 'ATDPN') },
  ecus: [{ source_id: '7E8', primary: true, mode01_pids: 26, mode09_infotypes: ['02', '04'], role: 'engine ECU candidate' }],
  identification: {
    calibration_id: f([{ ecu: '7E8', text: 'CAL123', raw_hex: '00' }], 'observed', 'service 09 InfoType 04'),
    vin: f([{ ecu: '7E8', length: 17, text: null }], 'observed', 'service 09 InfoType 02'),
    cvn: f(null, 'not_attempted', 'service 09 InfoType 06'),
  },
  isotp_multiframe: f(false, 'unknown', 'raw trace'),
  dde: { variant: f(null, 'unknown'), software_version: f(null, 'unknown'), manufacturer_requests_sent: f(0, 'observed', 'raw trace') },
  verdicts: {
    generic_obd: { verdict: 'generic_obd_verified', reasons: ['2 ECUs answered'] },
    manufacturer_transport: { verdict: 'manufacturer_transport_unverified', blockers: [], unverified: ['no completed ISO-TP multi-frame answer observed yet'], meaning: 'evidence missing' },
  },
}

async function vehicleTab() {
  const w = mount(App, { global: { plugins: [createPinia()] } })
  await flushPromises()
  await w.get('[data-test=tab-vehicle]').trigger('click')
  await flushPromises()
  return w
}

describe('DDE capability panel (DDE-01)', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('shows verdicts, quality per field and an export link; the VIN itself is never displayed', async () => {
    mockFetch({ ...baseRoutes(), 'GET /api/dde/capability/latest': { vehicle_profile: 'x', report } })
    const w = await vehicleTab()
    const p = w.get('[data-test=dde-capability]')
    expect(p.get('[data-test=dde-mfr-verdict]').text()).toBe('manufacturer_transport_unverified')
    expect(p.get('[data-test=dde-generic-verdict]').text()).toBe('generic_obd_verified')
    expect(p.get('[data-test=dde-fields]').text()).toContain('claimed')
    expect(p.get('[data-test=dde-fields]').text()).toContain('VIN (17 chars, not shown)')
    expect(p.get('[data-test=dde-export]').attributes('href')).toBe('/api/sessions/cap1/export')
    expect(p.get('[data-test=run-dde-capability]').attributes('disabled')).toBeDefined() // not connected
    expect(p.text()).not.toMatch(/DDE supported/i)
  })

  it('runs the check when connected and shows the new report', async () => {
    const calls = mockFetch({
      ...baseRoutes(connected),
      'GET /api/dde/capability/latest': { vehicle_profile: 'x', report: null },
      'POST /api/dde/capability': { id: 'cap2', kind: 'capability', report: { ...report, session_id: 'cap2', synthetic: true } },
    })
    const w = await vehicleTab()
    expect(w.find('[data-test=dde-none]').exists()).toBe(true)
    await w.get('[data-test=run-dde-capability]').trigger('click')
    await flushPromises()
    expect(calls.some((c) => c.method === 'POST' && c.url === '/api/dde/capability' && c.body.read_vin === false)).toBe(true)
    expect(w.get('[data-test=dde-synthetic]').text()).toContain('SYNTHETIC')
  })
})

describe('DDE manufacturer-read controls (DDE-03)', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('shows opt-in default off, saves the opt-in and the confirmed variant explicitly', async () => {
    const status = {
      vehicle_profile: 'mini', supported: true, settings: { opt_in: false, confirmed_variant: null }, version: 'dde-catalog/x',
      synthetic: false, context: { opted_in: false, confirmed_variant: null, adapter_verdict: null, synthetic_session: false },
      availability: [{ metric: 'dpf_soot_mass', title: 'DPF soot load', status: 'blocked', reasons: ['disabled in the catalog (enabled: false)'] }],
    }
    const calls = mockFetch({
      ...baseRoutes(),
      'GET /api/dde/capability/latest': { vehicle_profile: 'x', report: null },
      'GET /api/dde/status': status,
      'PUT /api/dde/settings': (b: any) => ({ body: { ...status, settings: { opt_in: !!b.opt_in, confirmed_variant: b.confirmed_variant ?? null } } }),
    })
    const w = await vehicleTab()
    const box = w.get('[data-test=dde-opt-in]')
    expect((box.element as HTMLInputElement).checked).toBe(false)
    expect(w.get('[data-test=dde-availability]').text()).toContain('blocked')
    expect(w.get('[data-test=dde-catalog-summary]').text()).toContain('0 of 1')
    ;(box.element as HTMLInputElement).checked = true
    await box.trigger('change')
    await flushPromises()
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ opt_in: true })
    await w.get('[data-test=dde-variant]').setValue('  ')
    await w.get('[data-test=dde-save-variant]').trigger('click')
    await flushPromises()
    expect(calls.filter((c) => c.method === 'PUT').at(-1)?.body).toEqual({ clear_variant: true })
  })
})
