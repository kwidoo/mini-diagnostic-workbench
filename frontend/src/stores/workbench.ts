import { defineStore } from 'pinia'
import { api } from '../api'
import type {
  AirPathTrend, Annotation, Comparison, Finding, HistoryRow, Inventory, LiveSignal, MonitoringSummary, MonitorState, Plan, Profile, Reading,
  Series, SessionDetail, SessionSummary, Status, VehicleRecord,
} from '../types'

export const STALE_AFTER_MS = 3000
const LIVE_WINDOW_S = 120
/** Live key of the adapter-measured supply voltage (ATRV); ECU PIDs use their PID number. */
export const ADAPTER_VOLTAGE_KEY = -1
const MAX_LIVE_EVENTS = 50

export interface RecordOptions { untilStop?: boolean; maxDurationS?: number | null; driveType?: string | null }
export interface LiveEvent { at: number; label: string; event_type: string | null; origin: string; note: string; t: number | null }

interface State {
  status: Status | null
  profiles: Profile[]
  activeProfileId: string
  vehicle: VehicleRecord | null
  error: string | null
  busy: string | null
  sessions: SessionSummary[]
  lastScan: SessionDetail | null
  inventory: Inventory | null
  plan: Plan | null
  live: Record<number, LiveSignal>
  liveSessionId: string | null
  liveMarkers: { t: number; label: string }[]
  wsState: 'closed' | 'connecting' | 'open'
  current: SessionDetail | null
  currentSeries: Series | null
  findings: Finding[]
  findingsFor: string | null
  comparison: Comparison | null
  fixtures: { name: string; bytes: number }[]
  /** timeline events of the running recording (manual, observed link/engine events) */
  liveEvents: LiveEvent[]
  monitoring: MonitoringSummary | null
  monitoringSeries: Series | null
  history: HistoryRow[]
  /** multi-session air-path statistics per cohort (real / synthetic never pooled) */
  airPathTrends: AirPathTrend[]
}

let socket: WebSocket | null = null

export const useWorkbench = defineStore('workbench', {
  state: (): State => ({
    status: null, profiles: [], activeProfileId: '', vehicle: null, error: null, busy: null, sessions: [],
    lastScan: null, inventory: null, plan: null, live: {}, liveSessionId: null, liveMarkers: [], wsState: 'closed',
    current: null, currentSeries: null, findings: [], findingsFor: null, comparison: null, fixtures: [],
    liveEvents: [], monitoring: null, monitoringSeries: null, history: [], airPathTrends: [],
  }),
  getters: {
    profile: (s): Profile | null => s.profiles.find((p) => p.id === s.activeProfileId) ?? null,
    connected: (s): boolean => !!s.status?.connected,
    recording: (s) => s.status?.recording ?? null,
    monitor: (s): MonitorState | null => s.status?.recording?.monitor ?? null,
    /** Why switching to another vehicle profile is refused right now (mirrors the backend's 409 rules). */
    profileLockReason: (s): string | null => {
      const st = s.status
      if (st?.recording) return 'Stop the recording before switching vehicle.'
      if (st?.operation) return `Wait for the running ${st.operation} to finish before switching vehicle.`
      if (st?.connected) return 'Disconnect before switching vehicle: the other vehicle must be probed on a fresh connection.'
      return null
    },
    /** Why disconnect is refused right now: an operation must be stopped / finish first. */
    disconnectLockReason: (s): string | null => {
      const st = s.status
      if (st?.recording) return 'Stop the recording first, then disconnect.'
      if (st?.operation) return `Wait for the running ${st.operation} to finish, then disconnect.`
      return null
    },
    connectionState: (s): string => {
      if (!s.status) return 'unknown'
      if (s.status.connected) return 'connected'
      return s.status.transport?.state === 'reconnecting' ? 'reconnecting' : 'disconnected'
    },
  },
  actions: {
    async run<T>(what: string, fn: () => Promise<T>): Promise<T | undefined> {
      this.busy = what
      this.error = null
      try {
        return await fn()
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
        return undefined
      } finally {
        this.busy = null
      }
    },
    async init() {
      await this.run('loading', async () => {
        this.status = await api.get<Status>('/api/status')
        const p = await api.get<{ active: string; profiles: Profile[] }>('/api/profiles')
        this.profiles = p.profiles
        this.activeProfileId = p.active
        this.vehicle = await api.get<VehicleRecord>('/api/vehicle')
        await this.loadSessions()
      })
    },
    async refreshStatus() {
      try {
        this.status = await api.get<Status>('/api/status')
      } catch (e) {
        this.error = e instanceof Error ? e.message : String(e)
      }
    },
    async setProfile(id: string) {
      await this.run('switching vehicle', async () => {
        const r = await api.put<{ active: string }>('/api/profiles/active', { id })
        this.activeProfileId = r.active
        this.vehicle = await api.get<VehicleRecord>('/api/vehicle')
        this.lastScan = null
        this.inventory = null
        this.plan = null
        this.current = null
        this.currentSeries = null
        this.comparison = null
        this.findings = []
        await this.loadSessions()
        await this.refreshStatus()
      })
    },
    async saveVehicle(record: VehicleRecord) {
      await this.run('saving', async () => {
        this.vehicle = await api.put<VehicleRecord>('/api/vehicle', record)
      })
    },
    async connect(body: { mode: string; host?: string; port?: number; scenario?: string }) {
      const ok = await this.run('connecting', async () => {
        this.status = await api.post<Status>('/api/connect', body)
        this.plan = null
        this.inventory = null
        return true
      })
      await this.settleStatus(ok)
    },
    async disconnect() {
      const ok = await this.run('disconnecting', async () => {
        this.status = await api.post<Status>('/api/disconnect')
        return true
      })
      await this.settleStatus(ok)
    },
    /** After connect/disconnect the response is the final state. If the request failed, or the
     *  snapshot still names a running operation, fetch the authoritative status so Connect /
     *  Disconnect / profile controls never stay locked by a phantom busy state. */
    async settleStatus(ok: boolean | undefined) {
      if (!ok || this.status?.operation) await this.refreshStatus()
    },
    async scan(readVin: boolean) {
      await this.run('scanning', async () => {
        this.lastScan = await api.post<SessionDetail>('/api/scan', { read_vin: readVin })
        await this.loadSessions()
      })
    },
    async runInventory() {
      await this.run('reading inventory', async () => {
        this.inventory = await api.post<Inventory>('/api/inventory', {})
        await this.loadSessions()
      })
    },
    async loadPlan(profile: string) {
      await this.run('planning', async () => {
        this.plan = await api.get<Plan>(`/api/recording/plan?profile=${encodeURIComponent(profile)}`)
      })
    },
    async startRecording(durationS: number, profile: string, label: string, opts: RecordOptions = {}) {
      await this.run('starting recording', async () => {
        this.live = {}
        this.liveMarkers = []
        this.liveEvents = []
        const body: Record<string, unknown> = { duration_s: durationS, profile, label }
        if (opts.untilStop) {
          body.until_stop = true
          if (opts.maxDurationS) body.max_duration_s = opts.maxDurationS
        }
        if (opts.driveType) body.drive_type = opts.driveType
        const r = await api.post<{ recording: Status['recording'] }>('/api/recording/start', body)
        this.liveSessionId = r.recording?.session_id ?? null
        await this.refreshStatus()
      })
    },
    async stopRecording() {
      await this.run('stopping', async () => {
        await api.post('/api/recording/stop')
      })
    },
    async annotate(sessionId: string, label: string, note = '') {
      await this.run('adding marker', async () => {
        await api.post(`/api/sessions/${sessionId}/annotations`, { label, note })
        const first = Math.min(...Object.values(this.live).map((s) => s.first), Infinity)
        if (sessionId === this.liveSessionId && Number.isFinite(first)) {
          const latest = Math.max(...Object.values(this.live).map((s) => s.points.at(-1)?.[0] ?? 0))
          this.liveMarkers.push({ t: latest, label })
        }
      })
    },
    /** Predefined event (fan_running, battery_warning, ...). Works while recording, after the engine
     *  stopped and after the recording ended (the backend then uses wall-clock time). Always origin 'manual'. */
    async markEvent(sessionId: string, eventType: string, note = '') {
      await this.run('adding event', async () => {
        const a = await api.post<Annotation>(`/api/sessions/${sessionId}/annotations`, { event_type: eventType, note })
        if (sessionId === this.liveSessionId) {
          const latest = Math.max(0, ...Object.values(this.live).map((s) => s.points.at(-1)?.[0] ?? 0))
          this.liveMarkers.push({ t: latest, label: a.label })
        }
        this.pushLiveEvent(a, Date.now())
        if (this.monitoring?.session_id === sessionId) await this.loadMonitoring(sessionId)
      })
    },
    async resumeEcuPolling() {
      await this.run('asking the ECU', async () => {
        await api.post('/api/recording/resume-ecu')
      })
    },
    async loadMonitoring(id: string) {
      await this.run('loading monitoring', async () => {
        this.monitoring = await api.get<MonitoringSummary>(`/api/sessions/${id}/monitoring`)
        this.monitoringSeries = await api.get<Series>(`/api/sessions/${id}/samples`)
      })
    },
    async loadHistory() {
      await this.run('loading history', async () => {
        const q = this.activeProfileId ? `?vehicle=${encodeURIComponent(this.activeProfileId)}` : ''
        const h = await api.get<{ sessions: HistoryRow[]; air_path_trends?: AirPathTrend[] }>(`/api/monitoring/history${q}`)
        this.history = h.sessions
        this.airPathTrends = h.air_path_trends ?? []
      })
    },
    async setDriveType(id: string, driveType: string | null) {
      await this.run('saving label', async () => {
        await api.put(`/api/sessions/${id}/drive-type`, { drive_type: driveType })
        await this.loadSessions()
        if (this.monitoring?.session_id === id) await this.loadMonitoring(id)
      })
    },
    pushLiveEvent(a: Annotation, now: number) {
      this.liveEvents.unshift({ at: now, label: a.label, event_type: a.event_type ?? null, origin: a.origin ?? 'manual', note: a.note, t: a.t_mono })
      this.liveEvents.splice(MAX_LIVE_EVENTS)
    },
    async loadSessions() {
      const q = this.activeProfileId ? `?vehicle=${encodeURIComponent(this.activeProfileId)}` : ''
      this.sessions = await api.get<SessionSummary[]>(`/api/sessions${q}`)
    },
    async openSession(id: string) {
      await this.run('loading session', async () => {
        this.current = await api.get<SessionDetail>(`/api/sessions/${id}`)
        this.currentSeries = await api.get<Series>(`/api/sessions/${id}/samples`)
      })
    },
    async loadFindings(id: string) {
      await this.run('analysing', async () => {
        this.findings = await api.get<Finding[]>(`/api/sessions/${id}/findings`)
        this.findingsFor = id
      })
    },
    async compare(a: string, b: string) {
      await this.run('comparing', async () => {
        this.comparison = await api.get<Comparison>(`/api/compare?a=${a}&b=${b}`)
      })
    },
    async loadFixtures() {
      await this.run('loading fixtures', async () => {
        this.fixtures = await api.get<{ name: string; bytes: number }[]>('/api/fixtures')
      })
    },
    async replay(fixture: string) {
      await this.run('replaying', async () => {
        const s = await api.post<SessionDetail>('/api/replay', { fixture })
        await this.loadSessions()
        await this.openSession(s.id)
      })
    },
    // -- live stream ---------------------------------------------------------
    handleEvent(ev: Record<string, any>, now = Date.now()) {
      if (ev.type === 'batch') return (ev.events as Record<string, any>[]).forEach((e) => this.handleEvent(e, now))
      if (ev.type === 'status') this.status = ev.status
      if (ev.type === 'recording') {
        if (ev.state === 'started') this.liveSessionId = ev.session_id
        void this.refreshStatus().then(() => {
          if (ev.state === 'stopped') void this.loadSessions()
        })
      }
      if (ev.type === 'monitor' && this.status?.recording && this.status.recording.session_id === ev.session_id) {
        this.status.recording.monitor = ev.monitor as MonitorState
      }
      if (ev.type === 'annotation' && ev.session_id === this.liveSessionId) {
        const a = ev.annotation as Annotation
        // manual markers are already shown by markEvent / annotate; add what the recorder observed
        if (a.origin && a.origin !== 'manual') {
          this.pushLiveEvent(a, now)
          const latest = Math.max(0, ...Object.values(this.live).map((s) => s.points.at(-1)?.[0] ?? 0))
          this.liveMarkers.push({ t: latest, label: a.label })
        }
      }
      if (ev.type === 'reading') {
        const r = ev.reading as Reading
        const primary = this.status?.capabilities?.primary_ecu
        if (r.mode !== 0 && r.ecu && primary && r.ecu !== primary) return
        const key = r.mode === 0 ? ADAPTER_VOLTAGE_KEY : r.pid
        const sig = (this.live[key] ??= {
          pid: key, name: r.name, unit: r.unit, value: null, quality: 'none', at: 0, points: [], attempts: 0, ok: 0, first: r.t_mono,
        })
        sig.attempts += 1
        sig.quality = r.quality
        sig.at = now
        if ((r.quality === 'ok' || r.quality === 'raw_only') && r.value !== null) {
          sig.ok += 1
          sig.value = r.value
          const t0 = Math.min(...Object.values(this.live).map((s) => s.first))
          sig.points.push([r.t_mono - t0, r.value])
          const cutoff = r.t_mono - t0 - LIVE_WINDOW_S
          while (sig.points.length && sig.points[0][0] < cutoff) sig.points.shift()
        } else if (r.quality !== 'ok') {
          sig.value = null // a failed read never leaves the old number on screen as if it were current
        }
      }
    },
    openSocket() {
      if (socket || typeof WebSocket === 'undefined') return
      this.wsState = 'connecting'
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${proto}://${location.host}/ws/live`)
      socket.onopen = () => (this.wsState = 'open')
      socket.onmessage = (m) => this.handleEvent(JSON.parse(m.data))
      socket.onclose = () => {
        socket = null
        this.wsState = 'closed'
        setTimeout(() => this.openSocket(), 2000)
      }
    },
  },
})

/** A value counts as stale after 3 of its own (effective) sampling intervals, at least STALE_AFTER_MS. */
export function staleAfterMs(monitor: MonitorState | null, pid: number): number {
  if (!monitor) return STALE_AFTER_MS
  const key = pid === ADAPTER_VOLTAGE_KEY ? 'adapter:ATRV' : `01:${pid.toString(16).toUpperCase().padStart(2, '0')}`
  const item = monitor.items[key]
  return item ? Math.max(STALE_AFTER_MS, 3000 * item.interval_s) : STALE_AFTER_MS
}

export function observedHz(sig: LiveSignal): number {
  const span = (sig.points.at(-1)?.[0] ?? 0) - (sig.points[0]?.[0] ?? 0)
  return span > 0 ? (sig.points.length - 1) / span : 0
}
