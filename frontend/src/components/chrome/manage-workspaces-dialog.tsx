"use client";

/**
 * Manage Workspaces dialog (BUILD.md).
 *
 * Opened from the profile dropdown. Lets an Owner:
 *   - Rename a workspace  -> PATCH  /api/v1/workspaces/{id}
 *   - Delete a workspace  -> DELETE /api/v1/workspaces/{id}
 *
 * The list comes from the local known-workspaces registry (the backend has no
 * list endpoint yet — see lib/workspaces.ts). Renames update the registry;
 * deletes remove the workspace and, if it was active, clear the active id.
 * Broadcasts ACTIVE_WORKSPACE_EVENT so the switcher and pages refresh.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiError, api, getActiveWorkspaceId, setActiveWorkspaceId } from "@/lib/api";
import {
  type KnownWorkspace,
  fetchWorkspaces,
  forgetWorkspace,
  listKnownWorkspaces,
  updateKnownWorkspace,
} from "@/lib/workspaces";
import { ACTIVE_WORKSPACE_EVENT } from "@/components/workspace/workspace-switcher";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface RenameResponse {
  id: string;
  name: string;
  slug: string;
}

function broadcast() {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(ACTIVE_WORKSPACE_EVENT));
  }
}

export function ManageWorkspacesDialog({ onClose }: { onClose: () => void }) {
  const [workspaces, setWorkspaces] = useState<KnownWorkspace[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setWorkspaces(listKnownWorkspaces());
    setActiveId(getActiveWorkspaceId());
    let cancelled = false;
    void fetchWorkspaces().then((list) => {
      if (!cancelled) setWorkspaces(list);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  function startEdit(ws: KnownWorkspace) {
    setEditingId(ws.id);
    setDraftName(ws.name);
    setError(null);
  }

  const handleRename = useCallback(async (ws: KnownWorkspace) => {
    const trimmed = draftName.trim();
    if (!trimmed) {
      setError("Workspace name must not be empty.");
      return;
    }
    setBusyId(ws.id);
    setError(null);
    try {
      const res = await api.patch<RenameResponse>(
        `/api/v1/workspaces/${ws.id}`,
        { name: trimmed },
        { headers: { "X-Workspace-Id": ws.id } },
      );
      setWorkspaces(updateKnownWorkspace(ws.id, { name: res.name, slug: res.slug }));
      setEditingId(null);
      broadcast();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to rename workspace.");
    } finally {
      setBusyId(null);
    }
  }, [draftName]);

  const handleDelete = useCallback(async (ws: KnownWorkspace) => {
    setBusyId(ws.id);
    setError(null);
    try {
      await api.del(`/api/v1/workspaces/${ws.id}`, undefined, {
        headers: { "X-Workspace-Id": ws.id },
      });
      const next = forgetWorkspace(ws.id);
      setWorkspaces(next);
      // If we deleted the active workspace, move active to another (or clear).
      if (getActiveWorkspaceId() === ws.id) {
        const fallback = next[0]?.id ?? null;
        setActiveWorkspaceId(fallback);
        setActiveId(fallback);
      }
      setConfirmingId(null);
      broadcast();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to delete workspace.");
    } finally {
      setBusyId(null);
    }
  }, []);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Manage workspaces"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-lg rounded-xl border bg-card text-card-foreground shadow-2xl">
        <div className="flex items-center justify-between border-b px-5 py-4">
          <h2 className="text-lg font-semibold">Manage workspaces</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-md p-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          >
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="max-h-[60vh] overflow-y-auto p-5">
          {error ? (
            <div
              role="alert"
              className="mb-4 rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {error}
            </div>
          ) : null}

          {workspaces.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No workspaces yet. Create one from the switcher in the header.
            </p>
          ) : (
            <ul className="space-y-2">
              {workspaces.map((ws) => {
                const busy = busyId === ws.id;
                const isEditing = editingId === ws.id;
                const isConfirming = confirmingId === ws.id;
                return (
                  <li
                    key={ws.id}
                    className="rounded-lg border p-3"
                  >
                    {isEditing ? (
                      <div className="flex flex-wrap items-center gap-2">
                        <Input
                          className="min-w-0 flex-1"
                          value={draftName}
                          onChange={(e) => setDraftName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") void handleRename(ws);
                          }}
                          autoFocus
                          aria-label="Workspace name"
                        />
                        <Button size="sm" disabled={busy} onClick={() => void handleRename(ws)}>
                          {busy ? "Saving…" : "Save"}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() => setEditingId(null)}
                        >
                          Cancel
                        </Button>
                      </div>
                    ) : (
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="min-w-0">
                          <p className="flex items-center gap-2 font-medium">
                            {ws.name}
                            {activeId === ws.id ? (
                              <span className="rounded-full border px-2 py-0.5 text-xs text-muted-foreground">
                                Active
                              </span>
                            ) : null}
                          </p>
                          <p className="truncate text-xs text-muted-foreground">
                            /{ws.slug}
                          </p>
                        </div>
                        {isConfirming ? (
                          <div className="flex items-center gap-2">
                            <span className="text-xs text-muted-foreground">
                              Delete permanently?
                            </span>
                            <Button
                              size="sm"
                              variant="destructive"
                              disabled={busy}
                              onClick={() => void handleDelete(ws)}
                            >
                              {busy ? "Deleting…" : "Confirm"}
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busy}
                              onClick={() => setConfirmingId(null)}
                            >
                              Cancel
                            </Button>
                          </div>
                        ) : (
                          <div className="flex items-center gap-2">
                            <Button size="sm" variant="outline" onClick={() => startEdit(ws)}>
                              Rename
                            </Button>
                            <Button
                              size="sm"
                              variant="destructive"
                              onClick={() => {
                                setConfirmingId(ws.id);
                                setError(null);
                              }}
                            >
                              Delete
                            </Button>
                          </div>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          <p className="mt-4 text-xs text-muted-foreground">
            Only a workspace Owner can rename or delete a workspace. Deleting is
            permanent and removes its integrations, rules, and history.
          </p>
        </div>
      </div>
    </div>
  );
}
