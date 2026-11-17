export interface PidRef { pid: number; hex: string; name: string; unit?: string | null }
export interface EcuCaps { ecu: string; mode01_pids: number[]; mode02_pids: number[]; mode06_mids: number[]; mode09_pids: number[] }
export interface Capabilities { ecus: EcuCaps[]; primary_ecu: string | null; modes: Record<string, string> }
export interface AdapterInfo {
  identity: string | null; device_description: string | null; voltage: string | null
  protocol_number: string | null; protocol_name: string | null; is_can: boolean
  init_log: { command: string; response: string; status: string }[]
}
/** Live state of a continuous-monitoring recording (link/ECU state, effective sampling intervals). */
export interface MonitorState {
  state: 'running' | 'ecu_unresponsive' | 'adapter_unreachable'
  engine: 'unknown' | 'running' | 'stopped'
  mil_on: boolean | null
  base_intervals: TierIntervals
  effective_intervals: TierIntervals
  capacity_rps: number | null
  suspended: string[]
  items: Record<string, { tier: Tier; pid: number | null; interval_s: number }>
  resume_probe_s: number | null
}
export type Tier = 'fast' | 'medium' | 'slow'
export interface TierIntervals { fast: number; medium: number; slow: number }
export interface Recording {
  session_id: string; started_at: number
  /** null: runs until stopped without a safety limit */
  duration_s: number | null
  fast: number[]; slow: number[]; unsupported: number[]; medium?: number[]
  until_stop?: boolean; profile?: string | null; monitoring?: boolean; monitor?: MonitorState
}
export interface Status {
  read_only: true; connected: boolean; source: string; synthetic: boolean; scenario: string | null
  transport: {
    kind: string; state: string; last_error: string | null; host?: string; port?: number; reconnects?: number
    synced?: boolean; resyncs?: number
  } | null
  adapter: AdapterInfo | null; capabilities: Capabilities | null; recording: Recording | null; vehicle_profile: string
  /** running workbench operation (scan | inventory | recording | connect | ...), null when idle */
  operation?: string | null
  /** profile the current connection was opened (and probed) for */
  connected_profile?: string | null
}
/** Kept deliberately separate: generic OBD readiness says nothing about manufacturer data or hardware validation. */
export interface ProfileCapabilities {
  generic_obd: 'ready' | 'incremental'; manufacturer_diagnostics: 'available' | 'not_available'; manufacturer_note: string
  hardware_validated: boolean; hardware_validation_note: string
}
export interface MfrParam { key: string; name: string; unit: string | null; status: string; value: null; reason: string }
export interface MfrExtension { id: string; ecu: string; enabled: boolean; verified: boolean; access_requirements: string; parameters: MfrParam[] }
export interface OwnerReported {
  symptoms: string[]; manufacturer_fault_codes: { code: string; system: string; source: string }[]
  repairs: { description: string; verified: boolean }[]; notes: string
}
export interface Profile {
  id: string; name: string; make: string; model: string; engine: string; fuel: string; status: string
  identity_note: string; protocol_note: string; expected_protocol_families: string[]
  ecus: { id: string; name: string; family: string; variant_confirmed: boolean; variant_note: string; generic_obd_note: string }[]
  priority_pids: PidRef[]; recording_profiles: { id: string; name: string; monitoring?: boolean; description?: string }[]; event_labels: string[]
  first_run_checklist: string[]; scenarios: { name: string; description: string }[]; manufacturer: MfrExtension[]
  /** predefined timeline events (MINI monitoring); empty for other vehicles */
  monitoring_events?: { id: string; label: string; hint: string }[]
  owner_reported: OwnerReported
  capabilities: ProfileCapabilities
}
export interface VehicleRecord { confirmed: Record<string, string>; owner_reported: OwnerReported }
export interface Reading {
  mode: number; pid: number; ecu: string | null; name: string; unit: string | null; value: number | null
  text: string | null; raw_hex: string; quality: string; detail: string | null; t_wall: number; t_mono: number; latency_ms: number
}
export interface Dtc { code: string; raw_hex: string; kind: string; ecu: string | null }
export interface FreezeFrame { status: string; dtc: string | null; dtc_raw_hex: string | null; ecu: string | null; readings: Reading[] }
export interface DtcScan {
  t_wall: number; phase: string; dtcs: Dtc[]; mode_status: Record<string, string>
  mil_on: boolean | null; dtc_count_reported: number | null; freeze_frame: FreezeFrame | null
}
export interface Metric {
  pid: number; name: string; attempts: number; ok: number; dropped: number; error_rate: number; actual_hz: number
  latency_ms_mean: number | null; latency_ms_p95: number | null; latency_ms_max: number | null; max_gap_s: number | null; gaps: number
}
export interface SessionSummary {
  id: string; kind: string; vehicle_profile: string; source: string; synthetic: boolean; label: string
  started_at: number; ended_at: number | null; aborted?: string | null; scenario?: string | null
  recording_profile?: string | null; monitoring?: boolean; drive_type?: string | null
}
export type Origin = 'manual' | 'observed' | 'inferred'
export interface Annotation {
  t_wall: number; t_mono: number | null; label: string; note: string
  event_type?: string | null; origin?: Origin; confidence?: string | null; evidence?: Record<string, unknown>
}
export interface SessionDetail extends SessionSummary {
  meta: {
    metrics?: Metric[]; selected?: { fast: number[]; slow: number[]; unsupported: number[] }
    vehicle_info?: { vin: string | null; vin_status: string; calibration_ids: { ecu: string; text: string }[] }
    aborted?: string; adapter?: AdapterInfo | null
  }
  scans: DtcScan[]; annotations: Annotation[]
}
export type Point = [number, number]
export interface Signal {
  pid: number; hex: string; name: string; unit: string | null; decoded: boolean; points: Point[]; failed: [number, string][]
  /** 'adapter' for the ATRV supply voltage (pid -1), 'ecu' otherwise */
  provenance?: 'ecu' | 'adapter'
}
export interface Series { session_id: string; signals: Signal[]; annotations: ({ t: number | null } & Annotation)[] }
export interface Evidence { text: string; values: Record<string, unknown> }
export interface Finding {
  id: string; title: string; hypothesis: string; explanation: string; strength: 'insufficient' | 'suggestive' | 'strong'
  supporting: Evidence[]; contradicting: Evidence[]; caveats: string[]; conditions: string[]; next_steps: string[]
}
export interface Comparison {
  a: SessionDetail; b: SessionDetail; series_a: Series; series_b: Series
  dtc_diff: { only_a: string[]; only_b: string[]; both: string[] }
  same_vehicle_profile: boolean; mixed_synthetic_and_real: boolean
}
export interface Inventory extends SessionDetail { readings: Reading[]; unsupported_priority: PidRef[]; raw_only: PidRef[] }
export interface Plan {
  fast: PidRef[]; slow: PidRef[]; unsupported: PidRef[]; medium?: PidRef[]
  tiers?: TierIntervals | null; monitoring?: boolean; adapter_voltage?: string; description?: string
}
/** Status words shared by the monitoring views. */
export type DataStatus = 'measured' | 'raw_only' | 'not_available' | 'no_valid_answer'
export interface SignalStatus {
  pid: number | null; hex: string; name: string; tier: Tier | null; status: DataStatus; provenance: 'ECU' | 'adapter'
  ok: number; raw: number; failed: number; reason?: string | null
}
export interface DetectedEvent {
  type: string; origin: 'inferred'; status: 'possible'; confidence: 'low' | 'moderate'; text: string
  t_start_rel_s?: number; t_end_rel_s?: number; t_rel_s?: number; indicators?: string[]; fan_runs_after_s?: (number | null)[]
}
export interface FanRun {
  t_start_rel_s: number | null; t_end_rel_s: number | null; duration_s: number | null; complete: boolean
  after_shutdown: boolean; seconds_after_shutdown: number | null; shutdown_origin: Origin | null; marked_unusually_loud: boolean
}
export interface VoltageSource {
  status: 'measured' | 'not_measured'; provenance: string
  at_start?: { value: number; t_rel_s: number }
  engine_running?: { n: number; min: number; median: number; max: number } | null
  engine_off?: { n: number; min: number; median: number; max: number } | null
  no_charging_rise_seen?: boolean
  low_while_running?: { screen_v: number; samples: number; share: number | null; min_v: number | null }
  drops?: { t_rel_s: number; from_v: number; to_v: number; drop_v: number; context: string | null }[]
  shutdowns?: {
    t_rel_s: number; origin: Origin; last_before: { value: number; age_s: number } | null
    after: { n: number; min: number; median: number; max: number } | null; trend_v_per_h: number | null
  }[]
  points?: Point[]
}
export interface MonitoringSummary {
  session_id: string; is_monitoring_session: boolean; synthetic: boolean
  drive: { running_s: number; moving_share: number | null; max_speed_kmh: number | null; mean_speed_moving_kmh: number | null; drive_type_user_label: string | null }
  engine_runs: { t_start_rel_s: number; t_end_rel_s: number; duration_s: number; start_observed: boolean; stop_observed: boolean }[]
  shutdowns: { t_rel_s: number; origin: Origin; basis: string; snapshot?: Record<string, unknown> | null }[]
  fan: { runs: FanRun[]; fan_stopped_without_running: number[]; source: string }
  voltage: {
    ecu: VoltageSource; adapter: VoltageSource; caveat: string
    battery_warnings: { event: string; t_rel_s: number; origin: Origin; engine_running: boolean; nearby: Record<string, { min: number; max: number; n: number } | null>; note: string }[]
  }
  coolant: {
    status: string; max_running?: number | null; usual_warm_c?: number | null; usual_warm_sessions?: number; running_s?: number
    first?: { value: number; t_rel_s: number }; detected: DetectedEvent[]; limitations?: string[]
    after_shutdown?: { t_rel_s: number; origin: Origin; max_rise_c: number | null; measured_for_s: number }[]
  }
  dpf: {
    availability: { group: 'wanted' | 'standard_pids' | 'manufacturer'; items: { key?: string; hex?: string; name: string; status: DataStatus; via?: string; reason?: string; samples?: number }[] }[]
    possible_regenerations: DetectedEvent[]; possible_interrupted_regenerations: DetectedEvent[]
    user_suspected: { t_rel_s: number; note: string; origin: Origin }[]; limitations: string[]; confirmed_regenerations: never[]
  }
  detected_events: DetectedEvent[]
  events: { t_rel_s: number; label: string; event_type: string | null; origin: Origin; confidence: string | null; note: string }[]
  link: { type: string; t_rel_s: number; gap_s: number | null; note: string }[]
  signals: SignalStatus[]
  sampling: {
    base_intervals: TierIntervals | null; final_effective_intervals: TierIntervals | null
    adjustments: { t_rel_s: number; capacity_rps: number; scale: { fast: number; low: number }; effective_intervals: TierIntervals }[]
    time_in_state_s: Record<string, number> | null
    signals: (Metric & { mode?: number; tier: Tier | null; target_hz_base: number | null; target_hz_final: number | null })[]
  }
  observed_capabilities: Record<string, string | number> | null
}
export interface HistoryRow {
  id: string; started_at: number; label: string; synthetic: boolean; aborted: string | null; drive_type: string | null
  running_min: number; max_speed_kmh: number | null; engine_stops: number; fan_runs_after_shutdown: number
  fan_durations_s: (number | null)[]; battery_warnings: number; possible_regenerations: number
  possible_interrupted_regenerations: number; user_suspected_regenerations: number; coolant_max_c: number | null
  ecu_voltage_running_median: number | null; adapter_voltage_running_median: number | null; link_events: number
}
export interface LiveSignal { pid: number; name: string; unit: string | null; value: number | null; quality: string; at: number; points: Point[]; attempts: number; ok: number; first: number }
