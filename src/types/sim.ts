export type WorkspaceTab = "debugger" | "plant_tracer" | "faults_fat" | "audit_bridge";

export type CpuMode = "STOP" | "RUN" | "RUN_P" | "PAUSED_BREAKPOINT" | "FAULT_WATCHDOG";

export interface CpuLeds {
  SF: boolean;
  BF: boolean;
  DC5V: boolean;
  FRCE: boolean;
  RUN: boolean;
  STOP: boolean;
}

export interface ScanStats {
  scan_count: number;
  ob35_count: number;
  sim_time_ms: number;
  scan_interval_ms: number;
  last_scan_ms: number;
  min_scan_ms: number;
  avg_scan_ms: number;
  max_scan_ms: number;
  watchdog_limit_ms: number;
  headroom_pct: number;
}

export interface TimerState {
  mode: string;
  preset_ms: number;
  elapsed_ms: number;
  remaining_ms: number;
  running: boolean;
  output: boolean;
}

export interface LadderElement {
  kind: "contact" | "coil" | "timer" | "counter" | "call" | "compare" | "load" | "transfer" | "sfc_step" | "sfc_transition";
  mode?: string;
  logic?: string;
  operand?: string;
  op?: string;
  block?: string;
  instance_db?: string | null;
  params?: Array<{ param: string; value: string }>;
  step_name?: string;
  is_initial?: boolean;
  actions?: Array<{ qualifier: string; operand: string }>;
  name?: string;
  to_step?: string;
  condition?: string;
  value?: boolean | number | string;
  energized?: boolean;
  forced?: boolean;
  running?: boolean;
  output?: boolean;
  preset_ms?: number;
  elapsed_ms?: number;
}

export interface LadderNetwork {
  id: string;
  block: string;
  index: number;
  language: string;
  title: string;
  code_lines: string[];
  has_breakpoint: boolean;
  elements: LadderElement[];
  energized: boolean;
  coverage_scans: number;
}

export interface VatRow {
  address: string;
  symbol: string;
  data_type: string;
  format: string;
  comment: string;
  value: boolean | number | string;
  formatted: string;
  forced: boolean;
  unforced_value: boolean | number | string;
}

export interface ForceEntry {
  address: string;
  value: boolean | number | string;
  data_type: string;
  unforced_value: boolean | number | string;
  forced_at_scan: number;
}

export interface FaultDefinition {
  id: string;
  name: string;
  ob: string;
  severity: "critical" | "warning";
  description: string;
}

export interface DiagnosticEntry {
  seq: number;
  timestamp_utc: string;
  event_id: string;
  severity: "info" | "warning" | "error";
  ob_number: number;
  scan_count: number;
  sim_time_ms: number;
  message: string;
}

export interface TraceChannel {
  address: string;
  label: string;
  kind: "digital" | "analog";
  data_type: string;
  color: string;
  unit?: string;
}

export interface TraceSample {
  scan: number;
  time_ms: number;
  values: Record<string, number>;
}

export interface PidMetrics {
  setpoint: number;
  process_variable: number;
  steady_state_error: number;
  overshoot_pct: number;
  rise_time_ms: number | null;
  settling_time_ms: number | null;
}

export interface StaticAuditFinding {
  id: string;
  severity: "error" | "warning" | "info";
  rule: string;
  address: string;
  locations: string[];
  message: string;
}

export interface CrossReferenceRow {
  address: string;
  symbol: string;
  reads: string[];
  writes: string[];
  read_count: number;
  write_count: number;
}

export interface FatAssertion {
  label: string;
  address: string;
  expected: boolean | number | string;
  actual: boolean | number | string;
  passed: boolean;
}

export interface FatCaseResult {
  id: string;
  name: string;
  category: string;
  description: string;
  passed: boolean;
  duration_ms: number;
  assertions: FatAssertion[];
}

export interface FatResults {
  executed: boolean;
  executed_at_utc?: string;
  passed: boolean;
  passed_count?: number;
  total_count?: number;
  duration_ms?: number;
  fat_network_coverage_pct?: number;
  cases: FatCaseResult[];
}

export interface PresetMeta {
  id: string;
  name: string;
  cpu_family: string;
  order_number: string;
  summary: string;
  plant_type: string;
}

export interface SimState {
  ok: boolean;
  project: {
    id: string;
    name: string;
    cpu_family: string;
    order_number: string;
    plant_type: string;
    sha256: string;
    blocks: Array<{ name: string; language: string; source: string }>;
    coverage_pct: number;
    covered_networks: number;
    total_networks: number;
  };
  cpu: {
    mode: CpuMode;
    leds: CpuLeds;
    stats: ScanStats;
    timers: Record<string, TimerState>;
    counters: Record<string, number>;
  };
  networks: LadderNetwork[];
  vat_tables: Record<string, VatRow[]>;
  forces: ForceEntry[];
  faults: Record<string, boolean>;
  fault_catalog: FaultDefinition[];
  diagnostic_buffer: DiagnosticEntry[];
  breakpoints: {
    networks: string[];
    conditions: Array<{ id: string; address: string; operator: string; value: string | number | boolean }>;
    last_hit: { type: string; target: string; reason: string } | null;
  };
  trace: {
    channels: TraceChannel[];
    trigger: {
      mode: string;
      channel: string;
      threshold: number;
      armed: boolean;
      triggered: boolean;
      trigger_scan: number | null;
    };
    samples: TraceSample[];
    total_samples: number;
    pid_metrics: PidMetrics | null;
  };
  plant: Record<string, number | string | boolean>;
  static_audit: {
    passed: boolean;
    cpu_family: string;
    error_count: number;
    warning_count: number;
    info_count: number;
    findings: StaticAuditFinding[];
    address_count: number;
  };
  cross_references: CrossReferenceRow[];
  fat_suite: Array<{ id: string; name: string; category: string; description: string }>;
  fat_results: FatResults;
  presets: PresetMeta[];
  bridge: {
    platform: string;
    virtual_plc_ready: boolean;
    plcsim_com_available: boolean;
    s7comm_tcp_ready: boolean;
  };
}
