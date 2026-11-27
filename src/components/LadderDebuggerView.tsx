import { useMemo, useState } from "react";
import { CircleDot, Eye, Code2, Plus, Trash2, ArrowRight, Flame } from "lucide-react";
import type { LadderElement, LadderNetwork, SimState } from "../types/sim";

interface LadderDebuggerViewProps {
  state: SimState;
  busy: boolean;
  onToggleNetworkBreakpoint: (networkId: string) => void;
  onAddConditionBreakpoint: (address: string, operator: string, value: string) => void;
  onRemoveConditionBreakpoint: (id: string) => void;
  onInspectOperand: (address: string) => void;
}

function renderElementNode(
  el: LadderElement,
  idx: number,
  onInspectOperand: (addr: string) => void,
) {
  const energized = Boolean(el.energized);
  const forced = Boolean(el.forced);
  const railColor = energized ? "bg-emerald-500" : "bg-[#334155]";
  const borderColor = forced
    ? "border-amber-500 bg-amber-500/10"
    : energized
      ? "border-emerald-500/70 bg-emerald-500/10"
      : "border-[#243041] bg-[#0B0F14]";

  if (el.kind === "contact") {
    const isNc = el.mode === "NC";
    return (
      <div key={idx} className="flex items-center">
        <div className={`w-4 h-0.5 ${railColor}`} />
        <button
          type="button"
          onClick={() => el.operand && onInspectOperand(el.operand)}
          className={`px-2 py-1.5 rounded border text-center min-w-[78px] transition-colors cursor-pointer hover:border-[#3B82F6] ${borderColor}`}
        >
          <div className="font-mono text-[10px] font-semibold text-[#F1F5F9]">
            {el.operand}
          </div>
          <div
            className={`font-mono text-xs font-bold leading-none mt-0.5 ${
              energized ? "text-emerald-400" : "text-[#94A3B8]"
            }`}
          >
            {isNc ? "--| / |--" : "--|   |--"}
          </div>
          {forced && (
            <div className="font-mono text-[9px] font-semibold text-amber-400 mt-0.5">FORCED</div>
          )}
        </button>
      </div>
    );
  }

  if (el.kind === "coil") {
    const symbol = el.mode === "SET" ? "( S )" : el.mode === "RESET" ? "( R )" : "(   )";
    return (
      <div key={idx} className="flex items-center">
        <div className={`w-5 h-0.5 ${railColor}`} />
        <button
          type="button"
          onClick={() => el.operand && onInspectOperand(el.operand)}
          className={`px-2.5 py-1.5 rounded border text-center min-w-[82px] transition-colors cursor-pointer hover:border-[#3B82F6] ${borderColor}`}
        >
          <div className="font-mono text-[10px] font-semibold text-[#F1F5F9]">
            {el.operand}
          </div>
          <div
            className={`font-mono text-xs font-bold leading-none mt-0.5 ${
              energized ? "text-emerald-400" : "text-[#94A3B8]"
            }`}
          >
            {symbol}
          </div>
          {forced && (
            <div className="font-mono text-[9px] font-semibold text-amber-400 mt-0.5">FORCED</div>
          )}
        </button>
      </div>
    );
  }

  if (el.kind === "timer") {
    const rawPreset = el.preset_ms || 0;
    const preset = Math.max(1, rawPreset);
    const elapsed = rawPreset > 0 ? Math.min(preset, el.elapsed_ms || 0) : 0;
    const pct = rawPreset > 0 ? Math.round((elapsed / preset) * 100) : 0;
    return (
      <div key={idx} className="flex items-center">
        <div className={`w-4 h-0.5 ${railColor}`} />
        <div className={`px-2.5 py-1.5 rounded border min-w-[144px] ${borderColor}`}>
          <div className="flex items-center justify-between font-mono text-[10px]">
            <span className="font-semibold text-[#F1F5F9]">
              {el.mode || "SD"} {el.operand}
            </span>
            <span className={el.output ? "text-emerald-400 font-semibold" : "text-[#94A3B8]"}>
              Q={el.output ? "1" : "0"}
            </span>
          </div>
          <div className="mt-1 h-1.5 rounded bg-[#121821] border border-[#243041] overflow-hidden">
            <div
              className={el.output ? "h-full bg-emerald-500" : "h-full bg-[#3B82F6]"}
              style={{ width: `${pct}%` }}
            />
          </div>
          <div className="mt-1 flex justify-between font-mono text-[9px] text-[#94A3B8] tabular-nums">
            <span>ET {elapsed}ms</span>
            <span>{rawPreset > 0 ? `PT ${rawPreset}ms` : "PT idle"}</span>
          </div>
        </div>
      </div>
    );
  }

  if (el.kind === "counter") {
    return (
      <div key={idx} className="flex items-center">
        <div className={`w-4 h-0.5 ${railColor}`} />
        <div className={`px-2.5 py-1.5 rounded border min-w-[104px] ${borderColor}`}>
          <div className="font-mono text-[10px] font-semibold text-[#F1F5F9]">
            {el.mode || "CU"} {el.operand}
          </div>
          <div className="font-mono text-xs font-bold text-[#3B82F6] tabular-nums mt-0.5">
            CV = {String(el.value ?? 0)}
          </div>
        </div>
      </div>
    );
  }

  if (el.kind === "call") {
    return (
      <div key={idx} className="flex items-center">
        <div className={`w-4 h-0.5 ${railColor}`} />
        <div className={`px-3 py-2 rounded border min-w-[180px] ${borderColor}`}>
          <div className="flex items-center justify-between gap-2 font-mono text-[11px] font-semibold text-[#F1F5F9]">
            <span>CALL {el.block}</span>
            {el.instance_db && <span className="text-[#3B82F6]">{el.instance_db}</span>}
          </div>
          {el.params && el.params.length > 0 && (
            <div className="mt-1.5 space-y-0.5 border-t border-[#243041] pt-1">
              {el.params.slice(0, 6).map((p, pIdx) => (
                <div
                  key={pIdx}
                  className="flex items-center justify-between gap-3 font-mono text-[10px]"
                >
                  <span className="text-[#94A3B8]">{p.param}</span>
                  <span className="text-[#F1F5F9]">{p.value}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  if (el.kind === "sfc_step") {
    return (
      <div
        key={idx}
        className={`px-3 py-2 rounded border min-w-[170px] ${
          energized
            ? "border-emerald-500 bg-emerald-500/15"
            : "border-[#243041] bg-[#0B0F14]"
        }`}
      >
        <div className="flex items-center justify-between gap-2 font-mono text-[11px] font-semibold">
          <span className="text-[#F1F5F9]">{el.step_name}</span>
          <span className={energized ? "text-emerald-400" : "text-[#64748B]"}>
            {energized ? "ACTIVE" : el.is_initial ? "INIT" : "IDLE"}
          </span>
        </div>
        {el.actions && el.actions.length > 0 && (
          <div className="mt-1 space-y-0.5 border-t border-[#243041] pt-1 font-mono text-[10px]">
            {el.actions.map((a, aIdx) => (
              <div key={aIdx} className="flex items-center justify-between text-[#CBD5E1]">
                <span className="text-[#3B82F6]">{a.qualifier}</span>
                <span>{a.operand}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  if (el.kind === "sfc_transition") {
    return (
      <div
        key={idx}
        className="px-2.5 py-1.5 rounded border border-[#243041] bg-[#171F2B] font-mono text-[10px] text-[#CBD5E1] flex items-center gap-1.5"
      >
        <span className="text-[#3B82F6] font-semibold">{el.name}</span>
        <ArrowRight className="w-3 h-3 text-[#64748B]" aria-hidden="true" />
        <span className="text-[#F1F5F9]">{el.to_step}</span>
        <span className="text-[#94A3B8]">({el.condition})</span>
      </div>
    );
  }

  return (
    <div
      key={idx}
      className="px-2 py-1 rounded border border-[#243041] bg-[#0B0F14] font-mono text-[11px] text-[#CBD5E1]"
    >
      {el.op || el.kind} {el.operand || ""}
    </div>
  );
}

export function LadderDebuggerView({
  state,
  busy,
  onToggleNetworkBreakpoint,
  onAddConditionBreakpoint,
  onRemoveConditionBreakpoint,
  onInspectOperand,
}: LadderDebuggerViewProps) {
  const blocks = useMemo(() => {
    const names = Array.from(new Set(state.networks.map((n) => n.block)));
    return names.length > 0 ? names : state.project.blocks.map((b) => b.name);
  }, [state.networks, state.project.blocks]);

  const [selectedBlock, setSelectedBlock] = useState<string>("");
  const [showHeatmap, setShowHeatmap] = useState(true);
  const [showSource, setShowSource] = useState(true);
  const [bpAddress, setBpAddress] = useState("Q0.0");
  const [bpOp, setBpOp] = useState("==");
  const [bpValue, setBpValue] = useState("1");

  const activeBlock = blocks.includes(selectedBlock) ? selectedBlock : blocks[0] || "";
  const visibleNetworks: LadderNetwork[] = state.networks.filter(
    (n) => !activeBlock || n.block === activeBlock,
  );
  const activeBlockSource =
    state.project.blocks.find((b) => b.name === activeBlock)?.source || "";

  const handleAddCondition = (e: React.FormEvent) => {
    e.preventDefault();
    if (!bpAddress.trim()) return;
    onAddConditionBreakpoint(bpAddress.trim().toUpperCase(), bpOp, bpValue.trim());
  };

  return (
    <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
      {/* Toolbar */}
      <div className="px-4 py-2 bg-[#121821] border-b border-[#243041] flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8] mr-1">
            Block
          </span>
          {blocks.map((blk) => (
            <button
              key={blk}
              type="button"
              onClick={() => setSelectedBlock(blk)}
              className={`h-7 px-2.5 rounded font-mono text-xs font-medium cursor-pointer transition-colors ${
                activeBlock === blk
                  ? "bg-[#3B82F6] text-white"
                  : "bg-[#171F2B] text-[#CBD5E1] border border-[#243041] hover:text-white"
              }`}
            >
              {blk}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowHeatmap((v) => !v)}
            className={`h-7 px-2.5 rounded border text-xs font-medium flex items-center gap-1.5 cursor-pointer ${
              showHeatmap
                ? "bg-emerald-500/15 border-emerald-500/50 text-emerald-300"
                : "bg-[#171F2B] border-[#243041] text-[#94A3B8]"
            }`}
          >
            <Flame className="w-3.5 h-3.5" aria-hidden="true" />
            <span>Coverage Heatmap</span>
          </button>

          <button
            type="button"
            onClick={() => setShowSource((v) => !v)}
            className={`h-7 px-2.5 rounded border text-xs font-medium flex items-center gap-1.5 cursor-pointer ${
              showSource
                ? "bg-[#3B82F6]/15 border-[#3B82F6]/50 text-[#F1F5F9]"
                : "bg-[#171F2B] border-[#243041] text-[#94A3B8]"
            }`}
          >
            <Code2 className="w-3.5 h-3.5" aria-hidden="true" />
            <span>STL / Source</span>
          </button>
        </div>
      </div>

      {/* Breakpoint Hit Banner */}
      {state.breakpoints.last_hit && (
        <div className="px-4 py-1.5 bg-amber-500/15 border-b border-amber-500/40 text-xs text-amber-200 flex items-center justify-between">
          <span>
            <strong>Breakpoint Hit:</strong> {state.breakpoints.last_hit.reason} (
            <span className="font-mono">{state.breakpoints.last_hit.target}</span>)
          </span>
          <span className="font-mono text-[11px]">CPU PAUSED_BREAKPOINT</span>
        </div>
      )}

      {/* Main Split: LAD/SFC Networks + Right Source & Breakpoints */}
      <div className="flex-1 min-h-0 flex overflow-hidden">
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {visibleNetworks.map((net) => {
            const covered = net.coverage_scans > 0;
            const netBorder = net.has_breakpoint
              ? "border-amber-500/70"
              : showHeatmap && covered
                ? "border-emerald-500/40"
                : "border-[#243041]";

            return (
              <article
                key={net.id}
                className={`rounded bg-[#121821] border ${netBorder} overflow-hidden`}
              >
                <header className="px-3 py-2 bg-[#171F2B] border-b border-[#243041] flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <button
                      type="button"
                      disabled={busy}
                      aria-label={`Toggle breakpoint on ${net.id}`}
                      onClick={() => onToggleNetworkBreakpoint(net.id)}
                      className={`p-1 rounded border cursor-pointer transition-colors ${
                        net.has_breakpoint
                          ? "bg-red-500/20 border-red-500 text-red-400"
                          : "bg-[#0B0F14] border-[#243041] text-[#64748B] hover:text-red-400"
                      }`}
                    >
                      <CircleDot className="w-3.5 h-3.5" aria-hidden="true" />
                    </button>
                    <span className="font-mono text-xs font-semibold text-[#3B82F6] shrink-0">
                      {net.id}
                    </span>
                    <h3 className="text-xs font-medium text-[#F1F5F9] truncate">{net.title}</h3>
                  </div>

                  <div className="flex items-center gap-3 font-mono text-[11px] shrink-0 tabular-nums">
                    <span className={net.energized ? "text-emerald-400 font-semibold" : "text-[#64748B]"}>
                      RLO {net.energized ? "TRUE" : "FALSE"}
                    </span>
                    {showHeatmap && (
                      <span className={covered ? "text-emerald-300" : "text-[#64748B]"}>
                        {covered ? `Executed ${net.coverage_scans}x` : "Uncovered"}
                      </span>
                    )}
                  </div>
                </header>

                {/* Power-Flow Diagram */}
                <div className="p-3.5 overflow-x-auto">
                  <div className="flex items-center gap-1 min-w-max">
                    {/* Left Power Rail */}
                    <div
                      className={`w-1.5 self-stretch min-h-[44px] rounded-sm ${
                        net.energized ? "bg-emerald-500" : "bg-[#3B82F6]"
                      }`}
                    />
                    <div className="flex flex-wrap items-center gap-y-2">
                      {net.elements.map((el, i) => renderElementNode(el, i, onInspectOperand))}
                    </div>
                  </div>
                </div>
              </article>
            );
          })}

          {visibleNetworks.length === 0 && (
            <div className="p-8 text-center text-xs text-[#94A3B8] bg-[#121821] border border-[#243041] rounded">
              No parsed networks in {activeBlock}. Inspect the STL/SCL source pane on the right.
            </div>
          )}
        </div>

        {/* Right Panel: Conditional Breakpoints + Synchronized Block Source */}
        {showSource && (
          <aside className="w-[340px] shrink-0 border-l border-[#243041] bg-[#121821] flex flex-col overflow-hidden">
            {/* Conditional Breakpoints */}
            <div className="p-3 border-b border-[#243041] space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">
                  Conditional Breakpoints
                </h3>
                <span className="font-mono text-[11px] text-[#64748B]">
                  {state.breakpoints.conditions.length} active
                </span>
              </div>

              <form onSubmit={handleAddCondition} className="flex items-center gap-1">
                <input
                  type="text"
                  aria-label="Breakpoint address"
                  value={bpAddress}
                  onChange={(e) => setBpAddress(e.target.value)}
                  placeholder="Q0.0"
                  className="h-7 w-20 px-2 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9]"
                />
                <select
                  aria-label="Breakpoint operator"
                  value={bpOp}
                  onChange={(e) => setBpOp(e.target.value)}
                  className="h-7 px-1 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9]"
                >
                  <option value="==">==</option>
                  <option value="!=">!=</option>
                  <option value=">">&gt;</option>
                  <option value=">=">&gt;=</option>
                  <option value="<">&lt;</option>
                  <option value="<=">&lt;=</option>
                </select>
                <input
                  type="text"
                  aria-label="Breakpoint value"
                  value={bpValue}
                  onChange={(e) => setBpValue(e.target.value)}
                  placeholder="1"
                  className="h-7 flex-1 min-w-0 px-2 rounded bg-[#0B0F14] border border-[#243041] font-mono text-xs text-[#F1F5F9]"
                />
                <button
                  type="submit"
                  disabled={busy}
                  aria-label="Add conditional breakpoint"
                  className="h-7 px-2 rounded bg-[#3B82F6] hover:bg-[#2563EB] text-white cursor-pointer"
                >
                  <Plus className="w-3.5 h-3.5" aria-hidden="true" />
                </button>
              </form>

              {state.breakpoints.conditions.length > 0 && (
                <ul className="space-y-1">
                  {state.breakpoints.conditions.map((cond) => (
                    <li
                      key={cond.id}
                      className="px-2 py-1 rounded bg-[#0B0F14] border border-[#243041] flex items-center justify-between font-mono text-[11px]"
                    >
                      <span className="text-[#F1F5F9]">
                        {cond.address} {cond.operator} {String(cond.value)}
                      </span>
                      <button
                        type="button"
                        aria-label={`Remove breakpoint ${cond.address}`}
                        onClick={() => onRemoveConditionBreakpoint(cond.id)}
                        className="text-[#94A3B8] hover:text-red-400 cursor-pointer"
                      >
                        <Trash2 className="w-3 h-3" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Active Timers & Counters Snapshot */}
            <div className="p-3 border-b border-[#243041] space-y-1.5">
              <div className="flex items-center gap-1.5">
                <Eye className="w-3.5 h-3.5 text-[#3B82F6]" aria-hidden="true" />
                <h3 className="text-[11px] font-semibold uppercase tracking-wider text-[#94A3B8]">
                  Live Block Source ({activeBlock})
                </h3>
              </div>
            </div>

            <pre className="flex-1 overflow-auto p-3 bg-[#0B0F14] font-mono text-[11px] leading-relaxed text-[#CBD5E1] whitespace-pre-wrap break-words">
              {activeBlockSource || "// No source text available for this block."}
            </pre>
          </aside>
        )}
      </div>
    </div>
  );
}
