import { useMemo, useState } from "react";
import { Download, RotateCcw, Crosshair, Radio } from "lucide-react";
import type { SimState } from "../types/sim";
import { computeAnalogScaleBounds, formatSimTime } from "../utils/formatters";

interface PlantAndTracerViewProps {
  state: SimState;
  busy: boolean;
  onWriteTag: (address: string, value: boolean | number | string, dataType?: string) => void;
  onPulseInput: (address: string, activeLow?: boolean) => void;
  onControl: (action: string, extra?: Record<string, unknown>) => void;
  onTraceAction: (payload: Record<string, unknown>) => Promise<void>;
  onExportCsv: () => Promise<void>;
}

function findVatBool(state: SimState, address: string, fallback = false): boolean {
  for (const rows of Object.values(state.vat_tables)) {
    const found = rows.find((r) => r.address === address);
    if (found) return Boolean(found.value);
  }
  return fallback;
}

function findVatNumber(state: SimState, address: string, fallback = 0): number {
  for (const rows of Object.values(state.vat_tables)) {
    const found = rows.find((r) => r.address === address);
    if (found) {
      const n = Number(found.value);
      return Number.isFinite(n) ? n : fallback;
    }
  }
  return fallback;
}

function PlantTwinPanel({
  state,
  busy,
  onWriteTag,
  onPulseInput,
  onControl,
}: {
  state: SimState;
  busy: boolean;
  onWriteTag: (address: string, value: boolean | number | string, dataType?: string) => void;
  onPulseInput: (address: string, activeLow?: boolean) => void;
  onControl: (action: string, extra?: Record<string, unknown>) => void;
}) {
  const { plant, project } = state;
  const plantType = project.plant_type;

  if (plantType === "conveyor_motor") {
    const partPos = Number(plant.part_pos_pct ?? 20);
    const rpm = Number(plant.motor_rpm ?? 0);
    const currentA = Number(plant.motor_current_a ?? 0);
    const running = findVatBool(state, "Q0.0");
    const fault = findVatBool(state, "Q0.2");
    const estopOk = findVatBool(state, "I0.0", true);
    const safetyOk = findVatBool(state, "M0.0");
    const photoeye = Boolean(plant.photoeye_blocked);
    const parts = Number(plant.parts_passed ?? 0);

    return (
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-center">
        <div className="lg:col-span-8 bg-[#0B0F14] border border-[#243041] rounded p-3">
          <svg
            viewBox="0 0 640 185"
            className="w-full h-[142px]"
            role="img"
            aria-label="Conveyor and Motor Starter 2D Process Twin"
          >
            {/* Belt Frame */}
            <rect x="40" y="95" width="520" height="30" rx="15" fill="#171F2B" stroke="#334155" strokeWidth="2" />
            {/* Rollers */}
            <circle cx="60" cy="110" r="12" fill="#0B0F14" stroke={running ? "#10B981" : "#64748B"} strokeWidth="3" />
            <circle cx="540" cy="110" r="12" fill="#0B0F14" stroke={running ? "#10B981" : "#64748B"} strokeWidth="3" />
            {/* Drive Motor */}
            <rect
              x="566"
              y="88"
              width="58"
              height="44"
              rx="4"
              fill={running ? "#064E3B" : fault ? "#7F1D1D" : "#171F2B"}
              stroke={running ? "#10B981" : fault ? "#EF4444" : "#334155"}
              strokeWidth="2"
            />
            <text x="595" y="114" textAnchor="middle" fill="#F1F5F9" fontSize="11" fontFamily="JetBrains Mono, monospace">
              M1
            </text>
            {/* Photoeye Sensor Gate at ~80% */}
            <line
              x1="448"
              y1="40"
              x2="448"
              y2="95"
              stroke={photoeye ? "#F59E0B" : "#3B82F6"}
              strokeWidth="2"
              strokeDasharray="4 3"
            />
            <rect x="434" y="24" width="28" height="16" rx="2" fill="#171F2B" stroke="#3B82F6" />
            <text x="448" y="35" textAnchor="middle" fill="#F1F5F9" fontSize="9" fontFamily="JetBrains Mono, monospace">
              I0.6
            </text>
            {/* Moving Part Box */}
            <rect
              x={68 + (partPos / 100) * 430}
              y="62"
              width="38"
              height="32"
              rx="2"
              fill="#3B82F6"
              stroke="#93C5FD"
              strokeWidth="1.5"
            />
            {/* Status Telemetry Labels */}
            <text x="42" y="24" fill="#94A3B8" fontSize="11" fontFamily="JetBrains Mono, monospace">
              MOTOR Q0.0: {running ? "ENERGIZED" : "STOPPED"}
            </text>
            <text x="235" y="24" fill={safetyOk ? "#10B981" : "#F59E0B"} fontSize="11" fontFamily="JetBrains Mono, monospace">
              SAFETY M0.0: {safetyOk ? "LATCHED" : "NEEDS RESET"}
            </text>
            <text x="42" y="156" fill="#CBD5E1" fontSize="11" fontFamily="JetBrains Mono, monospace">
              RPM: {rpm.toFixed(0)} ({currentA.toFixed(1)}A)  |  PARTS: {parts}  |  FAULT Q0.2: {fault ? "TRIPPED" : "OK"}
            </text>
          </svg>
        </div>

        <div className="lg:col-span-4 space-y-2.5">
          <div className="text-xs font-semibold text-[#F1F5F9]">Operator HMI Control Station</div>
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => onPulseInput("I0.2", false)}
              className="h-9 px-3 rounded bg-[#3B82F6]/20 hover:bg-[#3B82F6]/30 border border-[#3B82F6]/50 text-xs font-semibold text-[#93C5FD] cursor-pointer"
            >
              Safety Reset (I0.2)
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onPulseInput("I0.3", false)}
              className="h-9 px-3 rounded bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/50 text-xs font-semibold text-emerald-300 cursor-pointer"
            >
              Start PB (I0.3)
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onPulseInput("I0.4", true)}
              className="h-9 px-3 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs font-semibold text-[#F1F5F9] cursor-pointer"
            >
              Stop NC (I0.4)
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onWriteTag("I0.0", !estopOk, "BOOL")}
              className={`h-9 px-3 rounded border text-xs font-semibold cursor-pointer ${
                estopOk
                  ? "bg-red-500/15 hover:bg-red-500/25 border-red-500/50 text-red-300"
                  : "bg-emerald-500/15 hover:bg-emerald-500/25 border-emerald-500/50 text-emerald-300"
              }`}
            >
              {estopOk ? "Trip E-Stop (I0.0)" : "Release E-Stop (I0.0)"}
            </button>
          </div>
          <p className="text-[11px] text-[#94A3B8]">
            Press <span className="font-mono text-[#CBD5E1]">Safety Reset (I0.2)</span> to latch{" "}
            <span className="font-mono">M0.0</span>, then press{" "}
            <span className="font-mono text-[#CBD5E1]">Start PB (I0.3)</span> to seal in{" "}
            <span className="font-mono">Q0.0</span>.
          </p>
        </div>
      </div>
    );
  }

  if (plantType === "tank_pid") {
    const levelPct = Number(plant.level_pct ?? 20);
    const spPct = findVatNumber(state, "MD14", 60);
    const valvePct = Number(plant.inlet_valve_pct ?? 0);
    const disturbance = Number(plant.outlet_disturbance_pct ?? 18);
    const hiAlarm = findVatBool(state, "Q0.1");
    const sensorFault = findVatBool(state, "Q0.2");
    const fluidH = Math.max(0, Math.min(110, (levelPct / 100) * 110));
    const spY = 148 - (Math.max(0, Math.min(100, spPct)) / 100) * 110;

    return (
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-center">
        <div className="lg:col-span-8 bg-[#0B0F14] border border-[#243041] rounded p-3">
          <svg
            viewBox="0 0 640 185"
            className="w-full h-[142px]"
            role="img"
            aria-label="Closed-Loop Tank Level PID 2D Process Twin"
          >
            {/* Inlet Pipe & Control Valve */}
            <line x1="40" y1="54" x2="210" y2="54" stroke="#3B82F6" strokeWidth="6" />
            <polygon
              points="110,42 136,54 110,66"
              fill={valvePct > 1 ? "#10B981" : "#334155"}
              stroke="#F1F5F9"
              strokeWidth="1"
            />
            <polygon
              points="162,42 136,54 162,66"
              fill={valvePct > 1 ? "#10B981" : "#334155"}
              stroke="#F1F5F9"
              strokeWidth="1"
            />
            <text x="136" y="34" textAnchor="middle" fill="#CBD5E1" fontSize="10" fontFamily="JetBrains Mono, monospace">
              VALVE PQW256: {valvePct.toFixed(1)}%
            </text>

            {/* Tank Vessel */}
            <rect x="210" y="38" width="170" height="110" rx="4" fill="#121821" stroke="#475569" strokeWidth="2" />
            {/* Fluid Fill */}
            <rect
              x="212"
              y={148 - fluidH}
              width="166"
              height={fluidH}
              fill="#3B82F6"
              fillOpacity="0.45"
            />
            {/* Setpoint Line */}
            <line x1="204" y1={spY} x2="386" y2={spY} stroke="#10B981" strokeWidth="2" strokeDasharray="5 3" />
            <text x="394" y={spY + 4} fill="#10B981" fontSize="10" fontFamily="JetBrains Mono, monospace">
              SP {spPct.toFixed(1)}%
            </text>

            {/* Tank Center Readout */}
            <text x="295" y="98" textAnchor="middle" fill="#F1F5F9" fontSize="18" fontWeight="700" fontFamily="JetBrains Mono, monospace">
              {levelPct.toFixed(1)}%
            </text>

            {/* Outlet Pipe */}
            <line x1="380" y1="136" x2="535" y2="136" stroke="#64748B" strokeWidth="6" />
            <text x="462" y="126" textAnchor="middle" fill="#94A3B8" fontSize="10" fontFamily="JetBrains Mono, monospace">
              OUTFLOW {disturbance.toFixed(0)}%
            </text>

            {/* Alarm Status */}
            <text x="42" y="168" fill={hiAlarm || sensorFault ? "#EF4444" : "#94A3B8"} fontSize="11" fontFamily="JetBrains Mono, monospace">
              HI-HI Q0.1: {hiAlarm ? "TRIPPED" : "OK"}  |  SENSOR FAULT Q0.2: {sensorFault ? "TRIPPED" : "OK"}
            </text>
          </svg>
        </div>

        <div className="lg:col-span-4 space-y-3">
          <div className="text-xs font-semibold text-[#F1F5F9]">OB35 + FB41 PID Controls</div>
          <label className="block space-y-1">
            <div className="flex justify-between text-xs">
              <span className="text-[#94A3B8]">Setpoint SP_INT (MD14)</span>
              <span className="font-mono text-emerald-400 tabular-nums">{spPct.toFixed(0)}%</span>
            </div>
            <input
              type="range"
              min={10}
              max={95}
              step={1}
              value={Math.round(spPct)}
              onChange={(e) => onWriteTag("MD14", Number(e.target.value), "REAL")}
              className="w-full accent-[#3B82F6]"
            />
          </label>

          <label className="block space-y-1">
            <div className="flex justify-between text-xs">
              <span className="text-[#94A3B8]">Outlet Load Disturbance</span>
              <span className="font-mono text-amber-300 tabular-nums">{disturbance.toFixed(0)}%</span>
            </div>
            <input
              type="range"
              min={0}
              max={55}
              step={1}
              value={Math.round(disturbance)}
              onChange={(e) =>
                onControl("set_disturbance", { disturbance_load: Number(e.target.value) })
              }
              className="w-full accent-amber-500"
            />
          </label>

          <div className="flex gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => onWriteTag("MD14", 75.0, "REAL")}
              className="flex-1 h-8 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs font-medium text-[#F1F5F9] cursor-pointer"
            >
              Step SP to 75%
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onWriteTag("MD14", 40.0, "REAL")}
              className="flex-1 h-8 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs font-medium text-[#F1F5F9] cursor-pointer"
            >
              Step SP to 40%
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (plantType === "batch_mixer_sfc") {
    const level = Number(plant.level_pct ?? 0);
    const temp = Number(plant.temp_c ?? 24);
    const step = String(plant.active_step ?? "S0_IDLE");
    const inlet = findVatBool(state, "Q0.0");
    const agitator = findVatBool(state, "Q0.1");
    const heater = findVatBool(state, "Q0.2");
    const drain = findVatBool(state, "Q0.3");
    const estopOk = findVatBool(state, "I0.0", true);
    const fluidH = Math.max(0, Math.min(100, level));

    return (
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4 items-center">
        <div className="lg:col-span-8 bg-[#0B0F14] border border-[#243041] rounded p-3">
          <svg
            viewBox="0 0 640 185"
            className="w-full h-[142px]"
            role="img"
            aria-label="Sequential Batch Reactor S7-GRAPH 2D Process Twin"
          >
            {/* Reactor Vessel */}
            <rect
              x="200"
              y="38"
              width="180"
              height="106"
              rx="8"
              fill="#121821"
              stroke={heater ? "#EF4444" : "#475569"}
              strokeWidth="2.5"
            />
            <rect
              x="203"
              y={142 - fluidH}
              width="174"
              height={fluidH}
              rx="4"
              fill={temp > 55 ? "#F59E0B" : "#3B82F6"}
              fillOpacity="0.45"
            />
            {/* Agitator Shaft */}
            <line x1="290" y1="22" x2="290" y2="120" stroke={agitator ? "#10B981" : "#64748B"} strokeWidth="3" />
            <line x1="254" y1="118" x2="326" y2="118" stroke={agitator ? "#10B981" : "#64748B"} strokeWidth="4" />

            <text x="36" y="34" fill="#3B82F6" fontSize="11" fontWeight="700" fontFamily="JetBrains Mono, monospace">
              STEP: {step}
            </text>
            <text x="36" y="60" fill="#CBD5E1" fontSize="11" fontFamily="JetBrains Mono, monospace">
              LEVEL: {level.toFixed(1)}%
            </text>
            <text x="36" y="82" fill="#CBD5E1" fontSize="11" fontFamily="JetBrains Mono, monospace">
              TEMP:  {temp.toFixed(1)} C
            </text>
            <text x="36" y="104" fill="#94A3B8" fontSize="11" fontFamily="JetBrains Mono, monospace">
              BATCHES: {String(plant.completed_batches ?? 0)}
            </text>

            <text x="415" y="54" fill={inlet ? "#10B981" : "#64748B"} fontSize="11" fontFamily="JetBrains Mono, monospace">
              INLET Q0.0:    {inlet ? "OPEN" : "CLOSED"}
            </text>
            <text x="415" y="76" fill={agitator ? "#10B981" : "#64748B"} fontSize="11" fontFamily="JetBrains Mono, monospace">
              AGITATOR Q0.1: {agitator ? "RUN" : "OFF"}
            </text>
            <text x="415" y="98" fill={heater ? "#F59E0B" : "#64748B"} fontSize="11" fontFamily="JetBrains Mono, monospace">
              HEATER Q0.2:   {heater ? "ON" : "OFF"}
            </text>
            <text x="415" y="120" fill={drain ? "#3B82F6" : "#64748B"} fontSize="11" fontFamily="JetBrains Mono, monospace">
              DRAIN Q0.3:    {drain ? "OPEN" : "CLOSED"}
            </text>
          </svg>
        </div>

        <div className="lg:col-span-4 space-y-2.5">
          <div className="text-xs font-semibold text-[#F1F5F9]">Batch Reactor Sequence Controls</div>
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => onPulseInput("I0.5", false)}
              className="h-9 px-3 rounded bg-[#3B82F6]/20 hover:bg-[#3B82F6]/30 border border-[#3B82F6]/50 text-xs font-semibold text-[#93C5FD] cursor-pointer"
            >
              Reset / Ack (I0.5)
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onPulseInput("I0.1", false)}
              className="h-9 px-3 rounded bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/50 text-xs font-semibold text-emerald-300 cursor-pointer"
            >
              Start Batch (I0.1)
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onWriteTag("I0.0", !estopOk, "BOOL")}
              className={`col-span-2 h-9 px-3 rounded border text-xs font-semibold cursor-pointer ${
                estopOk
                  ? "bg-red-500/15 hover:bg-red-500/25 border-red-500/50 text-red-300"
                  : "bg-emerald-500/15 hover:bg-emerald-500/25 border-emerald-500/50 text-emerald-300"
              }`}
            >
              {estopOk ? "Abort / Trip E-Stop (I0.0)" : "Restore Safety E-Stop (I0.0)"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="p-4 bg-[#0B0F14] border border-[#243041] rounded text-xs text-[#94A3B8]">
      Custom AI-generated STEP 7 project loaded. Use the SIMATIC VAT &amp; Force Dock below to toggle inputs and inspect outputs in real time on the 1-Scan Trace Oscilloscope.
    </div>
  );
}

export function PlantAndTracerView({
  state,
  busy,
  onWriteTag,
  onPulseInput,
  onControl,
  onTraceAction,
  onExportCsv,
}: PlantAndTracerViewProps) {
  const { trace } = state;
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [trigMode, setTrigMode] = useState(trace.trigger.mode || "manual");
  const [trigChannel, setTrigChannel] = useState(
    trace.trigger.channel || trace.channels[0]?.address || "",
  );

  const digitalChannels = trace.channels.filter((c) => c.kind === "digital");
  const analogChannels = trace.channels.filter((c) => c.kind === "analog");
  const samples = trace.samples;

  const analogBounds = useMemo(() => {
    const vals: number[] = [];
    for (const s of samples) {
      for (const ch of analogChannels) {
        const v = s.values[ch.address];
        if (typeof v === "number" && Number.isFinite(v)) vals.push(v);
      }
    }
    return computeAnalogScaleBounds(vals);
  }, [samples, analogChannels]);

  const activeSample =
    hoverIdx !== null && samples[hoverIdx]
      ? samples[hoverIdx]
      : samples.length > 0
        ? samples[samples.length - 1]
        : null;

  const prevSample =
    hoverIdx !== null && hoverIdx > 0
      ? samples[hoverIdx - 1]
      : samples.length > 1
        ? samples[0]
        : null;

  const width = 760;
  const xForIndex = (i: number) =>
    samples.length <= 1 ? 0 : (i / (samples.length - 1)) * width;

  return (
    <div className="flex-1 overflow-y-auto p-3 space-y-3">
      {/* Section 1: 2D Closed-Loop Plant Twin */}
      <section className="rounded bg-[#121821] border border-[#243041] overflow-hidden">
        <header className="px-3.5 py-2 bg-[#171F2B] border-b border-[#243041] flex items-center justify-between">
          <div className="flex items-baseline gap-2.5">
            <h2 className="font-display text-sm font-semibold text-[#F1F5F9]">
              Closed-Loop 2D Plant Twin
            </h2>
            <p className="text-[11px] text-[#94A3B8] hidden sm:inline">
              Coupled to PLC process image (I/Q/PIW/PQW) every OB1/OB35 cycle
            </p>
          </div>
          <span className="font-mono text-xs text-[#94A3B8] tabular-nums">
            t = {formatSimTime(state.cpu.stats.sim_time_ms)}
          </span>
        </header>
        <div className="p-3">
          <PlantTwinPanel
            state={state}
            busy={busy}
            onWriteTag={onWriteTag}
            onPulseInput={onPulseInput}
            onControl={onControl}
          />
        </div>
      </section>

      {/* Section 2: High-Speed 1-Scan Trace Oscilloscope */}
      <section className="rounded bg-[#121821] border border-[#243041] overflow-hidden">
        <header className="px-3.5 py-2 bg-[#171F2B] border-b border-[#243041] flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Radio className="w-4 h-4 text-[#3B82F6]" aria-hidden="true" />
            <h2 className="font-display text-sm font-semibold text-[#F1F5F9]">
              1-Scan Trace Oscilloscope
            </h2>
            <span className="font-mono text-[11px] text-[#94A3B8] tabular-nums">
              ({trace.total_samples} samples)
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <select
              aria-label="Trace trigger mode"
              value={trigMode}
              onChange={(e) => setTrigMode(e.target.value)}
              className="h-7 px-2 rounded bg-[#0B0F14] border border-[#243041] text-xs text-[#F1F5F9]"
            >
              <option value="manual">Manual / Continuous</option>
              <option value="rising">Rising Edge</option>
              <option value="falling">Falling Edge</option>
              <option value="gt">Greater Than (&gt; 50)</option>
              <option value="lt">Less Than (&lt; 50)</option>
            </select>

            <select
              aria-label="Trace trigger channel"
              value={trigChannel}
              onChange={(e) => setTrigChannel(e.target.value)}
              className="h-7 px-2 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9]"
            >
              {trace.channels.map((c) => (
                <option key={c.address} value={c.address}>
                  {c.address} ({c.label})
                </option>
              ))}
            </select>

            <button
              type="button"
              disabled={busy}
              onClick={() =>
                onTraceAction({
                  action: "arm",
                  mode: trigMode,
                  channel: trigChannel || trace.channels[0]?.address || "",
                  threshold: 50.0,
                })
              }
              className="h-7 px-2.5 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs text-[#F1F5F9] cursor-pointer"
            >
              Arm Trigger
            </button>

            <button
              type="button"
              disabled={busy}
              onClick={() => onTraceAction({ action: "clear" })}
              className="h-7 px-2.5 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs text-[#CBD5E1] flex items-center gap-1 cursor-pointer"
            >
              <RotateCcw className="w-3 h-3" aria-hidden="true" />
              <span>Clear</span>
            </button>

            <button
              type="button"
              onClick={onExportCsv}
              className="h-7 px-2.5 rounded bg-[#3B82F6] hover:bg-[#2563EB] text-xs font-medium text-white flex items-center gap-1 cursor-pointer"
            >
              <Download className="w-3 h-3" aria-hidden="true" />
              <span>Export CSV</span>
            </button>
          </div>
        </header>

        <div className="p-4 space-y-3">
          {/* PID Step-Response Metrics Strip (if tank_pid) */}
          {trace.pid_metrics && (
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 text-xs font-mono tabular-nums">
              <div className="bg-[#0B0F14] border border-[#243041] rounded px-2.5 py-1.5">
                <div className="text-[10px] text-[#64748B]">SETPOINT</div>
                <div className="text-emerald-400 font-semibold">{trace.pid_metrics.setpoint.toFixed(1)}%</div>
              </div>
              <div className="bg-[#0B0F14] border border-[#243041] rounded px-2.5 py-1.5">
                <div className="text-[10px] text-[#64748B]">PROCESS VAR</div>
                <div className="text-[#3B82F6] font-semibold">{trace.pid_metrics.process_variable.toFixed(1)}%</div>
              </div>
              <div className="bg-[#0B0F14] border border-[#243041] rounded px-2.5 py-1.5">
                <div className="text-[10px] text-[#64748B]">STEADY ERROR</div>
                <div className="text-[#F1F5F9] font-semibold">{trace.pid_metrics.steady_state_error.toFixed(2)}%</div>
              </div>
              <div className="bg-[#0B0F14] border border-[#243041] rounded px-2.5 py-1.5">
                <div className="text-[10px] text-[#64748B]">OVERSHOOT</div>
                <div className="text-amber-300 font-semibold">{trace.pid_metrics.overshoot_pct.toFixed(1)}%</div>
              </div>
              <div className="bg-[#0B0F14] border border-[#243041] rounded px-2.5 py-1.5">
                <div className="text-[10px] text-[#64748B]">RISE TIME</div>
                <div className="text-[#F1F5F9] font-semibold">
                  {trace.pid_metrics.rise_time_ms !== null ? `${trace.pid_metrics.rise_time_ms}ms` : "—"}
                </div>
              </div>
            </div>
          )}

          {/* Interactive Crosshair Readout Bar */}
          <div className="px-3 py-1.5 rounded bg-[#0B0F14] border border-[#243041] flex flex-wrap items-center justify-between gap-2 font-mono text-[11px] tabular-nums">
            <div className="flex items-center gap-3 text-[#CBD5E1]">
              <span className="flex items-center gap-1 text-[#3B82F6]">
                <Crosshair className="w-3.5 h-3.5" aria-hidden="true" />
                <span>Scan #{activeSample ? activeSample.scan : 0}</span>
              </span>
              <span>t = {activeSample ? formatSimTime(activeSample.time_ms) : "00:00.000"}</span>
              {activeSample && prevSample && (
                <span className="text-[#94A3B8]">
                  Δt = {activeSample.time_ms - prevSample.time_ms}ms
                </span>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {trace.channels.map((ch) => {
                const v = activeSample?.values[ch.address];
                return (
                  <span key={ch.address} className="flex items-center gap-1">
                    <span
                      className="inline-block w-2 h-2 rounded-xs"
                      style={{ backgroundColor: ch.color }}
                    />
                    <span className="text-[#94A3B8]">{ch.address}:</span>
                    <span className="text-[#F1F5F9] font-semibold">
                      {v !== undefined ? (ch.kind === "digital" ? (v > 0.5 ? "1" : "0") : v.toFixed(1)) : "—"}
                    </span>
                  </span>
                );
              })}
            </div>
          </div>

          {samples.length === 0 ? (
            <div className="h-44 rounded bg-[#0B0F14] border border-[#243041] flex items-center justify-center text-xs text-[#64748B]">
              Step or Run the CPU to capture 1-scan digital and analog waveforms.
            </div>
          ) : (
            <div className="space-y-2">
              {/* Digital Logic Analyzer Lanes */}
              {digitalChannels.length > 0 && (
                <div className="rounded bg-[#0B0F14] border border-[#243041] p-2">
                  <svg
                    viewBox={`0 0 ${width + 110} ${digitalChannels.length * 30 + 10}`}
                    className="w-full"
                    onMouseMove={(e) => {
                      const rect = e.currentTarget.getBoundingClientRect();
                      const relX = ((e.clientX - rect.left) / rect.width) * (width + 110) - 105;
                      if (relX >= 0 && samples.length > 1) {
                        const idx = Math.min(
                          samples.length - 1,
                          Math.max(0, Math.round((relX / width) * (samples.length - 1))),
                        );
                        setHoverIdx(idx);
                      }
                    }}
                    onMouseLeave={() => setHoverIdx(null)}
                  >
                    {digitalChannels.map((ch, rowIdx) => {
                      const baseY = rowIdx * 30 + 24;
                      const highY = rowIdx * 30 + 8;
                      let d = "";
                      samples.forEach((s, i) => {
                        const x = 105 + xForIndex(i);
                        const y = (s.values[ch.address] || 0) > 0.5 ? highY : baseY;
                        if (i === 0) {
                          d = `M ${x} ${y}`;
                        } else {
                          const prevY = (samples[i - 1].values[ch.address] || 0) > 0.5 ? highY : baseY;
                          d += ` L ${x} ${prevY} L ${x} ${y}`;
                        }
                      });
                      return (
                        <g key={ch.address}>
                          <line x1="105" y1={baseY + 2} x2={width + 105} y2={baseY + 2} stroke="#1E293B" strokeWidth="1" />
                          <text x="6" y={baseY - 4} fill="#CBD5E1" fontSize="10" fontFamily="JetBrains Mono, monospace">
                            {ch.address}
                          </text>
                          <path d={d} fill="none" stroke={ch.color} strokeWidth="2" />
                        </g>
                      );
                    })}
                    {hoverIdx !== null && samples[hoverIdx] && (
                      <line
                        x1={105 + xForIndex(hoverIdx)}
                        y1={4}
                        x2={105 + xForIndex(hoverIdx)}
                        y2={digitalChannels.length * 30 + 6}
                        stroke="#94A3B8"
                        strokeDasharray="3 3"
                      />
                    )}
                  </svg>
                </div>
              )}

              {/* Analog Trend Scope */}
              {analogChannels.length > 0 && (
                <div className="rounded bg-[#0B0F14] border border-[#243041] p-2">
                  <svg
                    viewBox={`0 0 ${width + 110} 130`}
                    className="w-full h-[126px]"
                    onMouseMove={(e) => {
                      const rect = e.currentTarget.getBoundingClientRect();
                      const relX = ((e.clientX - rect.left) / rect.width) * (width + 110) - 105;
                      if (relX >= 0 && samples.length > 1) {
                        const idx = Math.min(
                          samples.length - 1,
                          Math.max(0, Math.round((relX / width) * (samples.length - 1))),
                        );
                        setHoverIdx(idx);
                      }
                    }}
                    onMouseLeave={() => setHoverIdx(null)}
                  >
                    {[0, 0.25, 0.5, 0.75, 1].map((frac) => {
                      const y = 12 + frac * 104;
                      const val = analogBounds.max - frac * (analogBounds.max - analogBounds.min);
                      return (
                        <g key={frac}>
                          <line x1="105" y1={y} x2={width + 105} y2={y} stroke="#1E293B" strokeWidth="1" />
                          <text x="98" y={y + 3} textAnchor="end" fill="#64748B" fontSize="9" fontFamily="JetBrains Mono, monospace">
                            {val.toFixed(0)}
                          </text>
                        </g>
                      );
                    })}
                    {analogChannels.map((ch) => {
                      const span = Math.max(1e-6, analogBounds.max - analogBounds.min);
                      const pts = samples
                        .map((s, i) => {
                          const x = 105 + xForIndex(i);
                          const v = s.values[ch.address] ?? 0;
                          const y = 116 - ((v - analogBounds.min) / span) * 104;
                          return `${x},${Math.max(8, Math.min(122, y))}`;
                        })
                        .join(" ");
                      return (
                        <polyline
                          key={ch.address}
                          points={pts}
                          fill="none"
                          stroke={ch.color}
                          strokeWidth="2"
                        />
                      );
                    })}
                    {hoverIdx !== null && samples[hoverIdx] && (
                      <line
                        x1={105 + xForIndex(hoverIdx)}
                        y1={8}
                        x2={105 + xForIndex(hoverIdx)}
                        y2={122}
                        stroke="#94A3B8"
                        strokeDasharray="3 3"
                      />
                    )}
                  </svg>
                </div>
              )}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
