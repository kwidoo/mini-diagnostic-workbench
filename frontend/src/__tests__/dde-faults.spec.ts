import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ScanCard from '../components/ScanCard.vue'
import type { DtcScan } from '../types'

const base: DtcScan = {
  t_wall: 1, phase: 'scan', dtcs: [{ code: 'P115D', raw_hex: '115D', kind: 'stored', ecu: '7E8' }],
  mode_status: { stored: 'supported', pending: 'supported', permanent: 'supported' }, mil_on: true, dtc_count_reported: 1, freeze_frame: null,
}
const mfr = (over: Partial<NonNullable<DtcScan['manufacturer']>>) => ({
  origin: 'bmw_dde' as const, status: 'read' as const, detail: null, synthetic: true, request_id: 'syn.fault_memory',
  catalog_version: 'x', decoder_version: '1', ecu: '7B8/F5', raw_hex: '', nrc: null, faults: [], ...over,
})

describe('BMW DDE fault memory in a scan (DDE-04)', () => {
  it('lists manufacturer codes apart from the generic codes', () => {
    const w = mount(ScanCard, {
      props: { scan: { ...base, manufacturer: mfr({ faults: [{ code: '4862', raw_hex: '486224', status_raw: '24', status: { present: false, intermittent: true }, counter: null }] }) } },
    })
    expect(w.get('[data-test=dtc-stored]').text()).toContain('P115D')
    expect(w.get('[data-test=dtc-stored]').text()).not.toContain('4862')
    const dde = w.get('[data-test=dde-faults]')
    expect(dde.text()).toContain('SYNTHETIC')
    expect(w.get('[data-test=dde-fault]').text()).toContain('4862')
    expect(w.get('[data-test=dde-fault]').text()).toContain('intermittent')
  })

  it('never says "no faults" unless the read was complete', () => {
    const un = mount(ScanCard, { props: { scan: { ...base, manufacturer: mfr({ status: 'unavailable', detail: 'disabled in the catalog', synthetic: false }) } } })
    expect(un.get('[data-test=dde-fault-status]').text()).toContain('unavailable')
    expect(un.text()).not.toContain('no fault listed')
    const none = mount(ScanCard, { props: { scan: { ...base, manufacturer: mfr({ status: 'no_faults' }) } } })
    expect(none.get('[data-test=dde-fault-status]').text()).toContain('Complete read: no fault listed')
    const old = mount(ScanCard, { props: { scan: base } })
    expect(old.find('[data-test=dde-faults]').exists()).toBe(false)
  })
})
