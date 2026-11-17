export interface PidRef { pid: number; hex: string; name: string; unit?: string | null }
export interface EcuCaps { ecu: string; mode01_pids: number[]; mode02_pids: number[]; mode06_mids: number[]; mode09_pids: number[] }
export interface Capabilities { ecus: EcuCaps[]; primary_ecu: string | null; modes: Record<string, string> }
export interface AdapterInfo {
  identity: string | null; device_description: string | null; voltage: string | null
  protocol_number: string | null; protocol_name: string | null; is_can: boolean
  init_log: { command: string; response: string; status: string }[]
}
export interface Recording { session_id: string; started_at: number; duration_s: number; fast: number[]; slow: number[]; unsupported: number[] }
export interface Status {
  read_only: true; connected: boolean; source: string; synthetic: boolean; scenario: string | null
  transport: { kind: string; state: string; last_error: string | null; host?: string; port?: number; reconnects?: number } | null
  adapter: AdapterInfo | null; capabilities: Capabilities | null; recording: Recording | null; vehicle_profile: string
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
  priority_pids: PidRef[]; recording_profiles: { id: string; name: string }[]; event_labels: string[]
  first_run_checklist: string[]; scenarios: { name: string; description: string }[]; manufacturer: MfrExtension[]
  owner_reported: OwnerReported
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
}
export interface SessionDetail extends SessionSummary {
  meta: {
    metrics?: Metric[]; selected?: { fast: number[]; slow: number[]; unsupported: number[] }
    vehicle_info?: { vin: string | null; vin_status: string; calibration_ids: { ecu: string; text: string }[] }
    aborted?: string; adapter?: AdapterInfo | null
  }
  scans: DtcScan[]; annotations: { t_wall: number; t_mono: number | null; label: string; note: string }[]
}
export type Point = [number, number]
export interface Signal { pid: number; hex: string; name: string; unit: string | null; decoded: boolean; points: Point[]; failed: [number, string][] }
export interface Series { session_id: string; signals: Signal[]; annotations: { t: number | null; label: string; note: string }[] }
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
export interface Plan { fast: PidRef[]; slow: PidRef[]; unsupported: PidRef[] }
export interface LiveSignal { pid: number; name: string; unit: string | null; value: number | null; quality: string; at: number; points: Point[]; attempts: number; ok: number; first: number }
