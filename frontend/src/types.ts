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
  /** vehicle polling paused after the ECU stopped answering (stays paused while the adapter is unreachable) */
  ecu_polling_paused?: boolean
  adapter_link?: 'ok' | 'lost'
  /** an explicit resume re-initialises the adapter first (ATZ ... 0100) */
  resume_requires_reinit?: boolean
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
/** DDE-04: BMW DDE fault memory read with a scan; kept apart from the generic codes. */
export interface ManufacturerFault {
  code: string; raw_hex: string; status_raw: string | null; status: Record<string, boolean> | null; counter: number | null
}
export interface ManufacturerFaultRead {
  origin: 'bmw_dde'
  status: 'not_attempted' | 'unavailable' | 'unsupported_adapter' | 'read' | 'no_faults' | 'partial' | 'negative'
    | 'no_data' | 'timeout' | 'disconnected' | 'error'
  detail: string | null; synthetic: boolean; request_id: string | null; catalog_version: string | null
  decoder_version: string | null; ecu: string | null; raw_hex: string; nrc: string | null; faults: ManufacturerFault[]
}
export interface DtcScan {
  t_wall: number; phase: string; dtcs: Dtc[]; mode_status: Record<string, string>
  mil_on: boolean | null; dtc_count_reported: number | null; freeze_frame: FreezeFrame | null
  origin?: 'generic_obd'
  /** absent / null on older scans and other vehicles */
  manufacturer?: ManufacturerFaultRead | null
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
  /** 'possible' for DPF / cooling interpretations, 'candidate' for air-path screening results */
  type: string; origin: 'inferred'; status: 'possible' | 'candidate'; confidence: 'low' | 'moderate'; text: string
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
  /** absent in responses of older servers */
  air_path?: AirPath
}
/** a measured value held from its own timestamp, with its age; stale values are not associated */
export interface Held { value: number; age_s: number; fresh: boolean }
export interface AirPathSignal { name: string; hex: string; status: DataStatus; samples: number; failed_reads: number; reason: string | null }
export interface AirPathGroup { running_samples: number; zero: number; low: number; episodes: number; low_per_1k: number | null; zero_per_1k: number | null }
export interface MafEpisode extends DetectedEvent {
  t_start_rel_s: number; t_end_rel_s: number; duration_s: number; samples: number; zero_samples: number; min_maf_gs: number
  pattern: 'persistent' | 'single_transient'; repeated: boolean; cluster: number; motion: string; temperature_state: string; ecu: string | null
  context: Record<string, Held | null>; rpm: { unstable: boolean | null; window: { n: number; min: number; max: number; std: number | null } | null }
  associations?: { egr_excursion_nearby?: boolean | null; map_change?: { change: number } | null; egr_cmd_change?: { change: number } | null }
  evidence: { t_rel_s: number; value_gs: number; raw_hex: string; response: string | null; ecu: string | null }[]
  limitations: string[]
}
export interface AirPathDtc {
  code: string; family: string; air_path_relevance: 'air_path_family' | 'unknown' | 'other_family'; description: string | null
  description_status: string; note: string; ecus: (string | null)[]; statuses: string[]; scan_phases: string[]; timing: string
}
export interface AirPath {
  status: 'evaluated' | 'maf_not_available'
  signals: Record<string, AirPathSignal>
  running_s: number
  maf: {
    samples_ok: number; failed_reads: { n: number; by_quality: Record<string, number> }
    running_state?: { running_confirmed: number; engine_not_running: number; unknown: number; note: string }
    zero_running?: number; low_running?: number; zero_per_1k_running?: number | null; low_per_1k_running?: number | null
    zero_engine_not_running?: number; low_running_state_unknown?: number
    episodes: MafEpisode[]; episode_count?: number; episode_total_s?: number; persistent_episodes?: number; transient_episodes?: number
    repeated_clusters?: number; dips: DetectedEvent[]
    by_temperature?: Record<'cold' | 'warm' | 'unknown', AirPathGroup>; by_motion?: Record<'idle' | 'moving' | 'unknown', AirPathGroup>
  }
  egr_error: {
    samples: number; count: number | null; per_running_h: number | null; max_abs_pct?: number | null; screen_pct?: number
    positive?: number; negative?: number; both_directions?: number
    excursions: { t_start_rel_s: number; t_end_rel_s: number; max_abs_pct: number; direction: string; motion: string; load_band: string }[]
  }
  correlations: { supporting: string[]; contradicting: string[]; note: string; symptom_markers: { event: string; t_rel_s: number; near_air_path_anomaly: boolean | null }[] }
  dtcs: {
    scans: { phase: string; t_rel_s: number | null; mil_on: boolean | null }[]; codes: string[]; by_code: AirPathDtc[]
    mil_on: boolean | null; owner_reported_manufacturer_codes: { code: string; system: string | null; source: string }[]
    owner_reported_note: string; notes: string[]
  }
  limitations: string[]
  heuristics: Record<string, number>
  history?: { other_sessions: number; medians: Record<string, number | null> | null; note: string }
}
/** compact per-session air-path statistics; null = unavailable (never zero) */
export interface AirPathRow {
  maf_status: string; running_h: number | null; maf_running_samples: number | null; maf_zero_running: number | null
  maf_low_running: number | null; maf_zero_per_1k: number | null; maf_low_per_1k: number | null; maf_episodes: number | null
  maf_episodes_per_h: number | null; maf_episode_total_s: number | null; maf_dips: number | null
  cold: AirPathGroup | null; warm: AirPathGroup | null; idle: AirPathGroup | null; moving: AirPathGroup | null
  rpm_unstable_episodes: number | null; egr_status: string; egr_excursions: number | null; egr_excursions_per_h: number | null
  egr_max_abs_pct: number | null; dtc_scanned: boolean; dtcs: string[] | null; mil_on: boolean | null
  symptom_markers: number; symptom_markers_near_anomaly: number
  vs_cohort?: { other_sessions: number; medians: Record<string, number | null> | null; note: string }
}
export interface AirPathTrend {
  cohort: { vehicle_profile: string; origin: 'real' | 'synthetic'; vehicle_identity: string | null } | null
  sessions: number; sessions_with_maf: number; sessions_scanned: number
  series: { id: string; started_at: number; drive_type: string | null; maf_zero_per_1k: number | null; maf_low_per_1k: number | null
    maf_episodes_per_h: number | null; egr_excursions_per_h: number | null; maf_episodes: number | null; dtcs: string[] | null; mil_on: boolean | null }[]
  medians: Record<string, number | null>
  by_drive_type: Record<string, { sessions: number } & Record<string, number | null>>
  cold: AirPathGroup | null; warm: AirPathGroup | null; idle: AirPathGroup | null; moving: AirPathGroup | null
  dtc_recurrence: { code: string; sessions: number; share_of_scanned_sessions: number; first_seen: number; last_seen: number }[]
  mil_on_sessions: number; note: string
}
export interface HistoryRow {
  id: string; started_at: number; label: string; synthetic: boolean; aborted: string | null; drive_type: string | null
  /** whose references judged this row: 'real' and 'synthetic' sessions never share a baseline */
  baseline_origin?: 'real' | 'synthetic'; interrupted?: boolean
  running_min: number; max_speed_kmh: number | null; engine_stops: number; fan_runs_after_shutdown: number
  fan_durations_s: (number | null)[]; battery_warnings: number; possible_regenerations: number
  possible_interrupted_regenerations: number; user_suspected_regenerations: number; coolant_max_c: number | null
  ecu_voltage_running_median: number | null; adapter_voltage_running_median: number | null; link_events: number
  air_path?: AirPathRow
}
export interface LiveSignal { pid: number; name: string; unit: string | null; value: number | null; quality: string; at: number; points: Point[]; attempts: number; ok: number; first: number }

/** DDE-01 capability report: every field says where it came from and how much it is worth. */
export type CapQuality = 'observed' | 'claimed' | 'derived' | 'not_supported' | 'not_attempted' | 'failed' | 'unknown'
export interface CapField {
  value: unknown; quality: CapQuality; source: string | null; detail: string; exchange_seq?: number | null
}
export type ManufacturerTransportVerdict =
  | 'manufacturer_transport_blocked' | 'manufacturer_transport_unverified' | 'manufacturer_transport_candidate'
export interface DdeCapabilityReport {
  format: string; session_id: string | null; assessed_at: number; source: string; synthetic: boolean
  adapter: Record<'identity' | 'device_description' | 'claimed_elm_version' | 'supply_voltage' | 'chip', CapField> & { notes: string[] }
  protocol: Record<'number' | 'name' | 'can_11bit_500k', CapField>
  ecus: { source_id: string; primary: boolean; mode01_pids: number; mode09_infotypes: string[]; role: string }[]
  identification: Record<string, CapField>
  isotp_multiframe: CapField
  dde: Record<'variant' | 'software_version' | 'manufacturer_requests_sent', CapField>
  verdicts: {
    generic_obd: { verdict: string; reasons: string[] }
    manufacturer_transport: { verdict: ManufacturerTransportVerdict; blockers: string[]; unverified: string[]; meaning: string }
  }
}

/** DDE-03: catalog availability under the current opt-in / variant / adapter verdict. */
export interface DdeAvailability {
  metric: string; title?: string; priority?: string; kind?: string
  status: 'executable' | 'blocked' | 'unavailable'; reasons: string[]; establish_by?: string[]
}
export interface DdeStatus {
  vehicle_profile: string; supported: boolean
  settings?: { opt_in: boolean; confirmed_variant: string | null; updated_at?: number }
  connected?: boolean; source?: string; version?: string; synthetic?: boolean
  context?: { opted_in: boolean; confirmed_variant: string | null; adapter_verdict: string | null; synthetic_session: boolean }
  availability?: DdeAvailability[]
}
