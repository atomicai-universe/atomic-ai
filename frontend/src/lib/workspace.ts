/**
 * Active-workspace helpers (re-exported from the API client).
 *
 * The active workspace id is persisted in localStorage and sent by the API
 * client as the `X-Workspace-Id` header so the backend RequestContext scopes
 * operations to it. The canonical implementation lives in `@/lib/api`; this
 * module re-exports it (plus a stable storage-key constant) so pages that
 * import `getActiveWorkspaceId`/`setActiveWorkspaceId` from `@/lib/workspace`
 * share that single source of truth.
 *
 * Pages still pass an explicit `workspace_id` in request bodies/query strings
 * where the backend endpoint expects it; that value and the header agree
 * because both derive from the same stored id.
 */

export { getActiveWorkspaceId, setActiveWorkspaceId } from "@/lib/api";

/** localStorage key holding the active workspace id (matches the API client). */
export const ACTIVE_WORKSPACE_STORAGE_KEY = "atomic.activeWorkspaceId";
