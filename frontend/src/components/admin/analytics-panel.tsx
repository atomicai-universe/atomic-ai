"use client";

/**
 * System analytics view (Req 14.2): token usage total, active session count,
 * and storage/DB metrics rendered as summary cards.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { SystemAnalytics } from "@/lib/admin";
import { formatBytes, formatNumber } from "./format";

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">
          {label}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-2xl font-semibold tabular-nums">{value}</p>
      </CardContent>
    </Card>
  );
}

export function AnalyticsPanel({ data }: { data: SystemAnalytics }) {
  const tableEntries = Object.entries(data.table_counts ?? {});
  return (
    <section className="space-y-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard label="Total tokens used" value={formatNumber(data.tokens_total)} />
        <StatCard label="Active sessions" value={formatNumber(data.active_sessions)} />
        <StatCard label="Database storage" value={formatBytes(data.storage_bytes)} />
      </div>
      {tableEntries.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Table row counts
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3 lg:grid-cols-4">
              {tableEntries.map(([table, count]) => (
                <div key={table} className="flex justify-between gap-2">
                  <span className="truncate text-muted-foreground">{table}</span>
                  <span className="tabular-nums">{formatNumber(count)}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </section>
  );
}
