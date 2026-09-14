"use client";

/**
 * Workspace switcher (Req 3.7).
 *
 * A header control that lets the user:
 *  - CREATE a workspace (`POST /api/v1/workspaces`), becoming its Owner, and
 *  - SWITCH the active workspace, which scopes subsequent workspace-level
 *    operations by persisting the selected id (sent as `X-Workspace-Id` by the
 *    API client — see `lib/api.ts`).
 *
 * Because the backend has no list-workspaces endpoint yet, the set of options
 * comes from a local registry (`lib/workspaces.ts`) of workspaces this browser
 * has created. Switching to one simply stores its id as active; creating one
 * remembers it and makes it active. Both survive reloads via `localStorage`.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiError, api, getActiveWorkspaceId, setActiveWorkspaceId } from "@/lib/api";
import {
  type KnownWorkspace,
  fetchWorkspaces,
  listKnownWorkspaces,
  rememberWorkspace,
} from "@/lib/workspaces";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { cn } from "@/lib/utils";

interface CreatedWorkspace {
  id: string;
  name: string;
  slug: string;
}

/** Emitted so sibling views (e.g. the team page) can react to a switch. */
export const ACTIVE_WORKSPACE_EVENT = "atomic:active-workspace-changed";

function broadcastActiveChange() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(ACTIVE_WORKSPACE_EVENT));
  }
}

export function WorkspaceSwitcher({ className }: { className?: string }) {
  const [workspaces, setWorkspaces] = useState<KnownWorkspace[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Hydrate from local storage first (avoids SSR/client mismatch), then refresh
  // from the authoritative backend list. Re-sync when a rename/delete/switch is
  // broadcast so the header stays consistent.
  useEffect(() => {
    setWorkspaces(listKnownWorkspaces());
    setActiveId(getActiveWorkspaceId());

    let cancelled = false;
    void fetchWorkspaces().then((list) => {
      if (!cancelled) setWorkspaces(list);
    });

    const sync = () => {
      setActiveId(getActiveWorkspaceId());
      setWorkspaces(listKnownWorkspaces());
    };
    window.addEventListener(ACTIVE_WORKSPACE_EVENT, sync);
    return () => {
      cancelled = true;
      window.removeEventListener(ACTIVE_WORKSPACE_EVENT, sync);
    };
  }, []);

  const handleSwitch = useCallback((id: string) => {
    setActiveWorkspaceId(id || null);
    setActiveId(id || null);
    broadcastActiveChange();
  }, []);

  const handleCreate = useCallback(async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Enter a workspace name.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const ws = await api.post<CreatedWorkspace>("/api/v1/workspaces", {
        name: trimmed,
      });
      const next = rememberWorkspace(ws);
      setWorkspaces(next);
      setActiveWorkspaceId(ws.id);
      setActiveId(ws.id);
      broadcastActiveChange();
      setName("");
      setCreating(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create workspace.");
    } finally {
      setSubmitting(false);
    }
  }, [name]);

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <label htmlFor="workspace-switcher" className="sr-only">
        Active workspace
      </label>
      <Select
        id="workspace-switcher"
        className="w-56"
        value={activeId ?? ""}
        onChange={(e) => handleSwitch(e.target.value)}
        disabled={workspaces.length === 0}
        aria-label="Active workspace"
      >
        {workspaces.length === 0 ? (
          <option value="">No workspaces yet</option>
        ) : (
          <>
            {activeId === null && <option value="">Select a workspace</option>}
            {workspaces.map((ws) => (
              <option key={ws.id} value={ws.id}>
                {ws.name}
              </option>
            ))}
          </>
        )}
      </Select>

      {creating ? (
        <div className="flex items-center gap-2">
          <Input
            className="w-48"
            placeholder="Workspace name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleCreate();
              if (e.key === "Escape") {
                setCreating(false);
                setError(null);
              }
            }}
            autoFocus
            aria-label="New workspace name"
          />
          <Button size="sm" onClick={() => void handleCreate()} disabled={submitting}>
            {submitting ? "Creating…" : "Create"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setCreating(false);
              setError(null);
            }}
            disabled={submitting}
          >
            Cancel
          </Button>
        </div>
      ) : (
        <Button size="sm" variant="outline" onClick={() => setCreating(true)}>
          New workspace
        </Button>
      )}

      {error && (
        <span role="alert" className="text-sm text-destructive">
          {error}
        </span>
      )}
    </div>
  );
}
