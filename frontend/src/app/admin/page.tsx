"use client";

/**
 * Super Admin Portal (task 18.1).
 *
 * Surfaces the backend super admin control plane (`/api/v1/admin/*`):
 * - System analytics (Req 14.2)
 * - User & workspace directory with ban / reassign-owner (Req 12.3, 12.4, 12.5)
 * - Active agent sessions with expandable reasoning traces + kill (Req 13.1, 13.2)
 * - MCP server health indicators (Req 14.1)
 *
 * The entire control plane requires a super admin session; the backend enforces
 * this and returns HTTP 403 for any other caller. The UI simply surfaces that:
 * a 403 from any call renders an "Admin access required" message. All other
 * failures surface `ApiError.message`.
 */

import { useCallback, useEffect, useState } from "react";

import { AnalyticsPanel } from "@/components/admin/analytics-panel";
import { McpHealthPanel } from "@/components/admin/mcp-health-panel";
import { SessionsPanel } from "@/components/admin/sessions-panel";
import { UsersPanel } from "@/components/admin/users-panel";
import { WorkspacesPanel } from "@/components/admin/workspaces-panel";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api";
import {
  getAnalytics,
  getMcpHealth,
  listSessions,
  listUsers,
  listWorkspaces,
  type AdminSession,
  type AdminUser,
  type AdminWorkspace,
  type McpHealth,
  type SystemAnalytics,
} from "@/lib/admin";

type LoadState = "loading" | "forbidden" | "error" | "ready";

interface AdminData {
  analytics: SystemAnalytics;
  users: AdminUser[];
  workspaces: AdminWorkspace[];
  sessions: AdminSession[];
  mcpHealth: McpHealth;
}

function SectionHeading({ title, description }: { title: string; description: string }) {
  return (
    <div className="space-y-1">
      <h2 className="text-lg font-semibold">{title}</h2>
      <p className="text-sm text-muted-foreground">{description}</p>
    </div>
  );
}

export default function AdminPortalPage() {
  const [state, setState] = useState<LoadState>("loading");
  const [data, setData] = useState<AdminData | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState("loading");
    setErrorMessage(null);
    try {
      const [analytics, users, workspaces, sessions, mcpHealth] =
        await Promise.all([
          getAnalytics(),
          listUsers(),
          listWorkspaces(),
          listSessions(),
          getMcpHealth(),
        ]);
      setData({
        analytics,
        users: users.users,
        workspaces: workspaces.workspaces,
        sessions: sessions.sessions,
        mcpHealth,
      });
      setState("ready");
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setState("forbidden");
      } else {
        setErrorMessage(
          err instanceof ApiError ? err.message : "Failed to load admin data.",
        );
        setState("error");
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (state === "loading") {
    return (
      <main className="p-8">
        <p className="text-sm text-muted-foreground">Loading super admin portal…</p>
      </main>
    );
  }

  if (state === "forbidden") {
    return (
      <main className="flex min-h-[60vh] flex-col items-center justify-center gap-3 p-8 text-center">
        <h1 className="text-xl font-semibold">Admin access required</h1>
        <p className="max-w-md text-sm text-muted-foreground">
          This is the super admin control plane. Your account does not have super
          admin privileges.
        </p>
      </main>
    );
  }

  if (state === "error") {
    return (
      <main className="flex min-h-[60vh] flex-col items-center justify-center gap-3 p-8 text-center">
        <h1 className="text-xl font-semibold">Something went wrong</h1>
        <p className="max-w-md text-sm text-destructive">{errorMessage}</p>
        <Button onClick={() => void load()}>Retry</Button>
      </main>
    );
  }

  if (!data) return null;

  return (
    <main className="mx-auto max-w-6xl space-y-10 p-6 sm:p-8">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Super Admin Portal</h1>
          <p className="text-sm text-muted-foreground">
            Platform-wide analytics, users, agent sessions, and MCP health.
          </p>
        </div>
        <Button variant="outline" onClick={() => void load()}>
          Refresh
        </Button>
      </header>

      <div className="space-y-4">
        <SectionHeading
          title="System analytics"
          description="Aggregate token usage, active sessions, and storage metrics."
        />
        <AnalyticsPanel data={data.analytics} />
      </div>

      <div className="space-y-4">
        <SectionHeading
          title="Users"
          description="Directory across all workspaces. Toggle roles, edit, ban, or delete users."
        />
        <UsersPanel users={data.users} onChanged={() => void load()} />
      </div>

      <div className="space-y-4">
        <SectionHeading
          title="Workspaces"
          description="All workspaces across the platform. Reassign ownership or delete a workspace."
        />
        <WorkspacesPanel
          workspaces={data.workspaces}
          users={data.users}
          onChanged={() => void load()}
        />
      </div>

      <div className="space-y-4">
        <SectionHeading
          title="Active agent sessions"
          description="Running sessions across all workspaces with reasoning traces and an emergency kill-switch."
        />
        <SessionsPanel sessions={data.sessions} onChanged={() => void load()} />
      </div>

      <div className="space-y-4">
        <SectionHeading
          title="MCP server health"
          description="Per-provider status counts and error-rate health indicators."
        />
        <McpHealthPanel data={data.mcpHealth} />
      </div>
    </main>
  );
}
