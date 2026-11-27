import { useEffect, useState } from "react";
import { ShieldAlert, Search, Network, Server, Play, Square } from "lucide-react";
import type { SimState } from "../types/sim";
import { simApi } from "../api/simClient";

interface AuditAndBridgeViewProps {
  state: SimState;
  initialXrefFilter?: string;
}

export function AuditAndBridgeView({ state, initialXrefFilter = "" }: AuditAndBridgeViewProps) {
  const [xrefQuery, setXrefQuery] = useState(initialXrefFilter);
  useEffect(() => {
    setXrefQuery(initialXrefFilter);
  }, [initialXrefFilter]);
  const [plcHost, setPlcHost] = useState("192.168.0.1");
  const [plcPort, setPlcPort] = useState("102");
  const [plcRack, setPlcRack] = useState("0");
  const [plcSlot, setPlcSlot] = useState("2");
  const [plcsimResult, setPlcsimResult] = useState<string | null>(null);
  const [diagResult, setDiagResult] = useState<string | null>(null);
  const [bridgeBusy, setBridgeBusy] = useState(false);

  const filteredXref = state.cross_references.filter((row) => {
    const q = xrefQuery.trim().toLowerCase();
    if (!q) return true;
    return (
      row.address.toLowerCase().includes(q) ||
      row.symbol.toLowerCase().includes(q) ||
      row.reads.some((r) => r.toLowerCase().includes(q)) ||
      row.writes.some((w) => w.toLowerCase().includes(q))
    );
  });

  const handlePlcsim = async (action: "start" | "stop") => {
    setBridgeBusy(true);
    setPlcsimResult(null);
    try {
      const res = await simApi.bridgePlcsim(action, "RUN_P");
      setPlcsimResult(JSON.stringify(res, null, 2));
    } catch (err) {
      setPlcsimResult(err instanceof Error ? err.message : "S7-PLCSIM request failed.");
    } finally {
      setBridgeBusy(false);
    }
  };

  const handleDiagnosePlc = async (e: React.FormEvent) => {
    e.preventDefault();
    setBridgeBusy(true);
    setDiagResult(null);
    try {
      const res = await simApi.bridgeDiagnosePlc(
        plcHost.trim(),
        Number(plcPort) || 102,
        Number(plcRack) || 0,
        Number(plcSlot) || 2,
      );
      setDiagResult(JSON.stringify(res, null, 2));
    } catch (err) {
      setDiagResult(err instanceof Error ? err.message : "PLC connectivity diagnostic failed.");
    } finally {
      setBridgeBusy(false);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-4">
      {/* Row 1: Static Safety Audit */}
      <section className="rounded bg-[#121821] border border-[#243041] overflow-hidden">
        <header className="px-4 py-2.5 bg-[#171F2B] border-b border-[#243041] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <ShieldAlert className="w-4 h-4 text-[#3B82F6]" aria-hidden="true" />
            <h2 className="font-display text-sm font-semibold text-[#F1F5F9]">
              Static Safety &amp; Program Audit
            </h2>
          </div>
          <div className="font-mono text-xs flex items-center gap-3 tabular-nums">
            <span className={state.static_audit.error_count > 0 ? "text-red-400" : "text-emerald-400"}>
              {state.static_audit.error_count} Errors
            </span>
            <span className={state.static_audit.warning_count > 0 ? "text-amber-300" : "text-[#94A3B8]"}>
              {state.static_audit.warning_count} Warnings
            </span>
          </div>
        </header>

        <div className="p-4 space-y-2">
          {state.static_audit.findings.length === 0 ? (
            <div className="px-3 py-2.5 rounded bg-emerald-500/10 border border-emerald-500/40 text-xs text-emerald-300">
              No duplicate coils, unlatched set coils, or critical addressing conflicts detected across{" "}
              <span className="font-mono font-semibold">{state.static_audit.address_count}</span> referenced addresses.
            </div>
          ) : (
            state.static_audit.findings.map((finding) => (
              <div
                key={finding.id}
                className={`p-3 rounded border text-xs flex flex-col gap-1 ${
                  finding.severity === "error"
                    ? "bg-red-500/10 border-red-500/50 text-red-200"
                    : finding.severity === "warning"
                      ? "bg-amber-500/10 border-amber-500/50 text-amber-200"
                      : "bg-[#0B0F14] border-[#243041] text-[#CBD5E1]"
                }`}
              >
                <div className="flex items-center justify-between font-mono text-[11px]">
                  <span className="font-semibold">
                    [{finding.severity.toUpperCase()}] {finding.rule} — {finding.address}
                  </span>
                  <span className="text-[#94A3B8]">{finding.locations.join(", ")}</span>
                </div>
                <p>{finding.message}</p>
              </div>
            ))
          )}
        </div>
      </section>

      {/* Row 2: Searchable Cross-Reference Table */}
      <section className="rounded bg-[#121821] border border-[#243041] overflow-hidden">
        <header className="px-4 py-2.5 bg-[#171F2B] border-b border-[#243041] flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-display text-sm font-semibold text-[#F1F5F9]">
            Address Cross-Reference (I / Q / M / PIW / PQW / DB / T / C)
          </h2>
          <div className="relative">
            <Search
              className="w-3.5 h-3.5 text-[#64748B] absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none"
              aria-hidden="true"
            />
            <input
              type="text"
              aria-label="Filter cross-reference table"
              placeholder="Search address, symbol, block..."
              value={xrefQuery}
              onChange={(e) => setXrefQuery(e.target.value)}
              className="h-7 pl-8 pr-2.5 w-60 rounded bg-[#0B0F14] border border-[#243041] text-xs text-[#F1F5F9] placeholder-[#64748B]"
            />
          </div>
        </header>

        <div className="max-h-64 overflow-y-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead className="sticky top-0 bg-[#121821] border-b border-[#243041] text-[11px] text-[#94A3B8]">
              <tr>
                <th className="py-1.5 px-3 font-medium">Address</th>
                <th className="py-1.5 px-2 font-medium">Symbol</th>
                <th className="py-1.5 px-2 font-medium">Reads</th>
                <th className="py-1.5 px-3 font-medium">Writes</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1E293B]">
              {filteredXref.map((row) => (
                <tr key={row.address} className="hover:bg-[#171F2B]/50">
                  <td className="py-1.5 px-3 font-mono font-semibold text-[#3B82F6] whitespace-nowrap">
                    {row.address}
                  </td>
                  <td className="py-1.5 px-2 text-[#F1F5F9] whitespace-nowrap">
                    {row.symbol || "—"}
                  </td>
                  <td className="py-1.5 px-2 font-mono text-[11px] text-[#94A3B8]">
                    {row.reads.length > 0 ? row.reads.join(", ") : "—"}
                  </td>
                  <td
                    className={`py-1.5 px-3 font-mono text-[11px] ${
                      row.write_count > 1 ? "text-amber-300 font-semibold" : "text-[#CBD5E1]"
                    }`}
                  >
                    {row.writes.length > 0 ? row.writes.join(", ") : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Row 3: Live S7 Target Bridge Hub (VirtualS7PLC / S7-PLCSIM / Physical S7-300/400) */}
      <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Windows 32-bit S7-PLCSIM Bridge */}
        <div className="rounded bg-[#121821] border border-[#243041] overflow-hidden">
          <header className="px-4 py-2.5 bg-[#171F2B] border-b border-[#243041] flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Server className="w-4 h-4 text-[#3B82F6]" aria-hidden="true" />
              <h2 className="font-display text-sm font-semibold text-[#F1F5F9]">
                Windows S7-PLCSIM 32-bit COM Bridge
              </h2>
            </div>
            <span className="font-mono text-[11px] text-[#94A3B8]">
              {state.bridge.plcsim_com_available ? "READY (WIN32)" : `HOST: ${state.bridge.platform}`}
            </span>
          </header>
          <div className="p-4 space-y-3">
            <p className="text-xs text-[#94A3B8]">
              Triggers <span className="font-mono text-[#CBD5E1]">S7ProSim.S7ProSim</span> 32-bit COM
              automation (<span className="font-mono">s7_plcsim_start_stop</span>) to place a local
              Siemens S7-PLCSIM instance into <span className="font-mono">RUN_P</span> or{" "}
              <span className="font-mono">STOP</span> mode on Windows engineering stations.
            </p>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={bridgeBusy}
                onClick={() => handlePlcsim("start")}
                className="h-8 px-3 rounded bg-[#3B82F6] hover:bg-[#2563EB] text-xs font-semibold text-white flex items-center gap-1.5 cursor-pointer disabled:opacity-50"
              >
                <Play className="w-3.5 h-3.5 fill-current" aria-hidden="true" />
                <span>Start S7-PLCSIM (RUN_P)</span>
              </button>
              <button
                type="button"
                disabled={bridgeBusy}
                onClick={() => handlePlcsim("stop")}
                className="h-8 px-3 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-xs font-semibold text-[#F1F5F9] flex items-center gap-1.5 cursor-pointer disabled:opacity-50"
              >
                <Square className="w-3.5 h-3.5 fill-current" aria-hidden="true" />
                <span>Stop S7-PLCSIM</span>
              </button>
            </div>
            {plcsimResult && (
              <pre className="p-2.5 rounded bg-[#0B0F14] border border-[#243041] font-mono text-[11px] text-[#CBD5E1] overflow-x-auto max-h-40">
                {plcsimResult}
              </pre>
            )}
          </div>
        </div>

        {/* Physical S7-300/400 Pre-Upload Connectivity Diagnostic */}
        <div className="rounded bg-[#121821] border border-[#243041] overflow-hidden">
          <header className="px-4 py-2.5 bg-[#171F2B] border-b border-[#243041] flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Network className="w-4 h-4 text-emerald-400" aria-hidden="true" />
              <h2 className="font-display text-sm font-semibold text-[#F1F5F9]">
                Physical S7-300 / S7-400 Pre-Upload Diagnostic
              </h2>
            </div>
            <span className="font-mono text-[11px] text-emerald-400">ISO-on-TCP 102</span>
          </header>
          <form onSubmit={handleDiagnosePlc} className="p-4 space-y-3">
            <p className="text-xs text-[#94A3B8]">
              Runs the 4-layer pre-upload connectivity diagnostic (DNS, TCP/102, ISO COTP, S7comm
              PDU &amp; SZL CPU state) before hardware download.
            </p>
            <div className="grid grid-cols-4 gap-2">
              <label className="col-span-2 block space-y-1">
                <span className="text-[11px] text-[#94A3B8]">PLC IP / Host</span>
                <input
                  type="text"
                  value={plcHost}
                  onChange={(e) => setPlcHost(e.target.value)}
                  required
                  className="w-full h-8 px-2 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9]"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-[11px] text-[#94A3B8]">Rack</span>
                <input
                  type="number"
                  value={plcRack}
                  onChange={(e) => setPlcRack(e.target.value)}
                  className="w-full h-8 px-2 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9]"
                />
              </label>
              <label className="block space-y-1">
                <span className="text-[11px] text-[#94A3B8]">Slot</span>
                <input
                  type="number"
                  value={plcSlot}
                  onChange={(e) => setPlcSlot(e.target.value)}
                  className="w-full h-8 px-2 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9]"
                />
              </label>
            </div>
            <div className="flex items-center justify-between gap-2">
              <label className="flex items-center gap-2 text-[11px] text-[#94A3B8]">
                <span>Port</span>
                <input
                  type="number"
                  value={plcPort}
                  onChange={(e) => setPlcPort(e.target.value)}
                  className="w-20 h-7 px-2 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9]"
                />
              </label>
              <button
                type="submit"
                disabled={bridgeBusy}
                className="h-8 px-3.5 rounded bg-[#3B82F6] hover:bg-[#2563EB] text-xs font-semibold text-white cursor-pointer disabled:opacity-50"
              >
                {bridgeBusy ? "Diagnosing..." : "Run Connectivity Check"}
              </button>
            </div>
            {diagResult && (
              <pre className="p-2.5 rounded bg-[#0B0F14] border border-[#243041] font-mono text-[11px] text-[#CBD5E1] overflow-x-auto max-h-40">
                {diagResult}
              </pre>
            )}
          </form>
        </div>
      </section>
    </div>
  );
}
