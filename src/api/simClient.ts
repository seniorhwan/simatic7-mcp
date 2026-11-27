import type { SimState } from "../types/sim";

async function apiRequest<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json();
  if (!res.ok || data?.ok === false) {
    throw new Error(data?.error || `Request failed (${res.status})`);
  }
  return data as T;
}

export const simApi = {
  getState: () => apiRequest<SimState>("/api/sim/state"),

  loadPreset: (preset_id: string) =>
    apiRequest<{ ok: boolean; state: SimState }>("/api/sim/load", {
      method: "POST",
      body: JSON.stringify({ preset_id }),
    }),

  loadCustom: (payload: { name: string; cpu_family: string; source_code: string; symbols: string }) =>
    apiRequest<{ ok: boolean; state: SimState }>("/api/sim/load", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  control: (action: string, extra?: Record<string, unknown>) =>
    apiRequest<{ ok: boolean; state: SimState }>("/api/sim/control", {
      method: "POST",
      body: JSON.stringify({ action, ...extra }),
    }),

  step: (cycles = 1, force_step = false, tick_ms?: number) =>
    apiRequest<{ ok: boolean; executed_cycles: number; state: SimState }>("/api/sim/step", {
      method: "POST",
      body: JSON.stringify({ cycles, force_step, ...(tick_ms !== undefined ? { tick_ms } : {}) }),
    }),

  writeTag: (address: string, value: boolean | number | string, data_type?: string) =>
    apiRequest<{ ok: boolean; state: SimState }>("/api/sim/write", {
      method: "POST",
      body: JSON.stringify({ address, value, data_type }),
    }),

  force: (payload: { action: "set" | "remove" | "clear_all"; address?: string; value?: boolean | number | string; data_type?: string }) =>
    apiRequest<{ ok: boolean; state: SimState }>("/api/sim/force", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  fault: (payload: { fault_id?: string; active?: boolean; action?: "brownout_warm_restart" | "clear_all" }) =>
    apiRequest<{ ok: boolean; state: SimState }>("/api/sim/fault", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  breakpoints: (payload: Record<string, unknown>) =>
    apiRequest<{ ok: boolean; state: SimState }>("/api/sim/breakpoints", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  trace: (payload: Record<string, unknown>) =>
    apiRequest<{ ok: boolean; state?: SimState; csv?: string; row_count?: number }>("/api/sim/trace", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  vat: (payload: Record<string, unknown>) =>
    apiRequest<{ ok: boolean; state: SimState }>("/api/sim/vat", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  runFat: (test_id?: string) =>
    apiRequest<{ ok: boolean; state: SimState }>("/api/sim/fat/run", {
      method: "POST",
      body: JSON.stringify(test_id ? { test_id } : {}),
    }),

  exportFatReport: (format: "markdown" | "json") =>
    apiRequest<{ ok: boolean; format: string; content: string }>(`/api/sim/fat/report?format=${format}`),

  bridgePlcsim: (action: "start" | "stop", run_mode = "RUN_P", scan_mode?: string) =>
    fetch("/api/sim/bridge/plcsim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, run_mode, scan_mode }),
    }).then((r) => r.json()),

  bridgeDiagnosePlc: (host: string, port: number, rack: number, slot: number) =>
    fetch("/api/sim/bridge/diagnose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, port, rack, slot }),
    }).then((r) => r.json()),
};
