"use client";

/**
 * User directory (Req 12.3) with role toggle, edit, Ban (Req 12.4), and delete
 * actions. The directory is global across all workspaces. Workspace ownership
 * reassignment lives in the Workspaces panel (Req 12.5).
 */

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  banUser,
  deleteUser,
  setUserRole,
  updateUser,
  type AdminUser,
} from "@/lib/admin";

interface Props {
  users: AdminUser[];
  onChanged: () => void;
}

export function UsersPanel({ users, onChanged }: Props) {
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleBan(user: AdminUser) {
    if (!window.confirm(`Ban ${user.email}? This ends their sessions and blocks sign-in.`))
      return;
    setError(null);
    setPendingId(user.id);
    try {
      await banUser(user.id);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to ban user.");
    } finally {
      setPendingId(null);
    }
  }

  async function handleRole(user: AdminUser) {
    const makeAdmin = !user.is_superadmin;
    const verb = makeAdmin ? "grant super admin to" : "revoke super admin from";
    if (!window.confirm(`Are you sure you want to ${verb} ${user.email}?`)) return;
    setError(null);
    setPendingId(user.id);
    try {
      await setUserRole(user.id, makeAdmin);
      onChanged();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to change role.",
      );
    } finally {
      setPendingId(null);
    }
  }

  async function handleEdit(user: AdminUser) {
    const next = window.prompt(`Edit name for ${user.email}:`, user.name ?? "");
    if (next === null) return; // cancelled
    const trimmed = next.trim();
    if (!trimmed || trimmed === (user.name ?? "")) return; // unchanged or empty
    setError(null);
    setPendingId(user.id);
    try {
      await updateUser(user.id, trimmed);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to update user.");
    } finally {
      setPendingId(null);
    }
  }

  async function handleDelete(user: AdminUser) {
    if (
      !window.confirm(
        `Permanently delete ${user.email}? This cannot be undone and removes all of their data.`,
      )
    )
      return;
    setError(null);
    setPendingId(user.id);
    try {
      await deleteUser(user.id);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to delete user.");
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
              <TableHead>User</TableHead>
              <TableHead>Provider</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {users.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground">
                  No users found.
                </TableCell>
              </TableRow>
            ) : (
              users.map((user) => (
                <TableRow key={user.id}>
                  <TableCell>
                    <div className="font-medium">{user.name || user.email}</div>
                    <div className="text-xs text-muted-foreground">{user.email}</div>
                  </TableCell>
                  <TableCell className="capitalize">{user.auth_provider}</TableCell>
                  <TableCell>
                    {user.is_superadmin ? (
                      <Badge variant="secondary">Super admin</Badge>
                    ) : (
                      <span className="text-muted-foreground">Member</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {user.is_banned ? (
                      <Badge variant="destructive">Banned</Badge>
                    ) : (
                      <Badge variant="success">Active</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={pendingId === user.id}
                        onClick={() => handleRole(user)}
                      >
                        {user.is_superadmin ? "Revoke admin" : "Make admin"}
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={pendingId === user.id}
                        onClick={() => handleEdit(user)}
                      >
                        Edit
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        disabled={pendingId === user.id || user.is_banned}
                        onClick={() => handleBan(user)}
                      >
                        {pendingId === user.id ? "…" : "Ban"}
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        disabled={pendingId === user.id}
                        onClick={() => handleDelete(user)}
                      >
                        Delete
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}
