"use client";

/**
 * Client-side registry of "workspaces this browser knows about".
 *
 * The backend does not (yet) expose a `GET /api/v1/workspaces` that lists the
 * caller's memberships, so the switcher cannot fetch the set of workspaces to
 * switch between. As an interim measure the client remembers every workspace it
 * has created (and could later remember accepted invites) in `localStorage`,
 * keyed alongside the active-workspace id used for `X-Workspace-Id` scoping.
 *
 * These are non-secret identifiers/labels, so `localStorage` is appropriate.
 * When a list endpoint lands (see task 7.5 notes), `listKnownWorkspaces` should
 * be replaced by an `api.get("/api/v1/workspaces")` call and this module can be
 * removed.
 */

import { api } from "@/lib/api";

export interface KnownWorkspace {
  id: string;
  name: string;
  slug: string;
  /** The caller's role in this workspace, when known (from the server list). */
  role?: string;
}

const KNOWN_WORKSPACES_KEY = "atomic.knownWorkspaces";

/** Read the locally-remembered workspaces (empty if none / storage blocked). */
export function listKnownWorkspaces(): KnownWorkspace[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(KNOWN_WORKSPACES_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (w): w is KnownWorkspace =>
        typeof w?.id === "string" &&
        typeof w?.name === "string" &&
        typeof w?.slug === "string",
    );
  } catch {
    return [];
  }
}

/** Add or update a known workspace, de-duplicating by id. Returns the new list. */
export function rememberWorkspace(ws: KnownWorkspace): KnownWorkspace[] {
  const existing = listKnownWorkspaces().filter((w) => w.id !== ws.id);
  const next = [...existing, ws];
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(KNOWN_WORKSPACES_KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable — the switcher just won't persist across reloads */
    }
  }
  return next;
}

/** Persist a full list of known workspaces (internal helper). */
function writeKnownWorkspaces(list: KnownWorkspace[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(KNOWN_WORKSPACES_KEY, JSON.stringify(list));
  } catch {
    /* storage unavailable — changes just won't persist across reloads */
  }
}

/** Update the name/slug of a known workspace by id. Returns the new list. */
export function updateKnownWorkspace(
  id: string,
  patch: Partial<Omit<KnownWorkspace, "id">>,
): KnownWorkspace[] {
  const next = listKnownWorkspaces().map((w) =>
    w.id === id ? { ...w, ...patch } : w,
  );
  writeKnownWorkspaces(next);
  return next;
}

/** Remove a known workspace by id. Returns the new list. */
export function forgetWorkspace(id: string): KnownWorkspace[] {
  const next = listKnownWorkspaces().filter((w) => w.id !== id);
  writeKnownWorkspaces(next);
  return next;
}


interface ServerWorkspace {
  id: string;
  name: string;
  slug: string;
  role: string;
}

interface ListWorkspacesResponse {
  workspaces: ServerWorkspace[];
}

/**
 * Fetch the caller's workspaces from the authoritative backend endpoint
 * (`GET /api/v1/workspaces`) and mirror them into the local registry so the
 * list is consistent across devices and page loads. Falls back to the local
 * registry if the request fails (e.g. offline or logged out).
 */
export async function fetchWorkspaces(): Promise<KnownWorkspace[]> {
  try {
    const res = await api.get<ListWorkspacesResponse>("/api/v1/workspaces");
    const list: KnownWorkspace[] = res.workspaces.map((w) => ({
      id: w.id,
      name: w.name,
      slug: w.slug,
      role: w.role,
    }));
    writeKnownWorkspaces(list);
    return list;
  } catch {
    // Network error / unauthenticated — fall back to whatever we remembered.
    return listKnownWorkspaces();
  }
}
