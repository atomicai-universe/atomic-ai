"use client";

/**
 * Workspace directory (global across all tenants) with assign-owner (Req 12.5)
 * and destructive delete actions.
 *
 * Ownership is reassigned via a proper owner picker (a native `<select>`
 * populated from the platform user directory) rather than pasting a user id.
 * Deleting a workspace is irreversible and cascades every dependent record
 * (members, integrations, rules, sessions, approvals, invites); audit logs are
 * retained. The confirm dialog spells this out before the call is made.
 */

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError } from "@/lib/api";
import {
  deleteWorkspace,
  reassignOwner,
  type AdminUser,
  type AdminWorkspace,
} from "@/lib/admin";

interface Props {
  workspaces: AdminWorkspace[];
  users: AdminUser[];
  onChanged: () => void;
}

function userLabel(user: AdminUser): string {
  return user.email || user.name || user.id;
}

export function WorkspacesPanel({ workspaces, users, onChanged }: Props) {
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Per-workspace selected owner in the picker (defaults to current owner).
  const [selectedOwner, setSelectedOwner] = useState<Record<string, string>>({});

  const usersById = new Map(users.map((u) => [u.id, u]));

  function ownerFor(workspace: AdminWorkspace): string {
    return (
      selectedOwner[workspace.id] ??
      workspace.owner_user_id ??
      users[0]?.id ??
      ""
    );
  }

  async function handleAssign(workspace: AdminWorkspace) {
    const newOwnerId = ownerFor(workspace);
    if (!newOwnerId) return;
    setError(null);
    setPendingId(workspace.id);
    try {
      await reassignOwner(workspace.id, newOwnerId);
      onChanged();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to assign owner.",
      );
    } finally {
      setPendingId(null);
    }
  }

  async function handleDelete(workspace: AdminWorkspace) {
    if (
      !window.confirm(
        `Permanently delete workspace "${workspace.name}"? This cannot be undone ` +
          `and removes all of its members, integrations, rules, agent sessions, ` +
          `approvals, and invites.`,
      )
    )
      return;
    setError(null);
    setPendingId(workspace.id);
    try {
      await deleteWorkspace(workspace.id);
      onChanged();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to delete workspace.",
      );
    } finally {
      setPendingId(null);
    }
  }

  return (
    <section className="space-y-3">
      {error && (
        <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Workspace</TableHead>
              <TableHead>Owner</TableHead>
              <TableHead>Members</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {workspaces.length === 0 ? (
              <TableRow>
                <TableCell colSpan={4} className="text-center text-muted-foreground">
                  No workspaces found.
                </TableCell>
              </TableRow>
            ) : (
              workspaces.map((workspace) => {
                const owner = workspace.owner_user_id
                  ? usersById.get(workspace.owner_user_id)
                  : undefined;
                const busy = pendingId === workspace.id;
                return (
                  <TableRow key={workspace.id}>
                    <TableCell>
                      <div className="font-medium">{workspace.name}</div>
                      <div className="text-xs text-muted-foreground">
                        {workspace.slug}
                      </div>
                    </TableCell>
                    <TableCell>
                      {owner ? (
                        owner.email
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell>{workspace.member_count}</TableCell>
                    <TableCell className="text-right">
                      <div className="flex flex-wrap items-center justify-end gap-2">
                        <Select
                          className="h-9 w-48"
                          aria-label={`Assign owner for ${workspace.name}`}
                          value={ownerFor(workspace)}
                          disabled={busy || users.length === 0}
                          onChange={(e) =>
                            setSelectedOwner((prev) => ({
                              ...prev,
                              [workspace.id]: e.target.value,
                            }))
                          }
                        >
                          {users.map((user) => (
                            <option key={user.id} value={user.id}>
                              {userLabel(user)}
                            </option>
                          ))}
                        </Select>
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busy || !ownerFor(workspace)}
                          onClick={() => handleAssign(workspace)}
                        >
                          Assign owner
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          disabled={busy}
                          onClick={() => handleDelete(workspace)}
                        >
                          {busy ? "…" : "Delete"}
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}
