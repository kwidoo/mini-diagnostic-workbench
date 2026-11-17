import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import { useWorkbench } from '../stores/workbench'
import { baseRoutes, connected, disconnected, miniProfile, mockFetch, porscheProfile } from './helpers'

async function mountApp() {
  const w = mount(App, { global: { plugins: [createPinia()] } })
  await flushPromises()
  return w
}

describe('connection states and errors', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('starts disconnected, read-only, with the documented adapter defaults', async () => {
    mockFetch(baseRoutes())
    const w = await mountApp()
    expect(w.get('[data-test=read-only]').text()).toBe('READ-ONLY')
    expect(w.get('[data-test=conn-badge]').text()).toBe('disconnected')
    expect((w.get('[data-test=host]').element as HTMLInputElement).value).toBe('192.168.0.10')
    expect((w.get('[data-test=port]').element as HTMLInputElement).value).toBe('35000')
    expect(w.text()).toContain('V-LINK')
    expect(w.find('[data-test=adapter]').exists()).toBe(false)
    expect(w.find('[data-test=synthetic-badge]').exists()).toBe(false)
    expect(w.get('[data-test=safety]').text()).toContain('does not certify roadworthiness')
  })

  it('shows the adapter error and stays disconnected when the connection fails', async () => {
    mockFetch({
      ...baseRoutes(),
      'POST /api/connect': () => ({ status: 502, body: { detail: 'cannot reach adapter at 192.168.0.10:35000. Check the V-LINK Wi-Fi.' } }),
    })
    const w = await mountApp()
    await w.get('[data-test=connect]').trigger('click')
    await flushPromises()
    expect(w.get('[data-test=error]').text()).toContain('cannot reach adapter at 192.168.0.10:35000')
    expect(w.get('[data-test=conn-badge]').text()).toBe('disconnected')
  })

  it('shows adapter identity, protocol and a synthetic badge once connected to the simulator', async () => {
    const calls = mockFetch({ ...baseRoutes(), 'POST /api/connect': connected })
    const w = await mountApp()
    await w.get('[data-test=mode-sim]').setValue(true)
    await w.get('[data-test=connect]').trigger('click')
    await flushPromises()
    expect(calls.at(-1)).toMatchObject({ url: '/api/connect', body: { mode: 'simulator', scenario: 'rough_idle' } })
    expect(w.get('[data-test=conn-badge]').text()).toBe('connected')
    expect(w.get('[data-test=adapter]').text()).toContain('ELM327 v2.3')
    expect(w.get('[data-test=adapter]').text()).toContain('ISO 15765-4 (CAN 11/500)')
    expect(w.get('[data-test=adapter]').text()).toContain('0A: not supported (no_data)')
    expect(w.get('[data-test=synthetic-badge]').text()).toContain('SYNTHETIC')
  })

  it('reports reconnecting and an unreachable local service', async () => {
    mockFetch(baseRoutes({ ...disconnected, transport: { kind: 'tcp', state: 'reconnecting', last_error: 'reset' } }))
    const w = await mountApp()
    expect(w.get('[data-test=conn-badge]').text()).toBe('reconnecting')
    expect(w.get('[data-test=conn-state]').text()).toContain('last error: reset')
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('network') }))
    await useWorkbench().connect({ mode: 'real' })
    await flushPromises()
    expect(w.get('[data-test=error]').text()).toContain('local workbench service is not reachable')
  })
})

describe('vehicle profile selection', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('lists both profiles and switches the active one from the header', async () => {
    const calls = mockFetch({ ...baseRoutes(), 'PUT /api/profiles/active': (b: any) => ({ body: { active: b.id } }) })
    const w = await mountApp()
    const select = w.get('[data-test=profile-select]')
    expect(select.findAll('option').map((o) => o.text())).toEqual([miniProfile.name, `${porscheProfile.name} (in progress)`])
    await select.setValue(porscheProfile.id)
    await flushPromises()
    expect(calls.find((c) => c.method === 'PUT')).toMatchObject({ url: '/api/profiles/active', body: { id: porscheProfile.id } })
    expect(useWorkbench().activeProfileId).toBe(porscheProfile.id)
    expect(calls.at(-2)?.url).toBe(`/api/sessions?vehicle=${porscheProfile.id}`)
  })

  it('vehicle screen shows unconfirmed identity and manufacturer values as not measured', async () => {
    mockFetch(baseRoutes())
    const w = await mountApp()
    await w.get('[data-test=tab-vehicle]').trigger('click')
    expect(w.get('[data-test=identity-note]').text()).toContain('NOT confirmed')
    expect(w.text()).toContain('variant unconfirmed')
    expect(w.text()).toContain('EGR cooler replaced: owner-reported, not independently verified')
    const mfr = w.get('[data-test=manufacturer]')
    expect(mfr.text()).toContain('DPF soot load')
    expect(mfr.text()).toContain('not supported / not measured')
    expect((w.get('[data-test=confirm-engine]').element as HTMLInputElement).value).toBe('')
    expect(w.find(`[data-test=profile-${porscheProfile.id}]`).text()).toContain('K-line')
  })
})

describe('no control surface', () => {
  it('has no clear/reset/regeneration/raw-command control anywhere', async () => {
    vi.unstubAllGlobals()
    mockFetch({ ...baseRoutes(connected), 'GET /api/recording/plan': { fast: [], slow: [], unsupported: [] } })
    const w = await mountApp()
    for (const tab of ['connection', 'vehicle', 'faults', 'live', 'monitoring', 'sessions', 'findings']) {
      await w.get(`[data-test=tab-${tab}]`).trigger('click')
      await flushPromises()
      const controls = w.findAll('button, a, input, select').map((b) => `${b.text()} ${b.attributes('placeholder') ?? ''}`.toLowerCase())
      for (const c of controls) expect(c).not.toMatch(/clear|reset|erase|regenerat|actuat|command|coding|adapt/)
    }
  })
})
