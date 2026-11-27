import { useState } from "react";
import { ChevronDown, ChevronUp, Plus, Search, ShieldOff, Lock } from "lucide-react";
import type { SimState, VatRow } from "../types/sim";
import { filterVatRows } from "../utils/formatters";

interface VatForceDockProps {
  state: SimState;
  busy: boolean;
  onWriteTag: (address: string, value: boolean | number | string, dataType?: string) => void;
  onForceSet: (address: string, value: boolean | number | string, dataType?: string) => void;
  onForceRemove: (address: string) => void;
  onForceClearAll: () => void;
  onSetFormat: (table: string, address: string, format: string) => void;
  onAddTag: (table: string, address: string, symbol: string, dataType: string, comment: string) => void;
}

const FORMAT_OPTIONS = ["BOOL", "DEC", "HEX", "BIN", "REAL", "TIME"];

export function VatForceDock({
  state,
  busy,
  onWriteTag,
  onForceSet,
  onForceRemove,
  onForceClearAll,
  onSetFormat,
  onAddTag,
}: VatForceDockProps) {
  const tableNames = Object.keys(state.vat_tables);
  const [expanded, setExpanded] = useState(true);
  const [selectedTable, setSelectedTable] = useState<string>("");
  const [query, setQuery] = useState("");
  const [draftValues, setDraftValues] = useState<Record<string, string>>({});
  const [showAddRow, setShowAddRow] = useState(false);
  const [newAddress, setNewAddress] = useState("");
  const [newSymbol, setNewSymbol] = useState("");
  const [newType, setNewType] = useState("BOOL");

  const activeTable = tableNames.includes(selectedTable) ? selectedTable : tableNames[0] || "";
  const rawRows: VatRow[] = activeTable ? state.vat_tables[activeTable] || [] : [];
  const rows = filterVatRows(rawRows, query);

  const parseDraft = (row: VatRow, rawText: string): boolean | number | string => {
    const trimmed = rawText.trim();
    if (row.data_type === "BOOL") {
      return trimmed === "1" || trimmed.toLowerCase() === "true";
    }
    if (row.data_type === "REAL") {
      const n = Number(trimmed);
      return Number.isNaN(n) ? 0 : n;
    }
    const n = parseInt(trimmed, trimmed.toLowerCase().startsWith("0x") ? 16 : 10);
    return Number.isNaN(n) ? 0 : n;
  };

  const handleAddSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const addr = newAddress.trim().toUpperCase();
    if (!addr || !activeTable) return;
    onAddTag(activeTable, addr, newSymbol.trim() || addr, newType, "Added in VAT");
    setNewAddress("");
    setNewSymbol("");
    setShowAddRow(false);
  };

  return (
    <section
      aria-label="SIMATIC Variable Table and Force Values"
      className="shrink-0 bg-[#121821] border-t border-[#243041] flex flex-col"
    >
      {/* Dock Header */}
      <div className="h-9 px-3 bg-[#171F2B] border-b border-[#243041] flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="flex items-center gap-1.5 text-xs font-semibold text-[#F1F5F9] cursor-pointer"
          >
            {expanded ? (
              <ChevronDown className="w-3.5 h-3.5 text-[#94A3B8]" aria-hidden="true" />
            ) : (
              <ChevronUp className="w-3.5 h-3.5 text-[#94A3B8]" aria-hidden="true" />
            )}
            <span>SIMATIC VAT &amp; Force Table</span>
          </button>

          <div className="flex items-center gap-1 ml-2 overflow-x-auto">
            {tableNames.map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => {
                  setSelectedTable(name);
                  setExpanded(true);
                }}
                className={`px-2 py-0.5 rounded text-[11px] font-medium whitespace-nowrap cursor-pointer ${
                  activeTable === name
                    ? "bg-[#3B82F6]/20 text-[#F1F5F9] border border-[#3B82F6]/50"
                    : "text-[#94A3B8] hover:text-[#F1F5F9] border border-transparent"
                }`}
              >
                {name}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative">
            <Search
              className="w-3 h-3 text-[#64748B] absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none"
              aria-hidden="true"
            />
            <input
              type="text"
              aria-label="Filter VAT tags"
              placeholder="Filter address or symbol..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="h-6 pl-6 pr-2 w-44 rounded bg-[#0B0F14] border border-[#243041] text-[11px] text-[#F1F5F9] placeholder-[#64748B] focus:outline-none focus:border-[#3B82F6]"
            />
          </div>

          <button
            type="button"
            onClick={() => {
              setExpanded(true);
              setShowAddRow((v) => !v);
            }}
            className="h-6 px-2 rounded bg-[#0B0F14] hover:bg-[#1E293B] border border-[#243041] text-[11px] text-[#CBD5E1] flex items-center gap-1 cursor-pointer"
          >
            <Plus className="w-3 h-3" aria-hidden="true" />
            <span>Add Tag</span>
          </button>

          {state.forces.length > 0 && (
            <button
              type="button"
              disabled={busy}
              onClick={onForceClearAll}
              className="h-6 px-2 rounded bg-amber-500/15 hover:bg-amber-500/25 border border-amber-500/50 text-[11px] font-semibold text-amber-300 flex items-center gap-1 cursor-pointer"
            >
              <ShieldOff className="w-3 h-3" aria-hidden="true" />
              <span>Unforce All ({state.forces.length})</span>
            </button>
          )}
        </div>
      </div>

      {expanded && (
        <div className="h-[164px] overflow-y-auto">
          {showAddRow && (
            <form
              onSubmit={handleAddSubmit}
              className="px-3 py-1.5 bg-[#0B0F14] border-b border-[#243041] flex items-center gap-2 text-xs"
            >
              <input
                type="text"
                aria-label="New tag address"
                placeholder="Address (e.g. M10.0, MW20)"
                value={newAddress}
                onChange={(e) => setNewAddress(e.target.value)}
                required
                className="h-6 px-2 w-40 rounded bg-[#121821] border border-[#243041] font-mono text-[11px] text-[#F1F5F9]"
              />
              <input
                type="text"
                aria-label="New tag symbol"
                placeholder="Symbol name"
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                className="h-6 px-2 w-40 rounded bg-[#121821] border border-[#243041] text-[11px] text-[#F1F5F9]"
              />
              <select
                aria-label="New tag data type"
                value={newType}
                onChange={(e) => setNewType(e.target.value)}
                className="h-6 px-1.5 rounded bg-[#121821] border border-[#243041] font-mono text-[11px] text-[#F1F5F9]"
              >
                <option value="BOOL">BOOL</option>
                <option value="INT">INT</option>
                <option value="WORD">WORD</option>
                <option value="REAL">REAL</option>
              </select>
              <button
                type="submit"
                className="h-6 px-2.5 rounded bg-[#3B82F6] text-white text-[11px] font-medium cursor-pointer"
              >
                Add
              </button>
              <button
                type="button"
                onClick={() => setShowAddRow(false)}
                className="h-6 px-2 rounded border border-[#243041] text-[#94A3B8] text-[11px] cursor-pointer"
              >
                Cancel
              </button>
            </form>
          )}

          <table className="w-full text-left border-collapse text-xs">
            <thead className="sticky top-0 bg-[#121821] border-b border-[#243041] text-[11px] text-[#94A3B8]">
              <tr>
                <th className="py-1.5 px-3 font-medium">Address</th>
                <th className="py-1.5 px-2 font-medium">Symbol</th>
                <th className="py-1.5 px-2 font-medium">Format</th>
                <th className="py-1.5 px-2 font-medium">Status Value</th>
                <th className="py-1.5 px-2 font-medium">Modify Value</th>
                <th className="py-1.5 px-3 font-medium text-right">Force Override</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1E293B]">
              {rows.map((row) => {
                const isBool = row.data_type === "BOOL";
                const defaultDraft =
                  row.data_type === "REAL" && typeof row.value === "number"
                    ? row.value.toFixed(2)
                    : String(row.value);
                const draft = draftValues[row.address] ?? defaultDraft;
                return (
                  <tr
                    key={`${activeTable}-${row.address}`}
                    className={row.forced ? "bg-amber-500/10" : "hover:bg-[#171F2B]/50"}
                  >
                    <td className="py-1 px-3 font-mono font-semibold text-[#F1F5F9] whitespace-nowrap">
                      {row.address}
                    </td>
                    <td className="py-1 px-2 text-[#CBD5E1] whitespace-nowrap">
                      <span>{row.symbol}</span>
                      {row.forced && (
                        <span className="ml-2 inline-flex items-center gap-0.5 font-mono text-[10px] font-semibold text-amber-400">
                          <Lock className="w-2.5 h-2.5" aria-hidden="true" />
                          FORCED
                        </span>
                      )}
                    </td>
                    <td className="py-1 px-2">
                      <select
                        aria-label={`Format for ${row.address}`}
                        value={row.format}
                        onChange={(e) => onSetFormat(activeTable, row.address, e.target.value)}
                        className="h-6 px-1.5 rounded bg-[#0B0F14] border border-[#243041] font-mono text-[11px] text-[#CBD5E1]"
                      >
                        {FORMAT_OPTIONS.map((fmt) => (
                          <option key={fmt} value={fmt}>
                            {fmt}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="py-1 px-2 font-mono tabular-nums">
                      <span
                        className={
                          row.forced
                            ? "text-amber-300 font-semibold"
                            : isBool && row.value === true
                              ? "text-emerald-400 font-semibold"
                              : "text-[#F1F5F9]"
                        }
                      >
                        {row.formatted}
                      </span>
                    </td>
                    <td className="py-1 px-2">
                      {isBool ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => onWriteTag(row.address, !row.value, "BOOL")}
                          className="h-6 px-2 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] font-mono text-[11px] text-[#F1F5F9] cursor-pointer disabled:opacity-50"
                        >
                          Toggle {row.value ? "0" : "1"}
                        </button>
                      ) : (
                        <div className="flex items-center gap-1">
                          <input
                            type="text"
                            aria-label={`Modify value for ${row.address}`}
                            value={draft}
                            onChange={(e) =>
                              setDraftValues((prev) => ({ ...prev, [row.address]: e.target.value }))
                            }
                            className="h-6 w-20 px-1.5 rounded bg-[#0B0F14] border border-[#243041] font-mono text-[11px] text-[#F1F5F9] tabular-nums"
                          />
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              onWriteTag(row.address, parseDraft(row, draft), row.data_type)
                            }
                            className="h-6 px-2 rounded bg-[#171F2B] hover:bg-[#1E293B] border border-[#243041] text-[11px] text-[#F1F5F9] cursor-pointer disabled:opacity-50"
                          >
                            Apply
                          </button>
                        </div>
                      )}
                    </td>
                    <td className="py-1 px-3 text-right whitespace-nowrap">
                      <div className="inline-flex items-center gap-1">
                        {isBool ? (
                          <>
                            <button
                              type="button"
                              disabled={busy}
                              aria-label={`Force ${row.address} to 1`}
                              onClick={() => onForceSet(row.address, true, "BOOL")}
                              className="h-6 px-2 rounded bg-[#171F2B] hover:bg-amber-500/20 border border-[#243041] hover:border-amber-500/50 font-mono text-[11px] text-[#CBD5E1] hover:text-amber-300 cursor-pointer"
                            >
                              Force 1
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              aria-label={`Force ${row.address} to 0`}
                              onClick={() => onForceSet(row.address, false, "BOOL")}
                              className="h-6 px-2 rounded bg-[#171F2B] hover:bg-amber-500/20 border border-[#243041] hover:border-amber-500/50 font-mono text-[11px] text-[#CBD5E1] hover:text-amber-300 cursor-pointer"
                            >
                              Force 0
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            disabled={busy}
                            aria-label={`Force ${row.address} value`}
                            onClick={() =>
                              onForceSet(row.address, parseDraft(row, draft), row.data_type)
                            }
                            className="h-6 px-2 rounded bg-[#171F2B] hover:bg-amber-500/20 border border-[#243041] hover:border-amber-500/50 font-mono text-[11px] text-[#CBD5E1] hover:text-amber-300 cursor-pointer"
                          >
                            Force Val
                          </button>
                        )}
                        {row.forced && (
                          <button
                            type="button"
                            disabled={busy}
                            aria-label={`Remove force on ${row.address}`}
                            onClick={() => onForceRemove(row.address)}
                            className="h-6 px-2 rounded bg-amber-500/15 hover:bg-amber-500/25 border border-amber-500/50 font-mono text-[11px] text-amber-300 cursor-pointer"
                          >
                            Remove
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-6 text-center text-xs text-[#64748B]">
                    No tags match the current filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
