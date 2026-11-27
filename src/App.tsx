import { useCallback, useEffect, useRef, useState } from "react";
import { simApi } from "./api/simClient";
import type { SimState, WorkspaceTab } from "./types/sim";
import { TopBar } from "./components/TopBar";
import { CpuControlRail } from "./components/CpuControlRail";
import { LadderDebuggerView } from "./components/LadderDebuggerView";
import { PlantAndTracerView } from "./components/PlantAndTracerView";
import { FaultsAndFatView } from "./components/FaultsAndFatView";
import { AuditAndBridgeView } from "./components/AuditAndBridgeView";
import { VatForceDock } from "./components/VatForceDock";
import { ImportProgramModal } from "./components/ImportProgramModal";

function downloadTextFile(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

export function App() {
  const [state, setState] = useState<SimState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("debugger");
  const [xrefFilter, setXrefFilter] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const stepInFlightRef = useRef(false);

  const showNotice = useCallback((msg: string) => {
    setNotice(msg);
  }, []);

  useEffect(() => {
    if (!notice) return;
    const id = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(id);
  }, [notice]);

  const loadInitial = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const s = await simApi.getState();
      setState(s);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to connect to SIMATIC verification engine.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadInitial();
  }, [loadInitial]);

  // Controlled deterministic live stepping while CPU is in RUN or RUN_P mode
  const isRunning = state?.cpu.mode === "RUN" || state?.cpu.mode === "RUN_P";
  useEffect(() => {
    if (!isRunning) return;
    const timer = window.setInterval(async () => {
      if (stepInFlightRef.current) return;
      stepInFlightRef.current = true;
      try {
        const res = await simApi.step(2, false);
        setState(res.state);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Scan execution error");
      } finally {
        stepInFlightRef.current = false;
      }
    }, 280);
    return () => window.clearInterval(timer);
  }, [isRunning]);

  const runAction = useCallback(async (fn: () => Promise<{ state?: SimState }>, okMessage?: string) => {
    setBusy(true);
    setError(null);
    try {
      const res = await fn();
      if (res.state) setState(res.state);
      if (okMessage) showNotice(okMessage);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Operation failed");
    } finally {
      setBusy(false);
    }
  }, [showNotice]);

  const handleToggleRun = () => {
    if (!state) return;
    const nextAction = isRunning ? "stop" : "run_p";
    runAction(() => simApi.control(nextAction));
  };

  const handleSelectPreset = (presetId: string) => {
    runAction(() => simApi.loadPreset(presetId), `Loaded preset: ${presetId}`);
  };

  const handleImportCustom = async (payload: {
    name: string;
    cpu_family: string;
    source_code: string;
    symbols: string;
  }) => {
    setBusy(true);
    try {
      const res = await simApi.loadCustom(payload);
      setState(res.state);
      showNotice(`Loaded custom STEP 7 program: ${payload.name}`);
    } finally {
      setBusy(false);
    }
  };

  const handleStep = (cycles: number) => {
    runAction(() => simApi.step(cycles, true));
  };

  const handleControl = (action: string, extra?: Record<string, unknown>) => {
    runAction(() => simApi.control(action, extra), extra ? undefined : `CPU command executed: ${action}`);
  };

  const handleWriteTag = (address: string, value: boolean | number | string, dataType?: string) => {
    runAction(() => simApi.writeTag(address, value, dataType));
  };

  const handlePulseInput = async (address: string, activeLow = false) => {
    setBusy(true);
    setError(null);
    try {
      await simApi.writeTag(address, !activeLow, "BOOL");
      await simApi.step(2, true);
      await simApi.writeTag(address, activeLow, "BOOL");
      const res = await simApi.step(1, true);
      setState(res.state);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Momentary input pulse failed");
    } finally {
      setBusy(false);
    }
  };

  const handleForceSet = (address: string, value: boolean | number | string, dataType?: string) => {
    runAction(
      () => simApi.force({ action: "set", address, value, data_type: dataType }),
      `Forced ${address} = ${String(value)}`,
    );
  };

  const handleForceRemove = (address: string) => {
    runAction(() => simApi.force({ action: "remove", address }), `Removed force on ${address}`);
  };

  const handleForceClearAll = () => {
    runAction(() => simApi.force({ action: "clear_all" }), "Cleared all forced values");
  };

  const handleSetFormat = (table: string, address: string, format: string) => {
    runAction(() => simApi.vat({ action: "set_format", table, address, format }));
  };

  const handleAddTag = (
    table: string,
    address: string,
    symbol: string,
    dataType: string,
    comment: string,
  ) => {
    runAction(
      () => simApi.vat({ action: "add_tag", tab: table, address, symbol, data_type: dataType, comment }),
      `Added ${address} to ${table}`,
    );
  };

  const handleToggleFault = (faultId: string, active: boolean) => {
    runAction(
      () => simApi.fault({ fault_id: faultId, active }),
      active ? `Injected fault: ${faultId}` : `Cleared fault: ${faultId}`,
    );
  };

  const handleBrownoutRestart = () => {
    runAction(
      () => simApi.fault({ action: "brownout_warm_restart" }),
      "Triggered 24V brownout -> OB100 warm restart",
    );
  };

  const handleClearAllFaults = () => {
    runAction(() => simApi.fault({ action: "clear_all" }), "Cleared all injected hardware faults");
  };

  const handleToggleNetworkBreakpoint = (networkId: string) => {
    runAction(() => simApi.breakpoints({ action: "toggle_network", network_id: networkId }));
  };

  const handleAddConditionBreakpoint = (address: string, operator: string, value: string) => {
    const parsedVal: string | number | boolean =
      value.toLowerCase() === "true"
        ? true
        : value.toLowerCase() === "false"
          ? false
          : !Number.isNaN(Number(value)) && value !== ""
            ? Number(value)
            : value;
    runAction(
      () => simApi.breakpoints({ action: "add_condition", address, operator, value: parsedVal }),
      `Added conditional breakpoint: ${address} ${operator} ${value}`,
    );
  };

  const handleRemoveConditionBreakpoint = (id: string) => {
    runAction(() => simApi.breakpoints({ action: "remove", id }));
  };

  const handleTraceAction = async (payload: Record<string, unknown>) => {
    await runAction(() => simApi.trace(payload));
  };

  const handleExportCsv = async () => {
    try {
      const res = await simApi.trace({ action: "export_csv" });
      if (res.csv) {
        downloadTextFile(
          `${state?.project.id || "simatic"}_trace.csv`,
          res.csv,
          "text/csv;charset=utf-8",
        );
        showNotice(`Exported ${res.row_count ?? 0} trace samples to CSV`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Trace CSV export failed");
    }
  };

  const handleRunFat = (testId?: string) => {
    runAction(
      () => simApi.runFat(testId),
      testId ? `Completed FAT case ${testId}` : "Completed full FAT suite",
    );
  };

  const handleExportCertificate = async (format: "markdown" | "json") => {
    try {
      const res = await simApi.exportFatReport(format);
      const ext = format === "markdown" ? "md" : "json";
      const mime = format === "markdown" ? "text/markdown;charset=utf-8" : "application/json;charset=utf-8";
      downloadTextFile(`${state?.project.id || "simatic"}_fat_certificate.${ext}`, res.content, mime);
      showNotice(`Exported FAT Sign-Off Certificate (.${ext})`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "FAT certificate export failed");
    }
  };

  const handleInspectOperand = (address: string) => {
    setXrefFilter(address);
    setActiveTab("audit_bridge");
  };

  if (loading && !state) {
    return (
      <div className="h-screen w-screen bg-[#0B0F14] text-[#F1F5F9] flex items-center justify-center">
        <div className="text-center space-y-2">
          <div className="font-display text-sm font-semibold">Initializing SIMATIC Verify Studio</div>
          <div className="font-mono text-xs text-[#94A3B8]">
            Starting Siemens S7-300 / S7-400 Virtual Verification Engine...
          </div>
        </div>
      </div>
    );
  }

  if (!state) {
    return (
      <div className="h-screen w-screen bg-[#0B0F14] text-[#F1F5F9] flex items-center justify-center p-6">
        <div className="max-w-md w-full p-5 rounded bg-[#121821] border border-red-500/50 space-y-3">
          <h1 className="font-display text-base font-semibold text-red-300">
            Verification Engine Unavailable
          </h1>
          <p className="text-xs text-[#CBD5E1]">{error || "Could not connect to the backend simulator."}</p>
          <button
            type="button"
            onClick={loadInitial}
            className="h-8 px-4 rounded bg-[#3B82F6] text-xs font-semibold text-white cursor-pointer"
          >
            Retry Connection
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen w-screen overflow-hidden bg-[#0B0F14] text-[#F1F5F9] flex flex-col">
      <TopBar
        activeTab={activeTab}
        onSelectTab={setActiveTab}
        cpuMode={state.cpu.mode}
        busy={busy}
        onToggleRun={handleToggleRun}
      />

      {(error || notice) && (
        <div
          role="status"
          className={`px-4 py-1.5 text-xs border-b flex items-center justify-between ${
            error
              ? "bg-red-500/15 border-red-500/40 text-red-200"
              : "bg-[#171F2B] border-[#243041] text-[#CBD5E1]"
          }`}
        >
          <span>{error || notice}</span>
          <button
            type="button"
            onClick={() => {
              setError(null);
              setNotice(null);
            }}
            className="text-[11px] underline ml-4 cursor-pointer"
          >
            Dismiss
          </button>
        </div>
      )}

      <div className="flex-1 min-h-0 flex overflow-hidden">
        <CpuControlRail
          state={state}
          busy={busy}
          onSelectPreset={handleSelectPreset}
          onOpenImport={() => setImportOpen(true)}
          onStep={handleStep}
          onControl={handleControl}
          onToggleFault={handleToggleFault}
        />

        <main className="flex-1 min-w-0 flex flex-col overflow-hidden">
          {activeTab === "debugger" && (
            <LadderDebuggerView
              state={state}
              busy={busy}
              onToggleNetworkBreakpoint={handleToggleNetworkBreakpoint}
              onAddConditionBreakpoint={handleAddConditionBreakpoint}
              onRemoveConditionBreakpoint={handleRemoveConditionBreakpoint}
              onInspectOperand={handleInspectOperand}
            />
          )}

          {activeTab === "plant_tracer" && (
            <PlantAndTracerView
              state={state}
              busy={busy}
              onWriteTag={handleWriteTag}
              onPulseInput={handlePulseInput}
              onControl={handleControl}
              onTraceAction={handleTraceAction}
              onExportCsv={handleExportCsv}
            />
          )}

          {activeTab === "faults_fat" && (
            <FaultsAndFatView
              state={state}
              busy={busy}
              onToggleFault={handleToggleFault}
              onBrownoutRestart={handleBrownoutRestart}
              onClearAllFaults={handleClearAllFaults}
              onRunFat={handleRunFat}
              onExportCertificate={handleExportCertificate}
            />
          )}

          {activeTab === "audit_bridge" && (
            <AuditAndBridgeView state={state} initialXrefFilter={xrefFilter} />
          )}

          <VatForceDock
            state={state}
            busy={busy}
            onWriteTag={handleWriteTag}
            onForceSet={handleForceSet}
            onForceRemove={handleForceRemove}
            onForceClearAll={handleForceClearAll}
            onSetFormat={handleSetFormat}
            onAddTag={handleAddTag}
          />
        </main>
      </div>

      <ImportProgramModal
        open={importOpen}
        busy={busy}
        onClose={() => setImportOpen(false)}
        onImport={handleImportCustom}
      />
    </div>
  );
}

export default App;
