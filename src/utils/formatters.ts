/** Pure, deterministic formatting and engineering calculation helpers for SIMATIC Verify Studio. */

export function formatSimTime(ms: number): string {
  const safe = Math.max(0, Math.floor(ms));
  const totalSec = Math.floor(safe / 1000);
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  const remMs = safe % 1000;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${String(remMs).padStart(3, "0")}`;
}

export function shortSha256(hash: string): string {
  if (!hash) return "—";
  const parts = hash.split(":");
  const digest = parts[parts.length - 1] || hash;
  return digest.slice(0, 12);
}

export function classifyWatchdogHeadroom(headroomPct: number): {
  status: "NOMINAL" | "WARNING" | "CRITICAL";
  textClass: string;
  barClass: string;
} {
  if (headroomPct >= 50) {
    return { status: "NOMINAL", textClass: "text-emerald-400", barClass: "bg-emerald-500" };
  }
  if (headroomPct >= 20) {
    return { status: "WARNING", textClass: "text-amber-400", barClass: "bg-amber-500" };
  }
  return { status: "CRITICAL", textClass: "text-red-400", barClass: "bg-red-500" };
}

export function computeAnalogScaleBounds(values: number[]): { min: number; max: number } {
  if (values.length === 0) return { min: 0, max: 100 };
  let mn = Math.min(...values);
  let mx = Math.max(...values);
  if (Math.abs(mx - mn) < 1e-6) {
    mn = Math.min(0, mn - 10);
    mx = Math.max(100, mx + 10);
  }
  const pad = (mx - mn) * 0.1;
  return { min: Math.floor(mn - pad), max: Math.ceil(mx + pad) };
}

export function filterVatRows<T extends { address: string; symbol: string; comment: string }>(
  rows: T[],
  query: string,
): T[] {
  const q = query.trim().toLowerCase();
  if (!q) return rows;
  return rows.filter(
    (r) =>
      r.address.toLowerCase().includes(q) ||
      r.symbol.toLowerCase().includes(q) ||
      r.comment.toLowerCase().includes(q),
  );
}
