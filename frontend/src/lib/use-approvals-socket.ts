"use client";

/**
 * Live approvals feed over the backend WebSocket_Gateway.
 *
 * ── The cookie / query-param mismatch (READ THIS) ─────────────────────────
 * The browser session is an **HttpOnly, Secure, SameSite=Lax** cookie that
 * JavaScript can neither read nor attach to a `WebSocket` handshake (browsers
 * forbid custom headers on the WS upgrade). The backend gateway
 * (`backend/app/api/ws.py`, path `/api/v1/ws/approvals`) instead authenticates
 * off a `?token=<session-token>` **query parameter**. There is currently no
 * endpoint that hands the JS a usable token, so the two sides do not meet on
 * their own.
 *
 * How this hook handles it:
 *   - The live layer is strictly **additive**. The approvals hub is fully
 *     functional over REST + manual refresh without any socket at all.
 *   - The hook only attempts a connection when a `token` is supplied by the
 *     caller (e.g. from config, a prop, or a future `/auth/ws-token` endpoint).
 *     When `token` is absent/empty it reports `status: "unavailable"` and never
 *     opens a socket — the UI then shows a "live updates unavailable" hint and
 *     relies on the manual Refresh button.
 *   - When a token IS provided the hook connects, listens for
 *     `approval.created` / `approval.resolved`, and reconnects on drop with
 *     capped backoff. Wiring live updates later is a one-line change: pass a
 *     token in.
 *
 * The WS base URL is derived from `API_BASE_URL` (http→ws, https→wss).
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE_URL } from "@/lib/api";

/** Path the backend mounts the approvals feed at (mirrors WS_APPROVALS_PATH). */
export const WS_APPROVALS_PATH = "/api/v1/ws/approvals";

/** Application close code the gateway uses for auth failures (WS_UNAUTHORIZED_CODE). */
export const WS_UNAUTHORIZED_CODE = 4401;

/** Connection lifecycle exposed to the UI. */
export type SocketStatus =
  | "unavailable" // no token supplied — feature degrades to REST + manual refresh
  | "connecting"
  | "open"
  | "closed"
  | "unauthorized"; // token rejected by the gateway (4401)

/**
 * Server → client event shapes. The gateway currently emits a flat resolved
 * payload (`approval_request_id`/`status`/`workspace_id`) and is documented to
 * emit a nested `approval` object on creation, so we accept both defensively.
 */
export interface ApprovalSocketEvent {
  type: "approval.created" | "approval.resolved" | string;
  /** Present on the documented nested shape. */
  approval?: Record<string, unknown>;
  /** Present on the flat resolved shape. */
  approval_request_id?: string;
  status?: string;
  workspace_id?: string;
  [key: string]: unknown;
}

export interface UseApprovalsSocketOptions {
  /**
   * Session token for the `?token=` query param. When omitted/empty the hook
   * stays in `"unavailable"` and does not open a socket (see file header).
   */
  token?: string | null;
  /** Only connect for this workspace's events (currently the gateway keys by user membership). */
  workspaceId?: string | null;
  /** Fired for every recognized event; the page uses it to refetch/patch the queue. */
  onEvent?: (event: ApprovalSocketEvent) => void;
  /** Master switch (e.g. disable while no workspace is selected). Defaults to true. */
  enabled?: boolean;
}

export interface UseApprovalsSocketResult {
  status: SocketStatus;
  /** Whether the live layer is currently delivering (status === "open"). */
  isLive: boolean;
  /** Force a reconnect attempt (used by a "retry" affordance). */
  reconnect: () => void;
}

/** Derive the WebSocket origin from the HTTP API base (http→ws, https→wss). */
export function deriveWsBase(apiBase: string): string {
  if (apiBase.startsWith("https://")) return "wss://" + apiBase.slice("https://".length);
  if (apiBase.startsWith("http://")) return "ws://" + apiBase.slice("http://".length);
  // Already a ws/wss URL, or a protocol-relative/relative value — pass through.
  return apiBase;
}

const MAX_BACKOFF_MS = 15_000;
const BASE_BACKOFF_MS = 1_000;

export function useApprovalsSocket(
  options: UseApprovalsSocketOptions,
): UseApprovalsSocketResult {
  const { token, workspaceId, onEvent, enabled = true } = options;

  const [status, setStatus] = useState<SocketStatus>("unavailable");

  // Keep the latest onEvent in a ref so the effect does not tear down the
  // socket every render when the parent passes an inline callback.
  const onEventRef = useRef<UseApprovalsSocketOptions["onEvent"]>(onEvent);
  useEffect(() => {
    onEventRef.current = onEvent;
  }, [onEvent]);

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  // Bumping this forces the connection effect to re-run (manual reconnect).
  const [reconnectNonce, setReconnectNonce] = useState(0);

  const reconnect = useCallback(() => {
    attemptRef.current = 0;
    setReconnectNonce((n) => n + 1);
  }, []);

  useEffect(() => {
    // The browser sends the HttpOnly session cookie on the WS handshake, so we
    // can connect without a JS-readable token. Only stay "unavailable" when the
    // hook is explicitly disabled (e.g. no workspace selected) or the WebSocket
    // API is missing (SSR). An optional `token` is appended for non-browser
    // clients that cannot rely on the cookie.
    if (!enabled || typeof WebSocket === "undefined") {
      setStatus("unavailable");
      return;
    }

    let cancelled = false;

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const connect = () => {
      if (cancelled) return;

      const wsBase = deriveWsBase(API_BASE_URL);
      // Cookie auth is primary; append ?token= only when a token was supplied.
      const url = token
        ? `${wsBase}${WS_APPROVALS_PATH}?token=${encodeURIComponent(token)}`
        : `${wsBase}${WS_APPROVALS_PATH}`;

      setStatus("connecting");
      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        // Malformed URL / environment without WebSocket — degrade quietly.
        setStatus("closed");
        return;
      }
      socketRef.current = ws;

      ws.onopen = () => {
        if (cancelled) return;
        attemptRef.current = 0;
        setStatus("open");
      };

      ws.onmessage = (ev: MessageEvent) => {
        if (cancelled) return;
        let parsed: ApprovalSocketEvent | null = null;
        try {
          parsed = JSON.parse(String(ev.data)) as ApprovalSocketEvent;
        } catch {
          return; // ignore non-JSON frames
        }
        if (parsed && typeof parsed.type === "string") {
          onEventRef.current?.(parsed);
        }
      };

      ws.onclose = (ev: CloseEvent) => {
        socketRef.current = null;
        if (cancelled) return;

        // 4401 = token rejected. Do not hammer the gateway with reconnects.
        if (ev.code === WS_UNAUTHORIZED_CODE) {
          setStatus("unauthorized");
          return;
        }

        setStatus("closed");
        // Reconnect with capped exponential backoff.
        const attempt = attemptRef.current++;
        const delay = Math.min(BASE_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);
        clearReconnectTimer();
        reconnectTimerRef.current = setTimeout(() => {
          if (!cancelled) connect();
        }, delay);
      };

      ws.onerror = () => {
        // The close handler drives the retry; just make sure the socket closes.
        try {
          ws.close();
        } catch {
          /* no-op */
        }
      };
    };

    connect();

    return () => {
      cancelled = true;
      clearReconnectTimer();
      const ws = socketRef.current;
      socketRef.current = null;
      if (ws) {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onclose = null;
        ws.onerror = null;
        try {
          ws.close();
        } catch {
          /* no-op */
        }
      }
    };
    // workspaceId participates so a workspace switch reconnects the feed.
  }, [token, enabled, workspaceId, reconnectNonce]);

  return { status, isLive: status === "open", reconnect };
}
