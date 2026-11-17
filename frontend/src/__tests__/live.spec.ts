import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { STALE_AFTER_MS, useWorkbench } from '../stores/workbench'
import LiveView from '../views/LiveView.vue'
import { baseRoutes, connected, disconnected, miniProfile, mockFetch } from './helpers'

const plan = {
  fast: [{ pid: 0x0c, hex: '0C', name: 'Engine speed', unit: 'rpm' }, { pid: 0x10, hex: '10', name: 'Mass air flow (MAF)', unit: 'g/s' }],
  slow: [{ pid: 0x05, hex: '05', name: 'Engine coolant temperature', unit: '°C' }],
  unsupported: [{ pid: 0x23, hex: '23', name: 'Fuel rail gauge pressure', unit: 'kPa' }],
}
const reading = (pid: number, value: number | null, quality = 'ok', t = 100) => ({
  type: 'reading', session_id: 's1',
  reading: { mode: 1, pid, ecu: '7E8', name: 'x', unit: 'u', value, text: null, raw_hex: '', quality, detail: null, t_wall: 0, t_mono: t, latency_ms: 30 },
})

async function setup(status = connected) {
  vi.unstubAllGlobals()
  const calls = mockFetch({ ...baseRoutes(status), 'GET /api/recording/plan': plan })
  const pinia = createPinia()
  setActivePinia(pinia)
  const wb = useWorkbench()
  wb.status = status
  wb.profiles = [miniProfile]
  wb.activeProfileId = miniProfile.id
  const w = mount(LiveView, { global: { plugins: [pinia] } })
  await flushPromises()
  return { w, wb, calls }
}

describe('live view: absent, failed and stale signals', () => {
  beforeEach(() => vi.useRealTimers())

  it('tells the user to connect when disconnected', async () => {
    const { w } = await setup(disconnected)
    expect(w.get('[data-test=live-disconnected]').text()).toContain('Disconnected')
    expect(w.find('[data-test=tiles]').exists()).toBe(false)
  })

  it('shows unsupported signals as "not supported" and unmeasured ones as "not measured", never as 0', async () => {
    const { w } = await setup()
    expect(w.get('[data-test=plan-unsupported]').text()).toContain('Fuel rail gauge pressure (23)')
    expect(w.get('[data-test=tile-23]').text()).toContain('not supported')
    expect(w.get('[data-test=tile-rpm]').text()).toContain('not measured')
    expect(w.get('[data-test=tile-10]').text()).toContain('not measured')
    expect(w.get('[data-test=tiles]').text()).not.toMatch(/(^|\s)0(\s|$)/)
    expect(w.findAll('[data-test=chart-empty]').length).toBe(2)
  })

  it('renders live values with units and observed rate, and blanks the value when a read fails', async () => {
    const { w, wb } = await setup()
    wb.handleEvent({ type: 'batch', events: [reading(0x0c, 812.5, 'ok', 100), reading(0x0c, 790, 'ok', 100.5), reading(0x10, 9.4, 'ok', 100.2)] })
    await flushPromises()
    expect(w.get('[data-test=tile-rpm]').text()).toContain('790')
    expect(w.get('[data-test=tile-rpm]').text()).toContain('rpm')
    expect(w.get('[data-test=tile-rpm]').text()).toContain('observed 2.0 Hz')
    expect(w.get('[data-test=tile-10]').text()).toContain('9.4')
    wb.handleEvent(reading(0x0c, null, 'disconnected', 101))
    await flushPromises()
    expect(w.get('[data-test=tile-rpm]').text()).toContain('not measured')
    expect(w.get('[data-test=tile-rpm]').text()).toContain('last read failed (disconnected)')
    expect(w.get('[data-test=tile-rpm]').text()).not.toContain('790')
    expect(wb.live[0x0c].points).toHaveLength(2) // the failed read added no point
  })

  it('ignores answers from non-primary ECUs and flags stale data', async () => {
    const { w, wb } = await setup()
    const other = reading(0x0c, 5, 'ok')
    other.reading.ecu = '7E9'
    wb.handleEvent(other)
    expect(wb.live[0x0c]).toBeUndefined()
    wb.handleEvent(reading(0x0c, 800), Date.now() - STALE_AFTER_MS - 1000)
    await flushPromises()
    expect(w.get('[data-test=tile-rpm]').find('[data-test=stale]').exists()).toBe(true)
  })

  it('starts a recording and offers event markers while it runs', async () => {
    const rec = { session_id: 's1', started_at: Date.now() / 1000, duration_s: 60, fast: [12], slow: [], unsupported: [] }
    const { w, wb, calls } = await setup()
    mockFetch({ ...baseRoutes({ ...connected, recording: rec }), 'POST /api/recording/start': { recording: rec }, 'POST /api/sessions/s1/annotations': {} })
    await w.get('[data-test=start]').trigger('click')
    await flushPromises()
    expect(wb.recording?.session_id).toBe('s1')
    expect(w.find('[data-test=stop]').exists()).toBe(true)
    const buttons = w.get('[data-test=markers]').findAll('button')
    expect(buttons.map((b) => b.text())).toEqual(miniProfile.event_labels)
    await buttons[0].trigger('click')
    await flushPromises()
    expect(calls.length).toBeGreaterThan(0)
    expect((globalThis.fetch as any).mock.calls.at(-1)[0]).toBe('/api/sessions/s1/annotations')
  })
})
