"use client";

/**
 * Approvals REST client + shared types (task 17.4).
 *
 * Wraps the backend approvals endpoints under `/api/v1/approvals`:
 *   - `GET /?workspace_id=&status=` → `{ approvals: Approval[] }`
 *   - `POST /{id}/approve`          → the updated `Approval`
 *   - `POST /{id}/reject`           → the updated `Approval`
 *
 * `arguments` are already scrubbed server-side (no secret-looking value is
 * returned), so it is safe to render the proposed action for review.
 */

import { api } from "@/lib/api";

export type ApprovalStatus = "pending" | "approved" | "rejected";

/** A single approval request as returned by the backend serializer. */
export interface Approval {
  id: string;
  tool_name: string;
  /** Proposed tool arguments — pre-scrubbed JSON, safe to render as a preview. */
  arguments: unknown;
  status: ApprovalStatus;
  triggered_by_user_id: string;
  reviewed_by_user_id: string | null;
  agent_session_id: string | null;
  created_at: string | null;
  /**
   * When set, this pending reply is scheduled to be sent automatically at this
   * ISO-8601 UTC time (via "Approve and Schedule"). `null`/absent otherwise.
   */
  scheduled_send_at?: string | null;
}

interface ListApprovalsResponse {
  approvals: Approval[];
}

/** List a workspace's approval requests, optionally filtered by status. */
export async function listApprovals(
  workspaceId: string,
  status?: ApprovalStatus,
): Promise<Approval[]> {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  if (status) params.set("status", status);
  const res = await api.get<ListApprovalsResponse>(
    `/api/v1/approvals?${params.toString()}`,
  );
  return res.approvals ?? [];
}

/** Approve a pending request (Owner/Admin only, 409 if already resolved). */
export function approveApproval(id: string): Promise<Approval> {
  return api.post<Approval>(`/api/v1/approvals/${id}/approve`);
}

/** Reject a pending request (Owner/Admin only, 409 if already resolved). */
export function rejectApproval(id: string): Promise<Approval> {
  return api.post<Approval>(`/api/v1/approvals/${id}/reject`);
}

/** Shape returned by the bulk clear-pending endpoint. */
export interface ClearPendingResult {
  cleared: number;
}

/**
 * Bulk-REJECT every pending reply in a workspace (keeps the audit trail).
 * Owner/Admin only (403 for viewer/member, 404 for a non-member). Returns the
 * count of approvals that were cleared. Destructive — call behind a confirm.
 */
export async function clearPendingApprovals(
  workspaceId: string,
): Promise<ClearPendingResult> {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  const res = await api.post<ClearPendingResult>(
    `/api/v1/approvals/clear-pending?${params.toString()}`,
  );
  return { cleared: res?.cleared ?? 0 };
}

/**
 * Regenerate a NEW AI reply body for a pending approval's same source email.
 * Owner/Admin only; pending-only (409). Returns the updated approval.
 */
export function regenerateApproval(id: string): Promise<Approval> {
  return api.post<Approval>(`/api/v1/approvals/${id}/regenerate`);
}

/** Fields a reviewer may directly edit on a pending AI reply. */
export interface EditApprovalFields {
  subject?: string;
  body?: string;
  to?: string;
  [key: string]: unknown;
}

/**
 * Directly edit a pending reply's Subject/Body/To; the backend rebuilds the
 * draft with the same clean message builder. Owner/Admin only; pending-only.
 */
export function editApproval(
  id: string,
  fields: EditApprovalFields,
): Promise<Approval> {
  return api.patch<Approval>(`/api/v1/approvals/${id}`, fields);
}

/**
 * Approve a pending reply AND create the Gmail draft for real. Owner/Admin
 * only; a Gmail failure leaves the request pending (502).
 */
export function approveDraft(id: string): Promise<Approval> {
  return api.post<Approval>(`/api/v1/approvals/${id}/approve-draft`);
}

/**
 * Approve a pending reply AND send it immediately. Owner/Admin only; a Gmail
 * failure leaves the request pending (502).
 */
export function approveSend(id: string): Promise<Approval> {
  return api.post<Approval>(`/api/v1/approvals/${id}/approve-send`);
}

/**
 * Schedule a pending reply to be SENT automatically at a future time. The
 * approval stays pending; a worker cron sweeps due ones. Owner/Admin only;
 * pending-only (409); a non-future time is rejected (422).
 *
 * @param scheduledSendAtISO an ISO-8601 UTC timestamp (e.g. from
 *   `new Date(localValue).toISOString()`).
 */
export function approveSchedule(
  id: string,
  scheduledSendAtISO: string,
): Promise<Approval> {
  return api.post<Approval>(`/api/v1/approvals/${id}/approve-schedule`, {
    scheduled_send_at: scheduledSendAtISO,
  });
}

/** Short, human-friendly identifier fragment for a UUID (for reviewer/user tags). */
export function shortId(id: string | null | undefined): string {
  if (!id) return "unknown";
  return id.length > 8 ? id.slice(0, 8) : id;
}

/** Format an ISO timestamp for display; falls back gracefully on bad input. */
export function formatTimestamp(iso: string | null): string {
  if (!iso) return "unknown time";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}
