import { vi } from 'vitest'
import type { Profile, Status } from '../types'

export const miniProfile: Profile = {
  id: 'mini-countryman-r60-diesel', name: 'MINI Countryman R60 2.0 Diesel (BMW DDE)', make: 'MINI', model: 'Countryman', engine: '2.0 diesel',
  fuel: 'diesel', status: 'complete', identity_note: 'Owner-reported. NOT confirmed until read from the car.', protocol_note: 'CAN expected',
  expected_protocol_families: ['can'],
  ecus: [{ id: 'bmw-dde-unconfirmed', name: 'BMW DDE', family: 'DDE', variant_confirmed: false, variant_note: 'unknown', generic_obd_note: '' }],
  priority_pids: [], recording_profiles: [{ id: 'idle', name: 'Idle investigation' }], event_labels: ['rough idle started', 'MIL on'],
  first_run_checklist: ['Park safely'], scenarios: [{ name: 'rough_idle', description: 'synthetic' }],
  manufacturer: [{ id: 'bmw-dde', ecu: 'BMW DDE', enabled: false, verified: false, access_requirements: 'needs a BMW tester',
    parameters: [{ key: 'dpf_soot_load', name: 'DPF soot load', unit: null, status: 'unavailable', value: null, reason: 'unverified' }] }],
  owner_reported: { symptoms: ['idle fluctuation'], manufacturer_fault_codes: [{ code: '4862', system: 'BMW DDE', source: 'owner report' }],
    repairs: [{ description: 'EGR cooler replaced', verified: false }], notes: '' },
}
export const porscheProfile: Profile = {
  ...miniProfile, id: 'porsche-boxster-986-mk2', name: 'Porsche Boxster 986 Mk2 (Bosch ME7.8)', status: 'incremental',
  expected_protocol_families: ['kline'], scenarios: [{ name: 'porsche_healthy_idle', description: 'synthetic' }],
  owner_reported: { symptoms: [], manufacturer_fault_codes: [], repairs: [], notes: '' },
}
export const disconnected: Status = {
  read_only: true, connected: false, source: 'none', synthetic: false, scenario: null, transport: null, adapter: null,
  capabilities: null, recording: null, vehicle_profile: miniProfile.id,
}
export const connected: Status = {
  ...disconnected, connected: true, source: 'simulator', synthetic: true, scenario: 'rough_idle',
  transport: { kind: 'vgate-wifi-tcp', state: 'connected', last_error: null, host: '127.0.0.1', port: 1, reconnects: 0 },
  adapter: { identity: 'ELM327 v2.3', device_description: 'SIM', voltage: '14.1V', protocol_number: 'A6',
    protocol_name: 'AUTO, ISO 15765-4 (CAN 11/500)', is_can: true, init_log: [{ command: 'ATZ', response: 'ELM327 v2.3', status: 'ok' }] },
  capabilities: { primary_ecu: '7E8', modes: { '01': 'supported', '0A': 'no_data' },
    ecus: [{ ecu: '7E8', mode01_pids: [12, 16], mode02_pids: [], mode06_mids: [], mode09_pids: [] }] },
}

type Handler = (body: any) => { status?: number; body: unknown }
export interface Call { method: string; url: string; body: any }

/** Minimal fetch mock: routes are "METHOD /path" (query string ignored unless given). */
export function mockFetch(routes: Record<string, Handler | unknown>) {
  const calls: Call[] = []
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(init.body as string) : undefined
    calls.push({ method, url, body })
    const route = routes[`${method} ${url}`] ?? routes[`${method} ${url.split('?')[0]}`]
    if (route === undefined) return new Response(JSON.stringify({ detail: `no mock for ${method} ${url}` }), { status: 404 })
    const out = typeof route === 'function' ? (route as Handler)(body) : { body: route }
    return new Response(JSON.stringify(out.body), { status: out.status ?? 200, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fn)
  return calls
}

export const baseRoutes = (status: Status = disconnected) => ({
  'GET /api/status': status,
  'GET /api/profiles': { active: miniProfile.id, profiles: [miniProfile, porscheProfile] },
  'GET /api/vehicle': { confirmed: { model: '', engine: '', ecu: '', model_year: '' }, owner_reported: miniProfile.owner_reported },
  'GET /api/sessions': [],
  'GET /api/fixtures': [{ name: 'rough-idle.jsonl', bytes: 10 }],
})
