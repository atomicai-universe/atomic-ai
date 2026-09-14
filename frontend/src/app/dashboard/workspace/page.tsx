"use client";

/**
 * Team & Workspace settings (task 17.2, Req 5.1 / 3.7).
 *
 * Owner-facing controls for the active workspace (the one selected in the header
 * switcher, sent as `X-Workspace-Id`):
 *
 *  - Invite a team member by email with a role (Admin/Member/Viewer — Owner is
 *    not invitable). Calls `POST /api/v1/workspaces/{id}/invites` and shows the
 *    returned hex invite token so the Owner can forward it to the invitee.
 *  - Manage a member's role via `PATCH /api/v1/workspaces/{id}/members/{user_id}`.
 *    The backend exposes no member-list endpoint yet, so this is an id-targeted
 *    form rather than a table; when a list endpoint lands it can populate a
 *    roster to pick from.
 *  - Activity logs: a placeholder. There is no workspace-activity/audit-read
 *    endpoint yet; this section documents that it will render audit-log entries
 *    for the workspace once that endpoint is available.
 *
 * All calls go through the API client, which attaches `X-Workspace-Id` from the
 * active workspace. Failures surface `ApiError.message`.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { ApiError, api, getActiveWorkspaceId } from "@/lib/api";
import { ACTIVE_WORKSPACE_EVENT } from "@/components/workspace/workspace-switcher";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { buttonClasses } from "@/lib/utils";

/** Roles that can be invited (Owner is not invitable — Req 5.7). */
const INVITE_ROLES = ["admin", "member", "viewer"] as const;
type InviteRole = (typeof INVITE_ROLES)[number];

/** Roles a member can be assigned via role management (Owner included). */
const MEMBER_ROLES = ["owner", "admin", "member", "viewer"] as const;
type MemberRole = (typeof MEMBER_ROLES)[number];

interface InviteResponse {
  id: string;
  email: string;
  role: string;
  status: string;
  token: string;
}

interface MemberResponse {
  workspace_id: string;
  user_id: string;
  role: string;
}

function roleLabel(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1);
}

export default function WorkspacePage() {
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(null);

  useEffect(() => {
    const sync = () => setActiveWorkspaceId(getActiveWorkspaceId());
    sync();
    window.addEventListener(ACTIVE_WORKSPACE_EVENT, sync);
    return () => window.removeEventListener(ACTIVE_WORKSPACE_EVENT, sync);
  }, []);

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-8">
      <div>
        <h1 className="text-2xl font-semibold">Team &amp; Workspace</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Invite members, manage roles, and review activity for the active
          workspace.
        </p>
      </div>

      <GetStartedCard />

      {!activeWorkspaceId && (
        <Card>
          <CardContent className="p-6 text-sm text-muted-foreground">
            Select or create a workspace from the switcher above to manage its
            team.
          </CardContent>
        </Card>
      )}

      <InviteSection workspaceId={activeWorkspaceId} />
      <MemberRoleSection workspaceId={activeWorkspaceId} />
      <ActivityLogSection />
    </main>
  );
}

function GetStartedCard() {
  return (
    <Card className="overflow-hidden border-primary/40 bg-gradient-to-br from-primary/10 via-card to-card">
      <CardContent className="flex flex-wrap items-center justify-between gap-4 p-6">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-base font-semibold">
            <span aria-hidden>🚀</span> Ready to automate? Connect a service first
          </p>
          <p className="mt-1 max-w-xl text-sm text-muted-foreground">
            Inviting teammates is optional. To put your agents to work, add the
            services and workloads you want to automate or integrate — then set
            rules for how they behave.
          </p>
        </div>
        <Link href="/dashboard/integrations" className={buttonClasses()}>
          Add a service to automate →
        </Link>
      </CardContent>
    </Card>
  );
}

function InviteSection({ workspaceId }: { workspaceId: string | null }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<InviteRole>("member");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invite, setInvite] = useState<InviteResponse | null>(null);
  const [copied, setCopied] = useState(false);

  const handleInvite = useCallback(async () => {
    if (!workspaceId) {
      setError("Select a workspace first.");
      return;
    }
    const trimmed = email.trim();
    if (!trimmed) {
      setError("Enter an email address.");
      return;
    }
    setSubmitting(true);
    setError(null);
    setInvite(null);
    setCopied(false);
    try {
      const res = await api.post<InviteResponse>(
        `/api/v1/workspaces/${workspaceId}/invites`,
        { email: trimmed, role },
      );
      setInvite(res);
      setEmail("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to send invite.");
    } finally {
      setSubmitting(false);
    }
  }, [workspaceId, email, role]);

  const handleCopy = useCallback(async () => {
    if (!invite) return;
    try {
      await navigator.clipboard.writeText(invite.token);
      setCopied(true);
    } catch {
      /* clipboard blocked — the token is still visible for manual copy */
    }
  }, [invite]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Invite a team member</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-[1fr_auto_auto] sm:items-end">
          <div className="space-y-1.5">
            <Label htmlFor="invite-email">Email</Label>
            <Input
              id="invite-email"
              type="email"
              placeholder="teammate@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={!workspaceId || submitting}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="invite-role">Role</Label>
            <Select
              id="invite-role"
              className="w-36"
              value={role}
              onChange={(e) => setRole(e.target.value as InviteRole)}
              disabled={!workspaceId || submitting}
            >
              {INVITE_ROLES.map((r) => (
                <option key={r} value={r}>
                  {roleLabel(r)}
                </option>
              ))}
            </Select>
          </div>
          <Button
            onClick={() => void handleInvite()}
            disabled={!workspaceId || submitting}
          >
            {submitting ? "Sending…" : "Send invite"}
          </Button>
        </div>

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        {invite && (
          <div className="rounded-md border bg-muted/40 p-4">
            <p className="text-sm font-medium">
              Invite created for {invite.email} ({roleLabel(invite.role)})
            </p>
            <p className="mt-1 text-sm text-muted-foreground">
              Share this token with the invitee to accept the invitation:
            </p>
            <div className="mt-2 flex items-center gap-2">
              <code className="block w-full overflow-x-auto rounded bg-background px-2 py-1 text-xs">
                {invite.token}
              </code>
              <Button size="sm" variant="outline" onClick={() => void handleCopy()}>
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function MemberRoleSection({ workspaceId }: { workspaceId: string | null }) {
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState<MemberRole>("member");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<MemberResponse | null>(null);

  const handleUpdate = useCallback(async () => {
    if (!workspaceId) {
      setError("Select a workspace first.");
      return;
    }
    const trimmed = userId.trim();
    if (!trimmed) {
      setError("Enter the member's user id.");
      return;
    }
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.patch<MemberResponse>(
        `/api/v1/workspaces/${workspaceId}/members/${trimmed}`,
        { role },
      );
      setResult(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to update role.");
    } finally {
      setSubmitting(false);
    }
  }, [workspaceId, userId, role]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Manage member roles</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Update a member&apos;s role by their user id. A member roster will
          replace this form once a member-list endpoint is available.
        </p>
        <div className="grid gap-4 sm:grid-cols-[1fr_auto_auto] sm:items-end">
          <div className="space-y-1.5">
            <Label htmlFor="member-user-id">Member user id</Label>
            <Input
              id="member-user-id"
              placeholder="00000000-0000-0000-0000-000000000000"
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              disabled={!workspaceId || submitting}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="member-role">Role</Label>
            <Select
              id="member-role"
              className="w-36"
              value={role}
              onChange={(e) => setRole(e.target.value as MemberRole)}
              disabled={!workspaceId || submitting}
            >
              {MEMBER_ROLES.map((r) => (
                <option key={r} value={r}>
                  {roleLabel(r)}
                </option>
              ))}
            </Select>
          </div>
          <Button
            onClick={() => void handleUpdate()}
            disabled={!workspaceId || submitting}
          >
            {submitting ? "Updating…" : "Update role"}
          </Button>
        </div>

        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        {result && (
          <p className="text-sm text-muted-foreground">
            Updated member {result.user_id} to {roleLabel(result.role)}.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function ActivityLogSection() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Activity log</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          Workspace activity will appear here. This view will render the
          workspace&apos;s audit-log entries (member changes, invites, and other
          state-changing operations) once a read endpoint for audit logs is
          available on the backend.
        </p>
      </CardContent>
    </Card>
  );
}
