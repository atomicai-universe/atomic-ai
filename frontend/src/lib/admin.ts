/**
 * Typed client for the super admin control plane (`/api/v1/admin/*`).
 *
 * Every endpoint requires a super admin session; the backend returns HTTP 403
 * for any non-superadmin caller. These helpers use the shared `api` client,
 * which sends the HttpOnly session cookie (`credentials: "include"`) and throws
 * `ApiError` on non-2xx responses — the portal surfaces `ApiError.message` and
 * treats a 403 as "admin access required".
 *
 * Requirements: 12.3, 13.1, 14.1, 14.2 (surfaced), 12.4/12.5/13.2 (actions).
 */

import { api } from "@/lib/api";

// --- System analytics (Req 14.2) --------------------------------------------

export interface SystemAnalytics {
  tokens_total: number;
  active_sessions: number;
  storage_bytes: number;
  table_counts: Record<string, number>;
}

export function getAnalytics(): Promise<SystemAnalytics> {
  return api.get<SystemAnalytics>("/api/v1/admin/analytics");
}

// --- User & workspace directory (Req 12.3) ----------------------------------

export interface AdminUser {
  id: string;
  email: string;
  name: string | null;
  auth_provider: string;
  is_superadmin: boolean;
  is_banned: boolean;
}

export function listUsers(): Promise<{ users: AdminUser[] }> {
  return api.get<{ users: AdminUser[] }>("/api/v1/admin/users");
}

export function banUser(userId: string): Promise<{ status: string; user_id: string }> {
  return api.post(`/api/v1/admin/users/${userId}/ban`);
}

export function setUserRole(
  userId: string,
  isSuperadmin: boolean,
): Promise<{ status: string; user_id: string; is_superadmin: boolean }> {
  return api.post(`/api/v1/admin/users/${userId}/role`, {
    is_superadmin: isSuperadmin,
  });
}

export function updateUser(userId: string, name: string): Promise<AdminUser> {
  return api.patch<AdminUser>(`/api/v1/admin/users/${userId}`, { name });
}

export function deleteUser(
  userId: string,
): Promise<{ status: string; user_id: string }> {
  return api.del(`/api/v1/admin/users/${userId}`);
}

export interface AdminWorkspace {
  id: string;
  name: string;
  slug: string;
  created_by_user_id: string;
  created_at: string;
  member_count: number;
  owner_user_id: string | null;
}

export function listWorkspaces(): Promise<{ workspaces: AdminWorkspace[] }> {
  return api.get<{ workspaces: AdminWorkspace[] }>("/api/v1/admin/workspaces");
}

export function deleteWorkspace(
  workspaceId: string,
): Promise<{ status: string; workspace_id: string }> {
  return api.del(`/api/v1/admin/workspaces/${workspaceId}`);
}

export function reassignOwner(
  workspaceId: string,
  newOwnerUserId: string,
): Promise<{
  status: string;
  workspace_id: string;
  new_owner_user_id: string;
  role: string;
}> {
  return api.post(`/api/v1/admin/workspaces/${workspaceId}/reassign-owner`, {
    new_owner_user_id: newOwnerUserId,
  });
}

// --- Active agent sessions with reasoning traces (Req 13.1) ------------------

export interface AdminSession {
  id: string;
  workspace_id: string;
  triggered_by_user_id: string;
  thread_id: string | null;
  status: string;
  execution_logs: unknown;
  created_at: string;
}

export function listSessions(): Promise<{ sessions: AdminSession[] }> {
  return api.get<{ sessions: AdminSession[] }>("/api/v1/admin/sessions");
}

export function killSession(agentSessionId: string): Promise<{
  agent_session_id: string;
  status: string;
  cancelled_approvals: number;
}> {
  return api.post(`/api/v1/admin/sessions/${agentSessionId}/kill`);
}

// --- MCP server health (Req 14.1) -------------------------------------------

export interface McpProviderHealth {
  name: string;
  total: number;
  active: number;
  error: number;
  disconnected: number;
  error_rate: number;
  tokens_used: number;
}

export interface McpHealth {
  providers: McpProviderHealth[];
  tokens_total: number;
}

export function getMcpHealth(): Promise<McpHealth> {
  return api.get<McpHealth>("/api/v1/admin/mcp-health");
}
