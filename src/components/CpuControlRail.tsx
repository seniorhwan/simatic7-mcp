import { StepForward, FastForward, RotateCcw, Power, Upload, ShieldAlert } from "lucide-react";
import type { SimState } from "../types/sim";
import { classifyWatchdogHeadroom, formatSimTime, shortSha256 } from "../utils/formatters";

interface CpuControlRailProps {
  state: SimState;
  busy: boolean;
  onSelectPreset: (presetId: string) => void;
  onOpenImport: () => void;
  onStep: (cycles: number) => void;
  onControl: (action: string) => void;
  onToggleFault: (faultId: string, active: boolean) => void;
}

const LED_SPECS: Array<{ key: keyof SimState["cpu"]["leds"]; label: string; onClass: string }> = [
  { key: "SF", label: "SF", onClass: "bg-red-500 text-white border-red-400" },
  { key: "BF", label: "BF", onClass: "bg-red-500 text-white border-red-400" },
  { key: "DC5V", label: "DC5V", onClass: "bg-emerald-500 text-[#0B0F14] border-emerald-400" },
  { key: "FRCE", label: "FRCE", onClass: "bg-amber-500 text-[#0B0F14] border-amber-400" },
  { key: "RUN", label: "RUN", onClass: "bg-emerald-500 text-[#0B0F14] border-emerald-400" },
  { key: "STOP", label: "STOP", onClass: "bg-amber-500 text-[#0B0F14] border-amber-400" },
];

export function CpuControlRail({
  state,
  busy,
  onSelectPreset,
  onOpenImport,
  onStep,
  onControl,
  onToggleFault,
}: CpuControlRailProps) {
  const { cpu, project, presets, faults, fault_catalog } = state;
  const watchdog = classifyWatchdogHeadroom(cpu.stats.headroom_pct);
  const usedPct = Math.min(100, Math.max(0, 100 - cpu.stats.headroom_pct));
  const quickFaults = fault_catalog.slice(0, 3);

  return (
    <aside
      aria-label="S7 CPU Panel and Program Controls"
      className="w-[272px] shrink-0 bg-[#121821] border-r border-[#243041] flex flex-col overflow-y-auto"
    >
      {/* Program Preset & AI Program Import */}
      <section className="p-3.5 border-b border-[#243041] space-y-2.5">
        <div className="flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">
            STEP 7 Project
          </h2>
          <span className="font-mono text-[11px] text-[#64748B]" title={project.sha256}>
            {shortSha256(project.sha256)}
          </span>
        </div>

        <label className="block">
          <span className="sr-only">Select Siemens preset project</span>
          <select
            aria-label="Select Siemens preset project"
            value={presets.some((p) => p.id === project.id) ? project.id : ""}
            onChange={(e) => {
              if (e.target.value) onSelectPreset(e.target.value);
            }}
            disabled={busy}
            className="w-full h-8 px-2.5 rounded bg-[#0B0F14] border border-[#243041] text-xs text-[#F1F5F9] focus:outline-none focus:border-[#3B82F6]"
          >
            {!presets.some((p) => p.id === project.id) && (
              <option value="">Custom: {project.name}</option>
            )}
            {presets.map((preset) => (
              <option key={preset.id} value={preset.id}>
                {preset.name}
              </option>
            ))}
          </select>
        </label>

        <div className="text-[11px] text-[#94A3B8] flex items-center justify-between font-mono">
          <span>{project.cpu_family}</span>
          <span className="text-[#64748B]">{project.order_number}</span>
        </div>

        <button
          type="button"
          onClick={onOpenImport}
          className="w-full h-8 px-2.5 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs font-medium text-[#F1F5F9] flex items-center justify-center gap-1.5 transition-colors cursor-pointer"
        >
          <Upload className="w-3.5 h-3.5 text-[#3B82F6]" aria-hidden="true" />
          <span>Import STEP 7 Code</span>
        </button>
      </section>

      {/* S7 Front-Panel LEDs */}
      <section className="p-3.5 border-b border-[#243041] space-y-2.5">
        <div className="flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">
            CPU Front Panel
          </h2>
          <span className="font-mono text-[11px] font-semibold text-[#F1F5F9]">{cpu.mode}</span>
        </div>

        <div className="grid grid-cols-3 gap-1.5" role="group" aria-label="CPU status LEDs">
          {LED_SPECS.map((led) => {
            const on = cpu.leds[led.key];
            return (
              <div
                key={led.key}
                data-led={led.key}
                data-state={on ? "on" : "off"}
                className={`h-7 px-2 rounded border flex items-center justify-between font-mono text-[11px] font-semibold ${
                  on ? led.onClass : "bg-[#0B0F14] text-[#64748B] border-[#243041]"
                }`}
              >
                <span>{led.label}</span>
                <span className="text-[10px]">{on ? "ON" : "OFF"}</span>
              </div>
            );
          })}
        </div>
      </section>

      {/* Deterministic Execution Controls */}
      <section className="p-3.5 border-b border-[#243041] space-y-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">
          Deterministic Scan Control
        </h2>

        <div className="grid grid-cols-2 gap-1.5">
          <button
            type="button"
            disabled={busy}
            onClick={() => onStep(1)}
            className="h-8 px-2 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs font-medium text-[#F1F5F9] flex items-center justify-center gap-1.5 cursor-pointer disabled:opacity-50"
          >
            <StepForward className="w-3.5 h-3.5 text-[#3B82F6]" aria-hidden="true" />
            <span>Step 1</span>
          </button>

          <button
            type="button"
            disabled={busy}
            onClick={() => onStep(10)}
            className="h-8 px-2 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs font-medium text-[#F1F5F9] flex items-center justify-center gap-1.5 cursor-pointer disabled:opacity-50"
          >
            <FastForward className="w-3.5 h-3.5 text-[#3B82F6]" aria-hidden="true" />
            <span>Step 10</span>
          </button>

          <button
            type="button"
            disabled={busy}
            onClick={() => onControl("warm_restart")}
            className="h-8 px-2 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs font-medium text-[#F1F5F9] flex items-center justify-center gap-1.5 cursor-pointer disabled:opacity-50"
          >
            <RotateCcw className="w-3.5 h-3.5 text-amber-400" aria-hidden="true" />
            <span>Warm OB100</span>
          </button>

          <button
            type="button"
            disabled={busy}
            onClick={() => onControl("cold_reset")}
            className="h-8 px-2 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs font-medium text-[#F1F5F9] flex items-center justify-center gap-1.5 cursor-pointer disabled:opacity-50"
          >
            <Power className="w-3.5 h-3.5 text-red-400" aria-hidden="true" />
            <span>Cold MRES</span>
          </button>
        </div>
      </section>

      {/* Scan-Cycle Watchdog Profiler */}
      <section className="p-3.5 border-b border-[#243041] space-y-2.5">
        <div className="flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">
            Scan-Cycle Profiler
          </h2>
          <span className={`font-mono text-[11px] font-semibold ${watchdog.textClass}`}>
            {watchdog.status}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="bg-[#0B0F14] border border-[#243041] rounded px-2 py-1.5">
            <div className="text-[10px] text-[#64748B]">OB1 Scans</div>
            <div className="font-mono font-semibold text-[#F1F5F9] tabular-nums" data-testid="scan-count">
              {cpu.stats.scan_count}
            </div>
          </div>
          <div className="bg-[#0B0F14] border border-[#243041] rounded px-2 py-1.5">
            <div className="text-[10px] text-[#64748B]">Sim Time</div>
            <div className="font-mono font-semibold text-[#F1F5F9] tabular-nums">
              {formatSimTime(cpu.stats.sim_time_ms)}
            </div>
          </div>
        </div>

        <div className="grid grid-cols-3 gap-1.5 text-[11px] font-mono tabular-nums">
          <div className="bg-[#0B0F14] border border-[#243041] rounded px-1.5 py-1">
            <div className="text-[9px] text-[#64748B]">MIN</div>
            <div className="text-[#F1F5F9]">{cpu.stats.min_scan_ms.toFixed(1)}ms</div>
          </div>
          <div className="bg-[#0B0F14] border border-[#243041] rounded px-1.5 py-1">
            <div className="text-[9px] text-[#64748B]">AVG</div>
            <div className="text-[#F1F5F9]">{cpu.stats.avg_scan_ms.toFixed(1)}ms</div>
          </div>
          <div className="bg-[#0B0F14] border border-[#243041] rounded px-1.5 py-1">
            <div className="text-[9px] text-[#64748B]">MAX</div>
            <div className="text-[#F1F5F9]">{cpu.stats.max_scan_ms.toFixed(1)}ms</div>
          </div>
        </div>

        <div className="space-y-1">
          <div className="flex items-center justify-between text-[11px] font-mono tabular-nums">
            <span className="text-[#94A3B8]">Watchdog ({cpu.stats.watchdog_limit_ms}ms)</span>
            <span className={watchdog.textClass}>{cpu.stats.headroom_pct.toFixed(1)}% free</span>
          </div>
          <div className="h-1.5 rounded bg-[#0B0F14] border border-[#243041] overflow-hidden">
            <div
              className={`h-full transition-all ${watchdog.barClass}`}
              style={{ width: `${usedPct}%` }}
            />
          </div>
        </div>

        <div className="flex items-center justify-between text-[11px] font-mono tabular-nums pt-0.5">
          <span className="text-[#94A3B8]">Network Coverage</span>
          <span className="text-[#F1F5F9]">
            {project.covered_networks}/{project.total_networks} ({project.coverage_pct.toFixed(0)}%)
          </span>
        </div>
      </section>

      {/* Quick Hardware Fault Interlocks */}
      <section className="p-3.5 space-y-2">
        <div className="flex items-center gap-1.5">
          <ShieldAlert className="w-3.5 h-3.5 text-amber-400" aria-hidden="true" />
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">
            Quick Fault Triggers
          </h2>
        </div>

        <div className="space-y-1.5">
          {quickFaults.map((fault) => {
            const active = Boolean(faults[fault.id]);
            return (
              <button
                key={fault.id}
                type="button"
                disabled={busy}
                onClick={() => onToggleFault(fault.id, !active)}
                className={`w-full px-2.5 py-1.5 rounded border text-left text-xs flex items-center justify-between gap-2 transition-colors cursor-pointer disabled:opacity-50 ${
                  active
                    ? "bg-red-500/15 border-red-500/50 text-red-200"
                    : "bg-[#0B0F14] border-[#243041] text-[#CBD5E1] hover:bg-[#171F2B]"
                }`}
              >
                <span className="truncate font-medium">{fault.name}</span>
                <span className="font-mono text-[10px] shrink-0 font-semibold">
                  {active ? "ACTIVE" : "NORMAL"}
                </span>
              </button>
            );
          })}
        </div>
      </section>
    </aside>
  );
}
