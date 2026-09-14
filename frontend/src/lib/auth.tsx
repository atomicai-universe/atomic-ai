"use client";

/**
 * Lightweight client-side auth state.
 *
 * There is no dedicated `/auth/me` endpoint on the backend, so login state is
 * inferred by calling a protected endpoint and treating HTTP 401 as
 * "logged out" (Req 2.6 — the session lives in an HttpOnly cookie the client
 * cannot read). Consumers use `useSession()` to gate UI.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { ApiError, api } from "@/lib/api";

export type AuthStatus = "loading" | "authenticated" | "unauthenticated";

interface SessionState {
  status: AuthStatus;
  /** Re-check auth state (e.g. after returning from the OAuth callback). */
  refresh: () => Promise<void>;
}

const SessionContext = createContext<SessionState | undefined>(undefined);

/** A protected endpoint used purely as an auth probe (401 => logged out). */
const AUTH_PROBE_PATH = "/api/v1/workspaces";

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");

  const refresh = useCallback(async () => {
    try {
      await api.get(AUTH_PROBE_PATH);
      setStatus("authenticated");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setStatus("unauthenticated");
      } else {
        // Network/other error: treat as unauthenticated but do not crash the UI.
        setStatus("unauthenticated");
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo<SessionState>(() => ({ status, refresh }), [status, refresh]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const ctx = useContext(SessionContext);
  if (ctx === undefined) {
    throw new Error("useSession must be used within a <SessionProvider>");
  }
  return ctx;
}
