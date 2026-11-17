import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import type { Status } from '../types'
import { baseRoutes, connected, disconnected, mockFetch } from './helpers'

/** A tiny stateful backend: GET /api/status always reflects the last lifecycle result. */
function backend(connectResult: () => { status?: number; body: unknown }, after: Status) {
  let current: Status = disconnected
  const calls = mockFetch({
    ...baseRoutes(),
    'GET /api/status': () => ({ body: current }),
    'POST /api/connect': () => {
      const r = connectResult()
      current = (r.status ?? 200) === 200 ? after : disconnected
      return r
    },
    'POST /api/disconnect': () => {
      current = disconnected
      return { body: disconnected }
    },
  })
  return calls
}

async function mountApp() {
  const w = mount(App, { global: { plugins: [createPinia()] } })
  await flushPromises()
  return w
}

const disabled = (w: ReturnType<typeof mount>, sel: string) => w.get(sel).attributes('disabled') !== undefined

describe('connect publishes/uses the final idle status (follow-up P1-3)', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('after connect: idle, Disconnect enabled and works; repeatable', async () => {
    backend(() => ({ body: connected }), connected)
    const w = await mountApp()
    for (let i = 0; i < 3; i++) {
      await w.get('[data-test=connect]').trigger('click')
      await flushPromises()
      expect(w.get('[data-test=conn-badge]').text()).toBe('connected')
      expect(disabled(w, '[data-test=disconnect]')).toBe(false)
      expect(w.find('[data-test=disconnect-hint]').exists()).toBe(false)
      await w.get('[data-test=disconnect]').trigger('click')
      await flushPromises()
      expect(w.get('[data-test=conn-badge]').text()).toBe('disconnected')
      expect(disabled(w, '[data-test=connect]')).toBe(false)
    }
  })

  it('a connect response still naming the operation is replaced by the authoritative status', async () => {
    const calls = backend(() => ({ body: { ...connected, operation: 'connect' } }), connected)
    const w = await mountApp()
    await w.get('[data-test=connect]').trigger('click')
    await flushPromises()
    expect(calls.at(-1)).toMatchObject({ method: 'GET', url: '/api/status' })
    expect(disabled(w, '[data-test=disconnect]')).toBe(false)
    expect(w.find('[data-test=disconnect-hint]').exists()).toBe(false)
  })

  it('failed connect: disconnected, no busy state, Connect enabled again', async () => {
    backend(() => ({ status: 502, body: { detail: 'adapter initialisation failed: incomplete adapter reply to ATE0' } }), connected)
    const w = await mountApp()
    await w.get('[data-test=connect]').trigger('click')
    await flushPromises()
    expect(w.get('[data-test=error]').text()).toContain('ATE0')
    expect(w.get('[data-test=conn-badge]').text()).toBe('disconnected')
    expect(disabled(w, '[data-test=connect]')).toBe(false)
    expect(disabled(w, '[data-test=profile-select]')).toBe(false)
  })
})
