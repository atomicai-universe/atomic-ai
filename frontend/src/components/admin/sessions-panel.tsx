"use client";

/**
 * Active agent sessions (Req 13.1): every running session across all workspaces
 * with an expandable reasoning trace (execution_logs) and an emergency Kill
 * button (Req 13.2 kill-switch).
 */

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError } from "@/lib/api";
import { killSession, type AdminSession } from "@/lib/admin";
import { formatDateTime } from "./format";

interface Props {
  sessions: AdminSession[];
  onChanged: () => void;
}

function TracePreview({ logs }: { logs: unknown }) {
  if (logs == null) {
    return <p className="text-sm text-muted-foreground">No reasoning trace recorded.</p>;
  }
  const text =
    typeof logs === "string" ? logs : JSON.stringify(logs, null, 2);
  return (
    <pre className="max-h-80 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed">
      {text}
    </pre>
  );
}

function SessionCard({ session, onChanged }: { session: AdminSession; onChanged: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleKill() {
    if (
      !window.confirm(
        "Emergency kill this agent session? It will be terminated and its pending approvals cancelled.",
      )
    )
      return;
    setError(null);
    setPending(true);
    try {
      await killSession(session.id);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to kill session.");
    } finally {
      setPending(false);
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 pb-3">
        <div className="min-w-0 space-y-1">
          <CardTitle className="flex items-center gap-2 text-sm">
            <span className="truncate font-mono">{session.id}</span>
            <Badge variant="warning">{session.status}</Badge>
          </CardTitle>
          <p className="text-xs text-muted-foreground">
            Workspace {session.workspace_id} · triggered by{" "}
            {session.triggered_by_user_id} · {formatDateTime(session.created_at)}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" onClick={() => setExpanded((v) => !v)}>
            {expanded ? "Hide trace" : "Show trace"}
          </Button>
          <Button
            variant="destructive"
            size="sm"
            disabled={pending}
            onClick={handleKill}
          >
            {pending ? "Killing…" : "Kill"}
          </Button>
        </div>
      </CardHeader>
      {(expanded || error) && (
        <CardContent className="space-y-2">
          {error && (
            <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </p>
          )}
          {expanded && <TracePreview logs={session.execution_logs} />}
        </CardContent>
      )}
    </Card>
  );
}

export function SessionsPanel({ sessions, onChanged }: Props) {
  if (sessions.length === 0) {
    return (
      <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
        No running agent sessions.
      </p>
    );
  }
  return (
    <section className="space-y-3">
      {sessions.map((session) => (
        <SessionCard key={session.id} session={session} onChanged={onChanged} />
      ))}
    </section>
  );
}
