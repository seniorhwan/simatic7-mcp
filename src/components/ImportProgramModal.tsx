import { useState } from "react";
import { X, FileCode2 } from "lucide-react";

interface ImportProgramModalProps {
  open: boolean;
  busy: boolean;
  onClose: () => void;
  onImport: (payload: {
    name: string;
    cpu_family: string;
    source_code: string;
    symbols: string;
  }) => Promise<void>;
}

const SAMPLE_AI_SOURCE = `ORGANIZATION_BLOCK OB 1
TITLE = AI-Generated Pump Interlock Verification Program
BEGIN
NETWORK
TITLE = Start/Stop Seal-In With E-Stop Interlock
      A     I0.0;
      O     Q0.0;
      AN    I0.1;
      A     I0.2;
      =     Q0.0;
NETWORK
TITLE = Run Supervision Timer T1 (2s)
      A     Q0.0;
      L     S5T#2S;
      SD    T 1;
      A     T 1;
      =     Q0.1;
END_ORGANIZATION_BLOCK`;

const SAMPLE_SYMBOLS = `Start_PB,I0.0,BOOL
Stop_PB,I0.1,BOOL
EStop_OK,I0.2,BOOL
Pump_Run,Q0.0,BOOL
Pump_Verified,Q0.1,BOOL`;

export function ImportProgramModal({ open, busy, onClose, onImport }: ImportProgramModalProps) {
  const [name, setName] = useState("AI-Generated Pump Interlock");
  const [cpuFamily, setCpuFamily] = useState("S7-300");
  const [sourceCode, setSourceCode] = useState(SAMPLE_AI_SOURCE);
  const [symbols, setSymbols] = useState(SAMPLE_SYMBOLS);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!sourceCode.trim()) {
      setError("Paste at least one Siemens STEP 7 block (OB/FB/FC/DB/SCL/GRAPH).");
      return;
    }
    try {
      await onImport({
        name: name.trim() || "Custom STEP 7 Program",
        cpu_family: cpuFamily,
        source_code: sourceCode,
        symbols,
      });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to compile STEP 7 source.");
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="import-modal-title"
      className="fixed inset-0 z-50 bg-black/70 flex items-center justify-center p-4"
    >
      <div className="w-full max-w-3xl bg-[#121821] border border-[#243041] rounded-md overflow-hidden flex flex-col max-h-[90vh]">
        <div className="px-4 py-3 bg-[#171F2B] border-b border-[#243041] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <FileCode2 className="w-4 h-4 text-[#3B82F6]" aria-hidden="true" />
            <h2 id="import-modal-title" className="font-display text-sm font-semibold text-[#F1F5F9]">
              Import AI-Generated Siemens STEP 7 Program
            </h2>
          </div>
          <button
            type="button"
            aria-label="Close import modal"
            onClick={onClose}
            className="p-1 rounded text-[#94A3B8] hover:text-[#F1F5F9] cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-4 space-y-3 overflow-y-auto">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <label className="sm:col-span-2 block space-y-1">
              <span className="text-xs font-medium text-[#CBD5E1]">Project Name</span>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                className="w-full h-8 px-2.5 rounded bg-[#0B0F14] border border-[#243041] text-xs text-[#F1F5F9]"
              />
            </label>

            <label className="block space-y-1">
              <span className="text-xs font-medium text-[#CBD5E1]">Target CPU Family</span>
              <select
                value={cpuFamily}
                onChange={(e) => setCpuFamily(e.target.value)}
                className="w-full h-8 px-2.5 rounded bg-[#0B0F14] border border-[#243041] text-xs text-[#F1F5F9]"
              >
                <option value="S7-300">SIMATIC S7-300</option>
                <option value="S7-400">SIMATIC S7-400</option>
                <option value="WinAC">SIMATIC WinAC RTX</option>
              </select>
            </label>
          </div>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-[#CBD5E1]">
              STEP 7 Source Code (AWL/STL, SCL, S7-GRAPH, DB)
            </span>
            <textarea
              rows={11}
              value={sourceCode}
              onChange={(e) => setSourceCode(e.target.value)}
              spellCheck={false}
              className="w-full p-2.5 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9] leading-relaxed"
            />
          </label>

          <label className="block space-y-1">
            <span className="text-xs font-medium text-[#CBD5E1]">
              Symbol Table (CSV: Symbol,Address,Type or JSON)
            </span>
            <textarea
              rows={4}
              value={symbols}
              onChange={(e) => setSymbols(e.target.value)}
              spellCheck={false}
              className="w-full p-2.5 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9]"
            />
          </label>

          {error && (
            <div className="px-3 py-2 rounded bg-red-500/15 border border-red-500/50 text-xs text-red-200">
              {error}
            </div>
          )}

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-[#243041]">
            <button
              type="button"
              onClick={onClose}
              className="h-8 px-3 rounded border border-[#243041] text-xs font-medium text-[#CBD5E1] hover:bg-[#171F2B] cursor-pointer"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy}
              className="h-8 px-4 rounded bg-[#3B82F6] hover:bg-[#2563EB] text-xs font-semibold text-white cursor-pointer disabled:opacity-50"
            >
              {busy ? "Compiling..." : "Compile & Load Simulator"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
