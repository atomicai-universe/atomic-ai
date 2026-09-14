/**
 * Authenticated API client for the Atomic AI backend.
 *
 * Auth model (Req 2.6): the backend issues an HttpOnly, Secure, SameSite=Lax
 * session cookie on OAuth callback. The browser stores it; JavaScript never
 * reads or holds a token. Every request therefore uses `credentials: "include"`
 * so the cookie is sent (and Set-Cookie honored) cross-origin. The backend must
 * allowlist this origin for CORS with credentials.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/**
 * Active-workspace persistence (Req 3.7).
 *
 * The backend resolves the active workspace from an `X-Workspace-Id` header,
 * which its `RequestContext`/tenant-scoping helper reads to scope subsequent
 * workspace-level operations. The client owns "which workspace am I acting in"
 * because there is no server-side session-scoped workspace switch endpoint yet.
 *
 * We persist the id in `localStorage` so it survives reloads and is readable
 * synchronously when building a request. It is a plain identifier, not a
 * secret, so `localStorage` is acceptable (unlike the session token, which
 * stays in an HttpOnly cookie the client never touches).
 */
const ACTIVE_WORKSPACE_KEY = "atomic.activeWorkspaceId";

/** The workspace id sent as `X-Workspace-Id`, or null if none is selected. */
export function getActiveWorkspaceId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(ACTIVE_WORKSPACE_KEY);
  } catch {
    return null;
  }
}

/** Set (or clear, with null) the active workspace id used for scoping. */
export function setActiveWorkspaceId(id: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (id) {
      window.localStorage.setItem(ACTIVE_WORKSPACE_KEY, id);
    } else {
      window.localStorage.removeItem(ACTIVE_WORKSPACE_KEY);
    }
  } catch {
    /* storage unavailable (private mode, etc.) — scoping falls back to none */
  }
}

/** Shape of the backend's error envelope: `{ error: { code, message, fields? } }`. */
export interface ApiErrorBody {
  code: string;
  message: string;
  fields?: Record<string, unknown>;
}

/** Thrown for any non-2xx response; carries the parsed envelope + status. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields?: Record<string, unknown>;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message || `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code || "error";
    this.fields = body.fields;
  }
}

type Json = Record<string, unknown> | unknown[] | null;

async function request<T>(
  method: string,
  path: string,
  body?: Json,
  init?: RequestInit,
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  // Attach the active workspace so the backend RequestContext scopes the
  // operation to it (Req 3.7). An explicit header in `init` wins.
  const activeWorkspaceId = getActiveWorkspaceId();
  if (activeWorkspaceId && !("X-Workspace-Id" in headers)) {
    headers["X-Workspace-Id"] = activeWorkspaceId;
  }

  const res = await fetch(`${API_BASE_URL}${path}`, {
    method,
    // Send/receive the HttpOnly session cookie (Req 2.6).
    credentials: "include",
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    ...init,
  });

  if (res.status === 204) {
    return undefined as T;
  }

  const text = await res.text();
  const data = text ? JSON.parse(text) : undefined;

  if (!res.ok) {
    const envelope = (data && (data as { error?: ApiErrorBody }).error) || {
      code: "error",
      message: `Request failed with status ${res.status}`,
    };
    throw new ApiError(res.status, envelope);
  }

  return data as T;
}

export const api = {
  get: <T>(path: string, init?: RequestInit) => request<T>("GET", path, undefined, init),
  post: <T>(path: string, body?: Json, init?: RequestInit) =>
    request<T>("POST", path, body, init),
  patch: <T>(path: string, body?: Json, init?: RequestInit) =>
    request<T>("PATCH", path, body, init),
  del: <T>(path: string, body?: Json, init?: RequestInit) =>
    request<T>("DELETE", path, body, init),
};

/** Absolute URL to begin an OAuth login for a provider (backend redirects to it). */
export function oauthLoginUrl(provider: "google" | "github"): string {
  return `${API_BASE_URL}/auth/login/${provider}`;
}

/** The signed-in user's own profile + SMS notification settings. */
export interface Profile {
  id: string;
  email: string;
  name: string;
  auth_provider: string;
  phone_number: string | null;
  phone_country: string | null;
  sms_notifications_enabled: boolean;
}

/** Partial profile update payload (only provided fields change). */
export interface UpdateProfileBody {
  name?: string;
  /** E.164 number, or null to clear it (also disables notifications). */
  phone_number?: string | null;
  phone_country?: string | null;
  sms_notifications_enabled?: boolean;
}

/** Per-country SMS usage row. */
export interface SmsCountryUsage {
  country: string;
  count: number;
  segments: number;
  spend_usd: string;
}

/** SMS usage + estimated spend for the signed-in user. */
export interface SmsUsage {
  total_sent: number;
  total_failed: number;
  total_segments: number;
  total_spend_usd: string;
  currency: string;
  by_country: SmsCountryUsage[];
  note: string;
}

/** Fetch the caller's own profile. */
export function getProfile(): Promise<Profile> {
  return api.get<Profile>("/api/v1/profile");
}

/** Update the caller's own profile (name / phone / SMS settings). */
export function updateProfile(body: UpdateProfileBody): Promise<Profile> {
  return api.patch<Profile>("/api/v1/profile", body as Record<string, unknown>);
}

/** Fetch the caller's SMS usage + estimated spend. */
export function getSmsUsage(): Promise<SmsUsage> {
  return api.get<SmsUsage>("/api/v1/profile/sms-usage");
}
