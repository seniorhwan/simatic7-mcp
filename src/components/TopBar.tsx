import { Play, Square, Cpu } from "lucide-react";
import type { CpuMode, WorkspaceTab } from "../types/sim";

interface TopBarProps {
  activeTab: WorkspaceTab;
  onSelectTab: (tab: WorkspaceTab) => void;
  cpuMode: CpuMode;
  busy: boolean;
  onToggleRun: () => void;
}

const TABS: Array<{ id: WorkspaceTab; label: string }> = [
  { id: "debugger", label: "Debugger" },
  { id: "plant_tracer", label: "Plant & Tracer" },
  { id: "faults_fat", label: "Faults & FAT" },
  { id: "audit_bridge", label: "Audit & Bridge" },
];

export function TopBar({ activeTab, onSelectTab, cpuMode, busy, onToggleRun }: TopBarProps) {
  const isRunning = cpuMode === "RUN" || cpuMode === "RUN_P";

  return (
    <header className="h-14 shrink-0 bg-[#121821] border-b border-[#243041] px-4 flex items-center justify-between gap-4">
      {/* Zone 1: Brand Title */}
      <div className="flex items-center gap-2.5 min-w-0">
        <div className="w-8 h-8 rounded bg-[#171F2B] border border-[#243041] flex items-center justify-center text-[#3B82F6]">
          <Cpu className="w-4 h-4" aria-hidden="true" />
        </div>
        <div className="leading-tight">
          <h1 className="font-display text-sm font-semibold tracking-tight text-[#F1F5F9]">
            SIMATIC Verify Studio
          </h1>
          <p className="text-[11px] text-[#94A3B8]">STEP 7 S7-300 / S7-400 Verification Workbench</p>
        </div>
      </div>

      {/* Zone 2: Primary View Tabs (4 crisp destinations) */}
      <nav aria-label="Verification workspace views" className="flex items-center gap-1 h-full">
        {TABS.map((tab) => {
          const active = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onSelectTab(tab.id)}
              className={`h-full px-3.5 text-sm font-medium border-b-2 transition-colors cursor-pointer ${
                active
                  ? "border-[#3B82F6] text-[#F1F5F9] bg-[#171F2B]/60"
                  : "border-transparent text-[#94A3B8] hover:text-[#F1F5F9] hover:bg-[#171F2B]/30"
              }`}
            >
              {tab.label}
            </button>
          );
        })}
      </nav>

      {/* Zone 3: Single Primary CPU Run/Stop CTA */}
      <div className="flex items-center">
        <button
          type="button"
          onClick={onToggleRun}
          disabled={busy}
          className={`h-9 px-4 rounded text-sm font-semibold flex items-center gap-2 transition-colors cursor-pointer disabled:opacity-50 ${
            isRunning
              ? "bg-amber-500/15 text-amber-300 border border-amber-500/50 hover:bg-amber-500/25"
              : "bg-[#3B82F6] text-white border border-[#3B82F6] hover:bg-[#2563EB]"
          }`}
        >
          {isRunning ? (
            <>
              <Square className="w-3.5 h-3.5 fill-current" aria-hidden="true" />
              <span>Stop CPU</span>
            </>
          ) : (
            <>
              <Play className="w-3.5 h-3.5 fill-current" aria-hidden="true" />
              <span>Run CPU</span>
            </>
          )}
        </button>
      </div>
    </header>
  );
}
