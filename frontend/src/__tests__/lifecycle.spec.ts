import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import { useWorkbench } from '../stores/workbench'
import type { Status } from '../types'
import { baseRoutes, connected, miniProfile, mockFetch, porscheProfile } from './helpers'

const recordingStatus: Status = {
  ...connected,
  operation: 'recording',
  recording: { session_id: 's1', started_at: 0, duration_s: 60, fast: [12], slow: [], unsupported: [] },
}

async function mountApp() {
  const w = mount(App, { global: { plugins: [createPinia()] } })
  await flushPromises()
  return w
}

describe('lifecycle guards mirror the backend (P0-2)', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('while recording: no profile switch, no disconnect, with the reason shown', async () => {
    mockFetch({ ...baseRoutes(recordingStatus), 'GET /api/recording/plan': { fast: [], slow: [], unsupported: [] } })
    const w = await mountApp()
    expect(w.get('[data-test=profile-select]').attributes('disabled')).toBeDefined()
    expect(w.get('[data-test=disconnect]').attributes('disabled')).toBeDefined()
    expect(w.get('[data-test=disconnect-hint]').text()).toContain('Stop the recording first')
    await w.get('[data-test=tab-vehicle]').trigger('click')
    expect(w.get(`[data-test=profile-${porscheProfile.id}]`).attributes('disabled')).toBeDefined()
    expect(w.get('[data-test=profile-lock]').text()).toContain('Stop the recording')
  })

  it('while a scan runs: disconnect waits for it', async () => {
    mockFetch(baseRoutes({ ...connected, operation: 'scan' }))
    const w = await mountApp()
    expect(w.get('[data-test=disconnect]').attributes('disabled')).toBeDefined()
    expect(w.get('[data-test=disconnect-hint]').text()).toContain('running scan')
  })

  it('connected and idle: disconnect allowed, switching vehicle requires disconnect first', async () => {
    mockFetch(baseRoutes(connected))
    const w = await mountApp()
    expect(w.get('[data-test=disconnect]').attributes('disabled')).toBeUndefined()
    expect(w.find('[data-test=disconnect-hint]').exists()).toBe(false)
    const select = w.get('[data-test=profile-select]')
    expect(select.attributes('disabled')).toBeDefined()
    expect(select.attributes('title')).toContain('Disconnect before switching vehicle')
  })

  it('a 409 from the backend is shown and the active profile stays unchanged', async () => {
    mockFetch({
      ...baseRoutes(),
      'PUT /api/profiles/active': () => ({ status: 409, body: { detail: 'cannot switch vehicle profile: scan in progress (stop it first)' } }),
    })
    const w = await mountApp()
    await useWorkbench().setProfile(porscheProfile.id)
    await flushPromises()
    expect(w.get('[data-test=error]').text()).toContain('scan in progress')
    expect(useWorkbench().activeProfileId).toBe(miniProfile.id)
  })
})

describe('generic OBD vs manufacturer data vs hardware validation (P1-4)', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('shows three separate indicators and never presents owner-reported BMW codes as read', async () => {
    mockFetch(baseRoutes())
    const w = await mountApp()
    await w.get('[data-test=tab-vehicle]').trigger('click')
    const mini = w.get(`[data-test=caps-${miniProfile.id}]`)
    expect(mini.get('[data-test=cap-generic]').text()).toBe('Generic OBD: ready')
    expect(mini.get('[data-test=cap-manufacturer]').text()).toBe('Manufacturer DDE data: not read by this tool')
    expect(mini.get('[data-test=cap-hardware]').text()).toContain('NOT hardware-validated')
    const porsche = w.get(`[data-test=caps-${porscheProfile.id}]`)
    expect(porsche.get('[data-test=cap-generic]').text()).toContain('in progress')
    expect(porsche.get('[data-test=cap-manufacturer]').text()).toContain('DME data: not read')
    expect(w.text()).not.toMatch(/\bcomplete\b/i)
    const code = w.get('[data-test=owner-code]').text()
    expect(code).toContain('4862')
    expect(code).toContain('not read from the car')
    expect(w.get('[data-test=mfr-note]').text()).toContain('not read by this tool')
  })
})
