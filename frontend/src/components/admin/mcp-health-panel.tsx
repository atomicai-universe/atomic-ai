"use client";

/**
 * MCP server health indicators (Req 14.1): a grid of providers showing
 * total/active/error counts and an error-rate health badge.
 */

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { McpHealth, McpProviderHealth } from "@/lib/admin";
import { formatNumber } from "./format";

/** Map an error rate (0..1) to a health badge. */
function HealthBadge({ rate }: { rate: number }) {
  const pct = `${(rate * 100).toFixed(1)}%`;
  if (rate <= 0) return <Badge variant="success">Healthy · {pct}</Badge>;
  if (rate < 0.25) return <Badge variant="warning">Degraded · {pct}</Badge>;
  return <Badge variant="destructive">Unhealthy · {pct}</Badge>;
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="tabular-nums">{formatNumber(value)}</span>
    </div>
  );
}

function ProviderCard({ provider }: { provider: McpProviderHealth }) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 pb-2">
        <CardTitle className="text-sm capitalize">{provider.name}</CardTitle>
        <HealthBadge rate={provider.error_rate} />
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-4 gap-2 text-sm">
          <Metric label="Total" value={provider.total} />
          <Metric label="Active" value={provider.active} />
          <Metric label="Error" value={provider.error} />
          <Metric label="Down" value={provider.disconnected} />
        </div>
        <div className="mt-3 text-xs text-muted-foreground">
          {formatNumber(provider.tokens_used)} tokens used
        </div>
      </CardContent>
    </Card>
  );
}

export function McpHealthPanel({ data }: { data: McpHealth }) {
  if (data.providers.length === 0) {
    return (
      <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
        No MCP providers registered.
      </p>
    );
  }
  return (
    <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {data.providers.map((provider) => (
        <ProviderCard key={provider.name} provider={provider} />
      ))}
    </section>
  );
}
