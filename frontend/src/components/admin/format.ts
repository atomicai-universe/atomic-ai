/** Small formatting helpers shared across the super admin portal. */

/** Format a byte count into a human-readable string (KB/MB/GB). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exp = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  );
  const value = bytes / 1024 ** exp;
  return `${value.toFixed(exp === 0 ? 0 : 1)} ${units[exp]}`;
}

/** Format a large integer with thousands separators. */
export function formatNumber(n: number): string {
  return new Intl.NumberFormat().format(n ?? 0);
}

/** Format an ISO timestamp for display; falls back to the raw string. */
export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}
