import { useState } from "react";
import {
  Zap,
  ShieldCheck,
  Play,
  FileDown,
  ChevronDown,
  ChevronRight,
  RotateCcw,
  AlertTriangle,
} from "lucide-react";
import type { SimState } from "../types/sim";
import { formatSimTime } from "../utils/formatters";

interface FaultsAndFatViewProps {
  state: SimState;
  busy: boolean;
  onToggleFault: (faultId: string, active: boolean) => void;
  onBrownoutRestart: () => void;
  onClearAllFaults: () => void;
  onRunFat: (testId?: string) => void;
  onExportCertificate: (format: "markdown" | "json") => void;
}

export function FaultsAndFatView({
  state,
  busy,
  onToggleFault,
  onBrownoutRestart,
  onClearAllFaults,
  onRunFat,
  onExportCertificate,
}: FaultsAndFatViewProps) {
  const [expandedCases, setExpandedCases] = useState<Record<string, boolean>>({});
  const { fault_catalog, faults, diagnostic_buffer, fat_suite, fat_results } = state;

  const toggleCase = (id: string) =>
    setExpandedCases((prev) => ({ ...prev, [id]: !prev[id] }));

  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-4">
      {/* Row 1: Automated FAT Suite & Sign-Off Certificate */}
      <section className="rounded bg-[#121821] border border-[#243041] overflow-hidden">
        <header className="px-4 py-3 bg-[#171F2B] border-b border-[#243041] flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-emerald-400" aria-hidden="true" />
            <div>
              <h2 className="font-display text-sm font-semibold text-[#F1F5F9]">
                Automated Factory Acceptance Test (FAT) Suite
              </h2>
              <p className="text-[11px] text-[#94A3B8]">
                Runs each scenario on an isolated simulator clone without disturbing your live session
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => onRunFat()}
              className="h-8 px-3.5 rounded bg-[#3B82F6] hover:bg-[#2563EB] text-xs font-semibold text-white flex items-center gap-1.5 cursor-pointer disabled:opacity-50"
            >
              <Play className="w-3.5 h-3.5 fill-current" aria-hidden="true" />
              <span>Run Full FAT Suite</span>
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onExportCertificate("markdown")}
              className="h-8 px-3 rounded bg-[#0B0F14] hover:bg-[#1E293B] border border-[#243041] text-xs font-medium text-[#CBD5E1] flex items-center gap-1.5 cursor-pointer"
            >
              <FileDown className="w-3.5 h-3.5" aria-hidden="true" />
              <span>Export Markdown</span>
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => onExportCertificate("json")}
              className="h-8 px-3 rounded bg-[#0B0F14] hover:bg-[#1E293B] border border-[#243041] text-xs font-medium text-[#CBD5E1] flex items-center gap-1.5 cursor-pointer"
            >
              <FileDown className="w-3.5 h-3.5" aria-hidden="true" />
              <span>Export JSON</span>
            </button>
          </div>
        </header>

        <div className="p-4 space-y-3">
          {/* Summary KPI Row */}
          {fat_results.executed && (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 text-xs font-mono tabular-nums">
              <div className="bg-[#0B0F14] border border-[#243041] rounded px-3 py-2">
                <div className="text-[10px] text-[#64748B]">VERDICT</div>
                <div
                  data-testid="fat-verdict"
                  className={`text-sm font-bold ${fat_results.passed ? "text-emerald-400" : "text-red-400"}`}
                >
                  {fat_results.passed ? "PASS" : "FAIL"}
                </div>
              </div>
              <div className="bg-[#0B0F14] border border-[#243041] rounded px-3 py-2">
                <div className="text-[10px] text-[#64748B]">TESTS PASSED</div>
                <div className="text-sm font-bold text-[#F1F5F9]">
                  {fat_results.passed_count}/{fat_results.total_count}
                </div>
              </div>
              <div className="bg-[#0B0F14] border border-[#243041] rounded px-3 py-2">
                <div className="text-[10px] text-[#64748B]">NETWORK COVERAGE</div>
                <div className="text-sm font-bold text-emerald-400">
                  {(fat_results.fat_network_coverage_pct ?? 0).toFixed(1)}%
                </div>
              </div>
              <div className="bg-[#0B0F14] border border-[#243041] rounded px-3 py-2">
                <div className="text-[10px] text-[#64748B]">DURATION</div>
                <div className="text-sm font-bold text-[#F1F5F9]">
                  {(fat_results.duration_ms ?? 0).toFixed(1)} ms
                </div>
              </div>
            </div>
          )}

          {/* FAT Case List */}
          <div className="divide-y divide-[#243041] border border-[#243041] rounded bg-[#0B0F14]">
            {fat_suite.map((spec) => {
              const result = fat_results.cases.find((c) => c.id === spec.id);
              const isOpen = Boolean(expandedCases[spec.id]);
              return (
                <div key={spec.id} className="p-3">
                  <div className="flex items-center justify-between gap-2">
                    <button
                      type="button"
                      onClick={() => toggleCase(spec.id)}
                      aria-expanded={isOpen}
                      className="flex items-center gap-2 text-left min-w-0 flex-1 cursor-pointer"
                    >
                      {isOpen ? (
                        <ChevronDown className="w-3.5 h-3.5 text-[#94A3B8] shrink-0" aria-hidden="true" />
                      ) : (
                        <ChevronRight className="w-3.5 h-3.5 text-[#94A3B8] shrink-0" aria-hidden="true" />
                      )}
                      <span className="font-mono text-xs text-[#3B82F6] shrink-0">{spec.id}</span>
                      <span className="text-xs font-semibold text-[#F1F5F9] truncate">{spec.name}</span>
                      <span className="text-[11px] text-[#64748B] hidden sm:inline">
                        — {spec.category}
                      </span>
                    </button>

                    <div className="flex items-center gap-2 shrink-0">
                      <span
                        className={`font-mono text-xs font-semibold ${
                          !result
                            ? "text-[#64748B]"
                            : result.passed
                              ? "text-emerald-400"
                              : "text-red-400"
                        }`}
                      >
                        {!result ? "PENDING" : result.passed ? "PASS" : "FAIL"}
                      </span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onRunFat(spec.id)}
                        className="h-6 px-2 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-[11px] text-[#CBD5E1] cursor-pointer"
                      >
                        Run
                      </button>
                    </div>
                  </div>

                  {isOpen && (
                    <div className="mt-2.5 pl-6 space-y-2">
                      <p className="text-xs text-[#94A3B8]">{spec.description}</p>
                      {result && (
                        <table className="w-full text-left border-collapse font-mono text-[11px]">
                          <thead>
                            <tr className="text-[#64748B] border-b border-[#243041]">
                              <th className="py-1 pr-2">Assertion</th>
                              <th className="py-1 px-2">Address</th>
                              <th className="py-1 px-2">Expected</th>
                              <th className="py-1 px-2">Actual</th>
                              <th className="py-1 pl-2 text-right">Status</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-[#1E293B]">
                            {result.assertions.map((a, idx) => (
                              <tr key={idx}>
                                <td className="py-1 pr-2 text-[#CBD5E1] font-sans">{a.label}</td>
                                <td className="py-1 px-2 text-[#3B82F6]">{a.address}</td>
                                <td className="py-1 px-2 text-[#94A3B8]">{String(a.expected)}</td>
                                <td className="py-1 px-2 text-[#F1F5F9]">{String(a.actual)}</td>
                                <td
                                  className={`py-1 pl-2 text-right font-semibold ${
                                    a.passed ? "text-emerald-400" : "text-red-400"
                                  }`}
                                >
                                  {a.passed ? "PASS" : "FAIL"}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
            {fat_suite.length === 0 && (
              <div className="p-4 text-xs text-[#64748B]">
                No predefined FAT cases for custom imported code. Load a Siemens preset or run interactive verification via VAT &amp; Fault Injection.
              </div>
            )}
          </div>
        </div>
      </section>

      {/* Row 2: Hardware & OB Fault Injection Matrix */}
      <section className="rounded bg-[#121821] border border-[#243041] overflow-hidden">
        <header className="px-4 py-2.5 bg-[#171F2B] border-b border-[#243041] flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Zap className="w-4 h-4 text-amber-400" aria-hidden="true" />
            <h2 className="font-display text-sm font-semibold text-[#F1F5F9]">
              Hardware &amp; Siemens OB Fault Injection
            </h2>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={onBrownoutRestart}
              className="h-7 px-2.5 rounded bg-amber-500/15 hover:bg-amber-500/25 border border-amber-500/50 text-xs font-medium text-amber-300 flex items-center gap-1.5 cursor-pointer"
            >
              <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />
              <span>24V Brownout -&gt; OB100</span>
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={onClearAllFaults}
              className="h-7 px-2.5 rounded bg-[#0B0F14] hover:bg-[#1E293B] border border-[#243041] text-xs text-[#CBD5E1] flex items-center gap-1 cursor-pointer"
            >
              <RotateCcw className="w-3 h-3" aria-hidden="true" />
              <span>Clear All Faults</span>
            </button>
          </div>
        </header>

        <div className="p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {fault_catalog.map((fault) => {
            const active = Boolean(faults[fault.id]);
            return (
              <div
                key={fault.id}
                className={`p-3 rounded border flex flex-col justify-between gap-2.5 ${
                  active
                    ? "bg-red-500/10 border-red-500/60"
                    : "bg-[#0B0F14] border-[#243041]"
                }`}
              >
                <div className="space-y-1">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="text-xs font-semibold text-[#F1F5F9]">{fault.name}</h3>
                    <span className="font-mono text-[10px] font-semibold text-[#3B82F6] shrink-0">
                      {fault.ob}
                    </span>
                  </div>
                  <p className="text-[11px] text-[#94A3B8] leading-snug">{fault.description}</p>
                </div>

                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onToggleFault(fault.id, !active)}
                  className={`h-7 px-2.5 rounded border text-xs font-semibold cursor-pointer ${
                    active
                      ? "bg-red-500 text-white border-red-400 hover:bg-red-600"
                      : "bg-[#171F2B] hover:bg-[#1E293B] text-[#CBD5E1] border-[#243041]"
                  }`}
                >
                  {active ? "Fault Active — Click to Clear" : "Inject Fault"}
                </button>
              </div>
            );
          })}
        </div>
      </section>

      {/* Row 3: Siemens CPU Diagnostic Buffer */}
      <section className="rounded bg-[#121821] border border-[#243041] overflow-hidden">
        <header className="px-4 py-2.5 bg-[#171F2B] border-b border-[#243041] flex items-center justify-between">
          <h2 className="font-display text-sm font-semibold text-[#F1F5F9]">
            Siemens CPU Diagnostic Buffer
          </h2>
          <span className="font-mono text-xs text-[#94A3B8] tabular-nums">
            {diagnostic_buffer.length} events
          </span>
        </header>

        <div className="max-h-60 overflow-y-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead className="sticky top-0 bg-[#121821] border-b border-[#243041] text-[11px] text-[#94A3B8]">
              <tr>
                <th className="py-1.5 px-3 font-medium">#</th>
                <th className="py-1.5 px-2 font-medium">Event ID</th>
                <th className="py-1.5 px-2 font-medium">OB</th>
                <th className="py-1.5 px-2 font-medium">Scan / Time</th>
                <th className="py-1.5 px-3 font-medium">Diagnostic Message</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1E293B] font-mono text-[11px]">
              {diagnostic_buffer.map((ev) => (
                <tr key={ev.seq}>
                  <td className="py-1.5 px-3 text-[#64748B] tabular-nums">{ev.seq}</td>
                  <td
                    className={`py-1.5 px-2 font-semibold ${
                      ev.severity === "error"
                        ? "text-red-400"
                        : ev.severity === "warning"
                          ? "text-amber-300"
                          : "text-emerald-400"
                    }`}
                  >
                    {ev.event_id}
                  </td>
                  <td className="py-1.5 px-2 text-[#CBD5E1]">
                    {ev.ob_number > 0 ? `OB${ev.ob_number}` : "SYS"}
                  </td>
                  <td className="py-1.5 px-2 text-[#94A3B8] tabular-nums">
                    #{ev.scan_count} / {formatSimTime(ev.sim_time_ms)}
                  </td>
                  <td className="py-1.5 px-3 font-sans text-xs text-[#F1F5F9]">{ev.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
