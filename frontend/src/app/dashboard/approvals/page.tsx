"use client";

/**
 * Collaborative Approvals Hub (task 17.4, Req 10.2 / 10.8).
 *
 * A real-time review queue for high-impact agent actions:
 *  - Fetches PENDING approvals for the active workspace over REST and renders
 *    each with tool name, a preview of the proposed arguments (pre-scrubbed
 *    JSON), who triggered it, and when — with Approve / Reject actions
 *    (Owner/Admin only; the backend returns 403 otherwise).
 *  - Layers LIVE updates over a WebSocket: `approval.created` prepends/refreshes
 *    the queue, `approval.resolved` moves the item to the resolved history with
 *    a reviewer tag. See the WS-token note below.
 *  - Keeps a resolved history section with reviewer tags so a team can see who
 *    acted on what.
 *
 * ── WS-token cookie/query mismatch (Req 11.1/11.2) ────────────────────────
 * The browser session is an HttpOnly cookie the JS cannot read, but the
 * gateway authenticates the WebSocket off a `?token=` query param. There is no
 * endpoint yet that hands the client a token, so the live layer is ADDITIVE:
 * the hub is fully usable over REST + the manual Refresh button, and shows a
 * "live updates unavailable" indicator when no token is present. The moment a
 * token becomes obtainable (config, prop, or a future `/auth/ws-token`
 * endpoint), pass it to `useApprovalsSocket` and live updates light up with no
 * other change. See `lib/use-approvals-socket.ts` for the full rationale.
 */

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";

import { ApiError, getActiveWorkspaceId } from "@/lib/api";
import {
  type ApprovalVoiceAction,
  VOICE_APPROVAL_EVENT,
  isoToDatetimeLocal,
} from "@/lib/voice-actions";
import { ACTIVE_WORKSPACE_EVENT } from "@/components/workspace/workspace-switcher";
import {
  type Approval,
  type EditApprovalFields,
  approveDraft,
  approveSchedule,
  approveSend,
  clearPendingApprovals,
  editApproval,
  formatTimestamp,
  listApprovals,
  regenerateApproval,
  rejectApproval,
  shortId,
} from "@/lib/approvals";
import {
  type ApprovalSocketEvent,
  type SocketStatus,
  useApprovalsSocket,
} from "@/lib/use-approvals-socket";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  RichTextEditor,
  type RichTextEditorHandle,
} from "@/components/ui/rich-text-editor";
import { replaceTextCI } from "@/lib/voice-editor-registry";

/**
 * Optional session token for the live WebSocket. The browser authenticates the
 * WebSocket with the HttpOnly session cookie it sends automatically on the
 * handshake, so no JS-readable token is needed in the browser and this stays
 * null. A token can be supplied via NEXT_PUBLIC_WS_TOKEN for non-browser
 * clients that cannot rely on the cookie.
 */
const WS_TOKEN: string | null =
  process.env.NEXT_PUBLIC_WS_TOKEN?.trim() || null;

export default function ApprovalsPage() {
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [pending, setPending] = useState<Approval[]>([]);
  const [resolved, setResolved] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // "Clear all" (bulk reject) state: an inline two-button confirm gates the
  // destructive action; `clearing` shows the busy state while it runs.
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [clearing, setClearing] = useState(false);

  // Guard against out-of-order refetches (workspace switch / rapid events).
  const fetchSeq = useRef(0);

  // Resolve the active workspace on mount and whenever the switcher changes it.
  useEffect(() => {
    const sync = () => setWorkspaceId(getActiveWorkspaceId());
    sync();
    if (typeof window === "undefined") return;
    window.addEventListener(ACTIVE_WORKSPACE_EVENT, sync);
    // Also react to another tab changing the active workspace.
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(ACTIVE_WORKSPACE_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!workspaceId) {
      setPending([]);
      setResolved([]);
      setLoading(false);
      return;
    }
    const seq = ++fetchSeq.current;
    setLoading(true);
    setError(null);
    try {
      // Pending drives the live queue; the rest form the resolved history.
      const [pendingList, allList] = await Promise.all([
        listApprovals(workspaceId, "pending"),
        listApprovals(workspaceId),
      ]);
      if (seq !== fetchSeq.current) return; // superseded by a newer refresh
      setPending(pendingList);
      setResolved(allList.filter((a) => a.status !== "pending"));
    } catch (err) {
      if (seq !== fetchSeq.current) return;
      setError(err instanceof ApiError ? err.message : "Failed to load approvals.");
    } finally {
      if (seq === fetchSeq.current) setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Live layer: refetch on any recognized event so the REST snapshot stays the
  // source of truth (handles both the nested `approval` and flat resolved
  // payload shapes the gateway may emit).
  const handleEvent = useCallback(
    (event: ApprovalSocketEvent) => {
      if (
        event.type === "approval.created" ||
        event.type === "approval.resolved" ||
        event.type === "approval.updated"
      ) {
        void refresh();
      }
    },
    [refresh],
  );

  const { status: socketStatus, reconnect } = useApprovalsSocket({
    token: WS_TOKEN,
    workspaceId,
    enabled: Boolean(workspaceId),
    onEvent: handleEvent,
  });

  // Approve (create draft or send) / reject — all move the item to history.
  const resolve = useCallback(
    async (id: string, action: "approve-draft" | "approve-send" | "reject") => {
      setBusyId(id);
      setActionError(null);
      // Optimistically remove from the pending queue.
      const previous = pending;
      setPending((cur) => cur.filter((a) => a.id !== id));
      try {
        const updated =
          action === "approve-draft"
            ? await approveDraft(id)
            : action === "approve-send"
              ? await approveSend(id)
              : await rejectApproval(id);
        // Fold the resolved item into history (dedupe by id).
        setResolved((cur) => [updated, ...cur.filter((a) => a.id !== id)]);
      } catch (err) {
        // Roll back the optimistic removal and surface the error.
        setPending(previous);
        setActionError(
          err instanceof ApiError ? err.message : "Failed to resolve the request.",
        );
        // A 409 means someone else already resolved it — resync to be safe.
        if (err instanceof ApiError && err.status === 409) {
          void refresh();
        }
      } finally {
        setBusyId(null);
      }
    },
    [pending, refresh],
  );

  // Regenerate a new AI reply body for a pending approval, then refresh the row.
  const regenerate = useCallback(
    async (id: string) => {
      setBusyId(id);
      setActionError(null);
      try {
        const updated = await regenerateApproval(id);
        setPending((cur) => cur.map((a) => (a.id === id ? updated : a)));
      } catch (err) {
        setActionError(
          err instanceof ApiError ? err.message : "Failed to regenerate the reply.",
        );
        if (err instanceof ApiError && err.status === 409) void refresh();
      } finally {
        setBusyId(null);
      }
    },
    [refresh],
  );

  // Save inline edits to a pending reply, then update the row in place.
  const saveEdit = useCallback(
    async (id: string, fields: EditApprovalFields) => {
      setBusyId(id);
      setActionError(null);
      try {
        const updated = await editApproval(id, fields);
        setPending((cur) => cur.map((a) => (a.id === id ? updated : a)));
        return true;
      } catch (err) {
        setActionError(
          err instanceof ApiError ? err.message : "Failed to save the edit.",
        );
        if (err instanceof ApiError && err.status === 409) void refresh();
        return false;
      } finally {
        setBusyId(null);
      }
    },
    [refresh],
  );

  // Schedule a pending reply to auto-send at a future time. The approval stays
  // pending (now carrying scheduled_send_at); refresh the pending list on
  // success so the "Scheduled for …" badge shows.
  const schedule = useCallback(
    async (id: string, scheduledSendAtISO: string) => {
      setBusyId(id);
      setActionError(null);
      try {
        await approveSchedule(id, scheduledSendAtISO);
        await refresh();
        return true;
      } catch (err) {
        setActionError(
          err instanceof ApiError ? err.message : "Failed to schedule the reply.",
        );
        if (err instanceof ApiError && err.status === 409) void refresh();
        return false;
      } finally {
        setBusyId(null);
      }
    },
    [refresh],
  );

  // Bulk-clear (reject) every pending reply. Destructive → gated by an inline
  // confirm. The backend keeps the audit trail (items move to rejected).
  const clearAll = useCallback(async () => {
    if (!workspaceId) return;
    setClearing(true);
    setActionError(null);
    try {
      await clearPendingApprovals(workspaceId);
      setConfirmingClear(false);
      await refresh();
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : "Failed to clear pending replies.",
      );
    } finally {
      setClearing(false);
    }
  }, [workspaceId, refresh]);

  // ── Voice: position-aware approval actions ────────────────────────────────
  // The voice session lives in the accessibility widget (which owns the WS),
  // so applyVoiceAction forwards focus_reply/open_edit/open_schedule to us over
  // a window CustomEvent. We keep an imperative handle per pending card (keyed
  // by approval id) so the handler can drive focus/edit/schedule without
  // lifting every card's local UI state up here.
  const cardRefs = useRef<Map<string, PendingCardHandle | null>>(new Map());
  const registerCardRef = useCallback(
    (id: string) => (handle: PendingCardHandle | null) => {
      if (handle) cardRefs.current.set(id, handle);
      else cardRefs.current.delete(id);
    },
    [],
  );
  // Latest pending list for the event handler (avoids stale closures without
  // re-subscribing the listener on every refresh).
  const pendingRef = useRef<Approval[]>(pending);
  useEffect(() => {
    pendingRef.current = pending;
  }, [pending]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onVoiceAction = (event: Event) => {
      const detail = (event as CustomEvent<ApprovalVoiceAction>).detail;
      if (!detail) return;

      // ── Queue-wide actions (no position) ──────────────────────────────────
      if (detail.type === "cancel_edit") {
        // "never mind" — close any open editor/scheduler on EVERY card.
        for (const handle of cardRefs.current.values()) handle?.closeEditors();
        return;
      }
      if (detail.type === "approvals_cleared") {
        // Bulk clear already happened server-side (all pending rejected); just
        // resync so the emptied queue is reflected.
        void refresh();
        return;
      }

      // ── Position-aware actions ────────────────────────────────────────────
      if (typeof detail.position !== "number") return;
      // Resolve 1-based position → the pending approval at index position-1.
      // The pending list is server-ordered created_at DESC (newest first), so
      // position 1 == the topmost/newest card the page renders (never re-sorted).
      const approval = pendingRef.current[detail.position - 1];
      if (!approval) return; // out-of-range: no-op (backend narrates the error)
      const handle = cardRefs.current.get(approval.id);
      if (!handle) return;
      switch (detail.type) {
        case "focus_reply":
          handle.focus();
          return;
        case "open_edit":
          handle.beginEdit();
          return;
        case "open_schedule":
          handle.beginSchedule(detail.when_iso);
          return;
        case "focus_field":
          // Focus a field of the reply's edit form (opens it if needed).
          handle.focusField(detail.field);
          return;
        case "edit_field":
          // Apply a live in-place edit (set or find-and-replace) to a field so
          // the reviewer sees the voice edit reflected in the open editor.
          handle.applyFieldEdit({
            field: detail.field,
            value: detail.value,
            search: detail.search,
            replacement: detail.replacement,
          });
          return;
        case "regenerate_body":
          // Frontend-ready: regenerate the whole reply. Awaiting a backend
          // emitter for `regenerate_body`.
          handle.regenerateBody();
          return;
      }
    };
    window.addEventListener(VOICE_APPROVAL_EVENT, onVoiceAction);
    return () => window.removeEventListener(VOICE_APPROVAL_EVENT, onVoiceAction);
  }, [refresh]);

  if (!workspaceId) {
    return (
      <main className="mx-auto max-w-4xl p-8">
        <PageHeading socketStatus={socketStatus} onReconnect={reconnect} onRefresh={refresh} />
        <Card className="mt-6">
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            Select or create a workspace from the switcher above to view its
            approval queue.
          </CardContent>
        </Card>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-4xl p-8">
      <PageHeading
        socketStatus={socketStatus}
        onReconnect={reconnect}
        onRefresh={refresh}
        refreshing={loading}
      />

      {error && (
        <div
          role="alert"
          className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          {error}
        </div>
      )}
      {actionError && (
        <div
          role="alert"
          className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          {actionError}
        </div>
      )}

      {/* Pending queue */}
      <section className="mt-6" aria-labelledby="pending-heading">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 id="pending-heading" className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Pending review {pending.length > 0 && `(${pending.length})`}
          </h2>
          {pending.length > 0 &&
            (confirmingClear ? (
              <div
                className="flex items-center gap-2"
                role="group"
                aria-label="Confirm clearing all pending replies"
              >
                <span className="text-xs text-muted-foreground">
                  This rejects every pending reply.
                </span>
                <Button
                  size="sm"
                  variant="destructive"
                  onClick={() => void clearAll()}
                  disabled={clearing}
                >
                  {clearing ? "Clearing…" : `Clear all ${pending.length} replies`}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setConfirmingClear(false)}
                  disabled={clearing}
                >
                  Cancel
                </Button>
              </div>
            ) : (
              <Button
                size="sm"
                variant="outline"
                onClick={() => setConfirmingClear(true)}
              >
                Clear all
              </Button>
            ))}
        </div>
        {loading && pending.length === 0 ? (
          <Card>
            <CardContent className="py-10 text-center text-sm text-muted-foreground">
              Loading approvals…
            </CardContent>
          </Card>
        ) : pending.length === 0 ? (
          <Card>
            <CardContent className="py-10 text-center text-sm text-muted-foreground">
              No pending approvals. New high-impact actions will appear here.
            </CardContent>
          </Card>
        ) : (
          <ul className="flex flex-col gap-4">
            {pending.map((approval, i) => (
              <li key={approval.id}>
                <PendingCard
                  // 1-based position matches the voice `position`. The list is
                  // rendered in the EXACT order the server returns it —
                  // created_at DESC (newest first), server-controlled — and is
                  // NEVER re-sorted here. So screen number N == voice position N
                  // == the Nth-newest reply (index N-1); position 1 is the
                  // newest reply at the top.
                  position={i + 1}
                  ref={registerCardRef(approval.id)}
                  approval={approval}
                  busy={busyId === approval.id}
                  onApproveDraft={() => void resolve(approval.id, "approve-draft")}
                  onApproveSend={() => void resolve(approval.id, "approve-send")}
                  onReject={() => void resolve(approval.id, "reject")}
                  onRegenerate={() => void regenerate(approval.id)}
                  onSaveEdit={(fields) => saveEdit(approval.id, fields)}
                  onSchedule={(iso) => schedule(approval.id, iso)}
                />
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Resolved history */}
      <section className="mt-10" aria-labelledby="resolved-heading">
        <h2 id="resolved-heading" className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Resolved history {resolved.length > 0 && `(${resolved.length})`}
        </h2>
        {resolved.length === 0 ? (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              Nothing resolved yet.
            </CardContent>
          </Card>
        ) : (
          <ul className="flex flex-col gap-3">
            {resolved.map((approval) => (
              <li key={approval.id}>
                <ResolvedCard approval={approval} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

/* ── Sub-components ─────────────────────────────────────────────────────── */

function PageHeading({
  socketStatus,
  onReconnect,
  onRefresh,
  refreshing,
}: {
  socketStatus: SocketStatus;
  onReconnect: () => void;
  onRefresh: () => void | Promise<void>;
  refreshing?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold">Approvals Hub</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Review high-impact agent actions before they run.
        </p>
      </div>
      <div className="flex items-center gap-3">
        <LiveIndicator status={socketStatus} onReconnect={onReconnect} />
        <Button
          size="sm"
          variant="outline"
          onClick={() => void onRefresh()}
          disabled={refreshing}
        >
          {refreshing ? "Refreshing…" : "Refresh"}
        </Button>
      </div>
    </div>
  );
}

function LiveIndicator({
  status,
  onReconnect,
}: {
  status: SocketStatus;
  onReconnect: () => void;
}) {
  if (status === "open") {
    return (
      <Badge variant="success" aria-live="polite">
        <span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-white" />
        Live
      </Badge>
    );
  }
  if (status === "connecting") {
    return <Badge variant="secondary">Connecting…</Badge>;
  }
  if (status === "unauthorized") {
    return (
      <div className="flex items-center gap-2">
        <Badge variant="warning">Live updates unauthorized</Badge>
        <Button size="sm" variant="ghost" onClick={onReconnect}>
          Retry
        </Button>
      </div>
    );
  }
  if (status === "closed") {
    return (
      <div className="flex items-center gap-2">
        <Badge variant="warning">Live updates offline</Badge>
        <Button size="sm" variant="ghost" onClick={onReconnect}>
          Retry
        </Button>
      </div>
    );
  }
  // "unavailable" — no token; feature degrades to manual refresh.
  return (
    <Badge
      variant="outline"
      title="Live updates require a WebSocket token, which the browser cannot read from the HttpOnly session cookie. Use Refresh to update the queue."
    >
      Live updates unavailable
    </Badge>
  );
}

/**
 * Imperative surface a pending card exposes to the voice handler so a blind
 * user can drive it by position. Kept minimal so button flows stay local.
 */
interface PendingCardHandle {
  /** Scroll into view, briefly highlight, and move focus to the card. */
  focus: () => void;
  /** Open the inline Edit form and focus its first field (the To field). */
  beginEdit: () => void;
  /**
   * Open the schedule panel and focus the picker; prefill from an ISO instant
   * when supplied (converted to a local `datetime-local` value).
   */
  beginSchedule: (whenIso?: string) => void;
  /**
   * Close any open inline editor AND scheduler on this card (voice
   * `cancel_edit` / "never mind"). Safe to call when nothing is open.
   */
  closeEditors: () => void;
  /**
   * Focus a specific field of the OPEN edit form so the user can dictate into
   * it. Opens the edit form first if it isn't already open (when editable).
   * Frontend-ready for the (not-yet-emitted) `focus_field` voice action.
   */
  focusField: (field: "to" | "subject" | "body") => void;
  /**
   * Regenerate the whole reply (replaces the body). If the edit form is open,
   * the body is refocused after the regenerate resolves. Frontend-ready for the
   * (not-yet-emitted) `regenerate_body` voice action.
   */
  regenerateBody: () => void;
  /**
   * Apply a live in-place edit to a field: opens the editor if needed, then
   * either sets the field to `value` or find-and-replaces `search`→`replacement`
   * (case-insensitive). Reflects a server-side voice edit in the open editor so
   * the reviewer sees it immediately. (Voice `edit_field`.)
   */
  applyFieldEdit: (edit: {
    field: "to" | "subject" | "body";
    value?: string;
    search?: string;
    replacement?: string;
  }) => void;
}

const PendingCard = forwardRef<
  PendingCardHandle,
  {
    approval: Approval;
    position: number;
    busy: boolean;
    onApproveDraft: () => void;
    onApproveSend: () => void;
    onReject: () => void;
    onRegenerate: () => void;
    onSaveEdit: (fields: EditApprovalFields) => Promise<boolean>;
    onSchedule: (scheduledSendAtISO: string) => Promise<boolean>;
  }
>(function PendingCard(
  {
    approval,
    position,
    busy,
    onApproveDraft,
    onApproveSend,
    onReject,
    onRegenerate,
    onSaveEdit,
    onSchedule,
  },
  ref,
) {
  const [editing, setEditing] = useState(false);
  const [scheduling, setScheduling] = useState(false);
  const [scheduleValue, setScheduleValue] = useState("");
  // Imperative handle to the open EditForm so voice focus_field can target a
  // field. A pending focus request (arriving before the form has mounted) is
  // parked here and flushed by an effect once the form + ref exist.
  const editFormRef = useRef<EditFormHandle>(null);
  const pendingFocusFieldRef = useRef<"to" | "subject" | "body" | null>(null);
  // A parked live field-edit (voice `edit_field`) applied once the form mounts,
  // mirroring pendingFocusFieldRef.
  const pendingFieldEditRef = useRef<{
    field: "to" | "subject" | "body";
    value?: string;
    search?: string;
    replacement?: string;
  } | null>(null);
  // Brief highlight ring applied when the voice handler focuses this card.
  const [highlighted, setHighlighted] = useState(false);
  // Ref to the card root so voice `focus_reply` can scroll + move focus to it.
  const cardRef = useRef<HTMLDivElement>(null);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Ref to the native datetime input so a click anywhere in the field can open
  // the picker via showPicker() (not just the tiny built-in calendar glyph).
  const scheduleInputRef = useRef<HTMLInputElement>(null);
  // `min` for the picker = now, as a local-wall-clock datetime-local string
  // (YYYY-MM-DDTHH:mm), so past times are discouraged at the UI level too.
  const minScheduleValue = useMemo(() => {
    const now = new Date();
    now.setSeconds(0, 0);
    const pad = (n: number) => String(n).padStart(2, "0");
    return (
      `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}` +
      `T${pad(now.getHours())}:${pad(now.getMinutes())}`
    );
  }, [scheduling]);
  // The decoded email (when this is a Gmail reply) prefills the edit form and
  // renders the readable preview when not editing.
  const email = useMemo(
    () => parseEmailFromArguments(approval.arguments),
    [approval.arguments],
  );
  const canEdit = Boolean(email);

  // Validate the inline datetime-local value: it must parse and be in the
  // future. `datetime-local` yields a local wall-clock string (no tz); the
  // Date constructor interprets it in the browser's local zone, and we convert
  // to a UTC ISO string before sending.
  const scheduleIso = useMemo(() => {
    if (!scheduleValue) return null;
    const d = new Date(scheduleValue);
    if (Number.isNaN(d.getTime())) return null;
    return d.toISOString();
  }, [scheduleValue]);
  const scheduleInFuture = scheduleIso !== null && new Date(scheduleIso).getTime() > Date.now();

  // Open the picker on the native input once the schedule panel has rendered.
  // showPicker() must run after the input exists in the DOM, so voice
  // beginSchedule() flips `scheduling` and defers the open to this effect.
  const wantPickerOpenRef = useRef(false);
  useEffect(() => {
    if (!scheduling || !wantPickerOpenRef.current) return;
    wantPickerOpenRef.current = false;
    const el = scheduleInputRef.current;
    if (!el) return;
    // Focus first so a screen-reader user lands on the field; then try to pop
    // the native picker (may be a no-op without a user gesture — focus alone
    // still lets the user proceed).
    try {
      el.focus();
    } catch {
      /* focus can throw in odd states — ignore */
    }
    if (typeof el.showPicker === "function") {
      try {
        el.showPicker();
      } catch {
        /* requires user activation in some browsers — focus is enough */
      }
    }
  }, [scheduling, scheduleValue]);

  // Clear the highlight timer on unmount.
  useEffect(() => {
    return () => {
      if (highlightTimer.current !== null) clearTimeout(highlightTimer.current);
    };
  }, []);

  // Flush a parked focus_field request once the edit form has mounted. A voice
  // `focus_field` that opens the form (setEditing(true)) can't focus the field
  // synchronously because EditForm mounts on the next render — so we stash the
  // target and focus it here after `editing` flips on.
  useEffect(() => {
    if (!editing) return;
    const field = pendingFocusFieldRef.current;
    if (field) {
      pendingFocusFieldRef.current = null;
      editFormRef.current?.focusField(field);
    }
    // Flush a parked live field-edit once the form (and its editor) exist. A
    // small timeout lets TipTap finish mounting + registering before we apply.
    const edit = pendingFieldEditRef.current;
    if (edit) {
      pendingFieldEditRef.current = null;
      const apply = () => editFormRef.current?.applyFieldEdit(edit);
      // Try immediately; if the editor isn't ready, retry on the next frame.
      apply();
      const t = setTimeout(apply, 50);
      return () => clearTimeout(t);
    }
  }, [editing]);

  useImperativeHandle(
    ref,
    () => ({
      focus: () => {
        const el = cardRef.current;
        if (!el) return;
        try {
          el.scrollIntoView({ behavior: "smooth", block: "center" });
        } catch {
          el.scrollIntoView();
        }
        try {
          el.focus();
        } catch {
          /* ignore */
        }
        // Brief highlight ring so a sighted user sees which card was targeted.
        setHighlighted(true);
        if (highlightTimer.current !== null) clearTimeout(highlightTimer.current);
        highlightTimer.current = setTimeout(() => setHighlighted(false), 2000);
      },
      beginEdit: () => {
        if (!canEdit) return; // non-editable (not a decodable email) — no-op
        setEditing(true);
        // The edit form focuses its first field (To) on mount (see EditForm).
      },
      beginSchedule: (whenIso?: string) => {
        const local = isoToDatetimeLocal(whenIso);
        if (local) setScheduleValue(local);
        wantPickerOpenRef.current = true;
        setScheduling(true);
      },
      closeEditors: () => {
        // "never mind" — close whatever is open. Safe if nothing is.
        setEditing(false);
        setScheduling(false);
        wantPickerOpenRef.current = false;
      },
      focusField: (field) => {
        if (!canEdit) return; // non-editable — nothing to focus
        if (editing) {
          // Form already open: focus immediately.
          editFormRef.current?.focusField(field);
        } else {
          // Open the form first; the effect above focuses once it mounts.
          pendingFocusFieldRef.current = field;
          setEditing(true);
        }
      },
      regenerateBody: () => {
        // Regenerate the whole reply (replaces the body). If the edit form is
        // open, refocus the body input so any follow-up dictation / manual
        // tweak lands there. (Regenerate replaces the approval's stored body;
        // the readable preview reflects it once the row refreshes.)
        onRegenerate();
        if (editing) editFormRef.current?.focusField("body");
      },
      applyFieldEdit: (edit) => {
        if (!canEdit) return; // non-editable — nothing to edit
        if (editing) {
          editFormRef.current?.applyFieldEdit(edit);
        } else {
          // Open the form first; the effect above applies once it mounts.
          pendingFieldEditRef.current = edit;
          setEditing(true);
        }
      },
    }),
    [canEdit, editing, onRegenerate],
  );

  return (
    <Card
      ref={cardRef}
      tabIndex={-1}
      aria-label={`Reply ${position}`}
      className={
        highlighted
          ? "outline-none ring-2 ring-primary ring-offset-2 transition-shadow"
          : "outline-none transition-shadow"
      }
    >
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div className="flex items-start gap-3">
          {/* Position badge: the user navigates pending replies by this number
              over voice ("edit reply 2", "schedule reply 3"). It matches the
              rendered order, which is server-controlled created_at DESC (newest
              first) — so position 1 is the newest reply at the top. Only pending
              replies are numbered — resolved history stays unnumbered. */}
          <span
            aria-label={`Reply ${position}`}
            className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-2 border-primary bg-primary/10 text-sm font-semibold tabular-nums text-primary"
          >
            <span aria-hidden="true">{position}</span>
          </span>
          <div>
          <CardTitle className="font-mono text-base">{approval.tool_name}</CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">
            Triggered by <span className="font-mono">{shortId(approval.triggered_by_user_id)}</span>
            {" · "}
            {formatTimestamp(approval.created_at)}
            {approval.agent_session_id && (
              <>
                {" · session "}
                <span className="font-mono">{shortId(approval.agent_session_id)}</span>
              </>
            )}
          </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={onRegenerate}
            disabled={busy}
            title="Ask the AI to write a different reply"
          >
            {busy ? (
              <span className="inline-flex items-center gap-1.5">
                <Spinner /> Working…
              </span>
            ) : (
              "Regenerate"
            )}
          </Button>
          {canEdit && !editing && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setEditing(true)}
              disabled={busy}
            >
              Edit
            </Button>
          )}
          {approval.scheduled_send_at && (
            <Badge variant="secondary" title={approval.scheduled_send_at}>
              Scheduled for {formatTimestamp(approval.scheduled_send_at)}
            </Badge>
          )}
          <Badge variant="warning">pending</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {editing && email ? (
          <EditForm
            ref={editFormRef}
            email={email}
            busy={busy}
            onCancel={() => setEditing(false)}
            onSave={async (fields) => {
              const ok = await onSaveEdit(fields);
              if (ok) setEditing(false);
            }}
          />
        ) : (
          <ArgumentsPreview value={approval.arguments} />
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={onApproveDraft} disabled={busy}>
            {busy ? "Working…" : "Approve and Save to Draft"}
          </Button>
          <Button size="sm" variant="outline" onClick={onApproveSend} disabled={busy}>
            {busy ? "Working…" : "Approve and Send"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setScheduling((s) => !s)}
            disabled={busy}
            aria-expanded={scheduling}
          >
            Approve and Schedule
          </Button>
          <Button size="sm" variant="destructive" onClick={onReject} disabled={busy}>
            Reject
          </Button>
        </div>
        {scheduling && (
          <div className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/40 p-3">
            <span className="text-xs font-medium text-muted-foreground">
              Send at
            </span>
            <div
              className="group flex cursor-pointer items-center gap-2 rounded-md border bg-background px-3 py-1.5 text-sm transition-colors hover:border-primary/60 focus-within:border-primary"
              onClick={() => {
                if (busy) return;
                const el = scheduleInputRef.current;
                if (!el) return;
                // Open the native picker on a click ANYWHERE in the field, not
                // just on the (hard-to-see) built-in calendar indicator.
                if (typeof el.showPicker === "function") {
                  try {
                    el.showPicker();
                    return;
                  } catch {
                    /* showPicker can throw if not user-activated; fall through */
                  }
                }
                el.focus();
              }}
            >
              <svg
                aria-hidden="true"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                className="h-4 w-4 shrink-0 text-muted-foreground group-hover:text-primary"
              >
                <rect x="3" y="4" width="18" height="18" rx="2" />
                <path d="M16 2v4M8 2v4M3 10h18" />
              </svg>
              <input
                ref={scheduleInputRef}
                type="datetime-local"
                aria-label="Send at date and time"
                className="w-[13.5rem] cursor-pointer bg-transparent text-sm outline-none [color-scheme:dark]"
                value={scheduleValue}
                min={minScheduleValue}
                onChange={(e) => setScheduleValue(e.target.value)}
                disabled={busy}
              />
            </div>
            <Button
              size="sm"
              onClick={async () => {
                if (!scheduleIso) return;
                const ok = await onSchedule(scheduleIso);
                if (ok) {
                  setScheduling(false);
                  setScheduleValue("");
                }
              }}
              disabled={busy || !scheduleInFuture}
            >
              {busy ? "Scheduling…" : "Schedule"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setScheduling(false);
                setScheduleValue("");
              }}
              disabled={busy}
            >
              Cancel
            </Button>
            {scheduleValue && !scheduleInFuture && (
              <span className="text-xs text-destructive">
                Pick a time in the future.
              </span>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
});

/** A tiny inline spinner for busy states. */
function Spinner() {
  return (
    <span
      aria-hidden
      className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-r-transparent"
    />
  );
}

/**
 * Imperative surface the EditForm exposes to its parent card so voice
 * `focus_field` / `regenerate_body` can target a specific field.
 */
interface EditFormHandle {
  /** Focus the To / Subject / Body input of this open edit form. */
  focusField: (field: "to" | "subject" | "body") => void;
  /**
   * Apply a live in-place edit to a field of this open form: set the whole
   * field to `value`, or find-and-replace `search`→`replacement`
   * (case-insensitive). The body routes through the TipTap editor; to/subject
   * update their controlled state.
   */
  applyFieldEdit: (edit: {
    field: "to" | "subject" | "body";
    value?: string;
    search?: string;
    replacement?: string;
  }) => void;
}

/**
 * Inline edit form for a Gmail reply: To, Subject, Body.
 *
 * ── Voice dictation wiring ────────────────────────────────────────────────
 * Each field carries a `data-voice-field` attribute so the backend `type`
 * action (`{"type":"type","field":"reply_to","text":"…"}`) can dictate into it
 * via `applyVoiceAction`'s `setNativeValue` (which uses React's native value
 * setter + fires an input event). The inputs are controlled, so those synthetic
 * input events drive `onChange` and keep local state in sync — meaning dictated
 * text is what gets saved. As a belt-and-braces guard against any environment
 * where a programmatic value+event is ignored by React's controlled input, we
 * ALSO read the live DOM value off each field ref at submit time and prefer it,
 * so the saved edit always reflects what's actually in the box.
 */
const EditForm = forwardRef<
  EditFormHandle,
  {
    email: ParsedEmail;
    busy: boolean;
    onCancel: () => void;
    onSave: (fields: EditApprovalFields) => void | Promise<void>;
  }
>(function EditForm({ email, busy, onCancel, onSave }, ref) {
  const [to, setTo] = useState(email.to ?? "");
  const [subject, setSubject] = useState(email.subject ?? "");
  const [body, setBody] = useState(email.body ?? "");
  // Field refs: the To ref doubles as the "first field" focus target on mount
  // (voice `open_edit`), and to/subject back the focus_field handle + the
  // read-from-DOM-on-submit guard for dictated text. The BODY is now a TipTap
  // rich-text editor (voice-programmable), so its ref is the editor handle and
  // its value is read via `bodyEditorRef.current.getText()` at save time.
  const toRef = useRef<HTMLInputElement>(null);
  const subjectRef = useRef<HTMLInputElement>(null);
  const bodyEditorRef = useRef<RichTextEditorHandle | null>(null);

  useEffect(() => {
    // Move focus to the To field when the form appears (voice or click) so
    // `open_edit` lands the caret on the first field.
    toRef.current?.focus();
  }, []);

  useImperativeHandle(
    ref,
    () => ({
      focusField: (field) => {
        try {
          if (field === "body") {
            bodyEditorRef.current?.focus();
          } else {
            (field === "to" ? toRef.current : subjectRef.current)?.focus();
          }
        } catch {
          /* focus can throw in odd states — ignore */
        }
      },
      applyFieldEdit: ({ field, value, search, replacement }) => {
        if (field === "body") {
          const editor = bodyEditorRef.current;
          if (!editor) return;
          if (typeof value === "string") {
            editor.setText(value);
          } else if (search) {
            editor.replaceText(search, replacement ?? "");
          }
          editor.focus();
          return;
        }
        // to / subject: controlled inputs — set state directly.
        const setter = field === "to" ? setTo : setSubject;
        if (typeof value === "string") {
          setter(value);
        } else if (search) {
          const cur = field === "to" ? to : subject;
          const [next] = replaceTextCI(cur, search, replacement ?? "");
          setter(next);
        }
        (field === "to" ? toRef.current : subjectRef.current)?.focus();
      },
    }),
    [to, subject],
  );

  const invalid = !to.trim() || !body.trim();

  const handleSave = () => {
    // Prefer the live values so dictated / programmatically-edited text is
    // always captured. To/Subject read from the native input DOM; the Body
    // reads from the TipTap editor's plain text.
    const nextTo = toRef.current?.value ?? to;
    const nextSubject = subjectRef.current?.value ?? subject;
    const nextBody = bodyEditorRef.current?.getText() ?? body;
    void onSave({ to: nextTo, subject: nextSubject, body: nextBody });
  };

  return (
    <div className="space-y-3 rounded-md border bg-muted/40 p-4">
      <label className="block space-y-1">
        <span className="text-xs font-medium text-muted-foreground">To</span>
        <input
          ref={toRef}
          data-voice-field="reply_to"
          className="w-full rounded-md border bg-background px-3 py-2 text-sm"
          value={to}
          onChange={(e) => setTo(e.target.value)}
          disabled={busy}
        />
      </label>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-muted-foreground">Subject</span>
        <input
          ref={subjectRef}
          data-voice-field="reply_subject"
          className="w-full rounded-md border bg-background px-3 py-2 text-sm"
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          disabled={busy}
        />
      </label>
      <label className="block space-y-1">
        <span className="text-xs font-medium text-muted-foreground">Body</span>
        <RichTextEditor
          ref={bodyEditorRef}
          value={body}
          onChange={setBody}
          disabled={busy}
          voiceField="reply_body"
          ariaLabel="Reply body"
        />
      </label>
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={handleSave} disabled={busy || invalid}>
          {busy ? "Saving…" : "Save"}
        </Button>
        <Button size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        {invalid && (
          <span className="text-xs text-destructive">
            Recipient and body are required.
          </span>
        )}
      </div>
    </div>
  );
});

function ResolvedCard({ approval }: { approval: Approval }) {
  const approved = approval.status === "approved";
  return (
    <Card className="opacity-90">
      <CardHeader className="flex flex-row items-center justify-between gap-4 space-y-0 py-4">
        <div>
          <span className="font-mono text-sm font-medium">{approval.tool_name}</span>
          <p className="mt-1 text-xs text-muted-foreground">
            Reviewed by{" "}
            <span className="font-mono">{shortId(approval.reviewed_by_user_id)}</span>
            {" · "}
            {formatTimestamp(approval.created_at)}
          </p>
        </div>
        <Badge variant={approved ? "success" : "destructive"}>{approval.status}</Badge>
      </CardHeader>
    </Card>
  );
}

/** A parsed, human-readable email extracted from a proposed action. */
interface ParsedEmail {
  subject: string | null;
  from: string | null;
  to: string | null;
  cc: string | null;
  body: string;
  action: string; // e.g. "Create draft" / "Send email"
  /**
   * Best-effort context about the ORIGINAL inbound email this is a reply to.
   *
   * Data limitation: the backend stores the OUTGOING reply (as an RFC822 raw
   * message) plus opaque `source_message_id` / `thread_id` in the approval
   * arguments — it does NOT persist the original sender/subject/snippet. So the
   * best we can honestly surface is the thread the reply targets: the reply's
   * `To` (the original sender we're replying to), the reply's `Subject`
   * (typically "Re: <original subject>"), and the `In-Reply-To` message id.
   * We never fabricate original content. If/when the arguments carry the
   * original email, extend `parseOriginalEmail` below to prefer it.
   */
  inReplyTo: string | null;
  threadId: string | null;
  sourceMessageId: string | null;
}

/** Decode a base64url (RFC 4648) string to a UTF-8 string in the browser. */
function decodeBase64Url(input: string): string {
  // base64url -> base64, then pad.
  let b64 = input.replace(/-/g, "+").replace(/_/g, "/");
  const pad = b64.length % 4;
  if (pad) b64 += "=".repeat(4 - pad);
  const binary = atob(b64);
  // Turn the binary string into UTF-8 text.
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  try {
    return new TextDecoder("utf-8").decode(bytes);
  } catch {
    return binary;
  }
}

/** Decode base64 (standard alphabet) bytes to a UTF-8 string. */
function decodeBase64Utf8(input: string): string {
  let b64 = input.replace(/\s+/g, "");
  const pad = b64.length % 4;
  if (pad) b64 += "=".repeat(4 - pad);
  const binary = atob(b64);
  const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
  try {
    return new TextDecoder("utf-8").decode(bytes);
  } catch {
    return binary;
  }
}

/**
 * Decode a quoted-printable string (RFC 2045):
 *  - `=\r?\n` soft line breaks are removed (this is what shows as a trailing "="),
 *  - `=XX` hex escapes become their byte, decoded as UTF-8,
 *  - everything else is passed through.
 */
function decodeQuotedPrintable(text: string): string {
  // Drop soft line breaks first so multi-byte "=XX=XX" sequences stay intact.
  const unwrapped = text.replace(/=\r?\n/g, "");
  const bytes: number[] = [];
  for (let i = 0; i < unwrapped.length; i++) {
    const ch = unwrapped[i];
    if (ch === "=" && i + 2 < unwrapped.length) {
      const hex = unwrapped.slice(i + 1, i + 3);
      if (/^[0-9A-Fa-f]{2}$/.test(hex)) {
        bytes.push(parseInt(hex, 16));
        i += 2;
        continue;
      }
    }
    // Push the char's UTF-8 bytes (usually ASCII here).
    const code = ch.charCodeAt(0);
    if (code < 0x80) {
      bytes.push(code);
    } else {
      for (const b of new TextEncoder().encode(ch)) bytes.push(b);
    }
  }
  try {
    return new TextDecoder("utf-8").decode(Uint8Array.from(bytes));
  } catch {
    return unwrapped;
  }
}

/**
 * Decode RFC 2047 encoded-word headers like `=?utf-8?B?...?=` (base64) or
 * `=?utf-8?Q?...?=` (quoted-printable). Handles multiple adjacent encoded
 * words, strips the whitespace between them per spec, and passes plain text
 * through unchanged. This is what makes a non-ASCII Subject/From readable.
 */
function decodeEncodedWord(value: string | null): string | null {
  if (!value) return value;
  const re = /=\?([^?]+)\?([BbQq])\?([^?]*)\?=/g;
  // Remove whitespace separating consecutive encoded words (RFC 2047 §6.2).
  const collapsed = value.replace(/(\?=)\s+(=\?)/g, "$1$2");
  return collapsed.replace(re, (_m, _charset: string, enc: string, data: string) => {
    try {
      if (enc.toUpperCase() === "B") {
        return decodeBase64Utf8(data);
      }
      // "Q" encoding: like QP but "_" means space.
      return decodeQuotedPrintable(data.replace(/_/g, " "));
    } catch {
      return _m;
    }
  });
}

/** Pull a header value (case-insensitive) from an RFC822 header block. */
function rawHeader(headers: string, name: string): string | null {
  // Support folded headers (continuation lines begin with whitespace).
  const re = new RegExp(`^${name}\\s*:\\s*(.*(?:\\r?\\n[ \\t].*)*)$`, "im");
  const m = headers.match(re);
  if (!m) return null;
  return m[1].replace(/\r?\n[ \t]+/g, " ").trim();
}

/** Pull a header value and decode any RFC 2047 encoded-words in it. */
function header(headers: string, name: string): string | null {
  return decodeEncodedWord(rawHeader(headers, name));
}

/**
 * If the proposed action is a Gmail draft/send carrying a base64url RFC822
 * message (``body.message.raw`` for drafts, ``body.raw`` for sends), decode it
 * into a readable email. Returns null when the shape doesn't match so the caller
 * falls back to the raw JSON view.
 */
function parseEmailFromArguments(value: unknown): ParsedEmail | null {
  if (!value || typeof value !== "object") return null;
  const args = value as Record<string, unknown>;
  const path = typeof args.path === "string" ? args.path : "";
  const body = (args.body ?? null) as Record<string, unknown> | null;
  if (!body || typeof body !== "object") return null;

  // Locate the base64url raw message.
  const message = (body.message ?? null) as Record<string, unknown> | null;
  const raw =
    (message && typeof message.raw === "string" && message.raw) ||
    (typeof body.raw === "string" && body.raw) ||
    null;
  if (!raw) return null;

  let decoded: string;
  try {
    decoded = decodeBase64Url(raw);
  } catch {
    return null;
  }

  // Split headers from body at the first blank line.
  const sepIdx = decoded.search(/\r?\n\r?\n/);
  const headerBlock = sepIdx >= 0 ? decoded.slice(0, sepIdx) : decoded;
  let emailBody = sepIdx >= 0 ? decoded.slice(sepIdx).replace(/^\r?\n\r?\n/, "") : "";

  // Decode the body per its Content-Transfer-Encoding so it renders cleanly
  // regardless of how it was produced (base64 for new messages, quoted-printable
  // for older ones). Un-wraps QP soft line breaks ("=\r?\n" -> "").
  const cte = (rawHeader(headerBlock, "Content-Transfer-Encoding") || "").toLowerCase();
  if (/base64/.test(cte)) {
    try {
      emailBody = decodeBase64Utf8(emailBody);
    } catch {
      /* leave as-is */
    }
  } else if (/quoted-printable/.test(cte)) {
    try {
      emailBody = decodeQuotedPrintable(emailBody);
    } catch {
      /* leave as-is */
    }
  }

  const action = /drafts/.test(path)
    ? "Create draft reply"
    : /messages\/send|\/send/.test(path)
      ? "Send email"
      : "Email";

  // Original-thread context (see the data-limitation note on ParsedEmail). The
  // reply's In-Reply-To header threads it to the original message; source
  // ids come from the gated arguments alongside the body (not inside the raw
  // message). These are the only original-email signals actually stored.
  const sourceMessageId =
    typeof args.source_message_id === "string" ? args.source_message_id : null;
  const threadId =
    typeof args.thread_id === "string"
      ? args.thread_id
      : message && typeof message.threadId === "string"
        ? message.threadId
        : null;

  return {
    subject: header(headerBlock, "Subject"),
    from: header(headerBlock, "From"),
    to: header(headerBlock, "To"),
    cc: header(headerBlock, "Cc"),
    body: emailBody.trim(),
    action,
    inReplyTo: header(headerBlock, "In-Reply-To"),
    threadId,
    sourceMessageId,
  };
}

/** A labeled row in the email preview (hidden when empty). */
function EmailField({ label, value }: { label: string; value: string | null }) {
  if (!value) return null;
  return (
    <div className="flex gap-2 text-sm">
      <span className="w-16 shrink-0 font-medium text-muted-foreground">{label}</span>
      <span className="min-w-0 break-words">{value}</span>
    </div>
  );
}

/** Render a decoded email as a readable preview, with raw JSON behind a toggle. */
function EmailPreview({ email, raw }: { email: ParsedEmail; raw: unknown }) {
  const rawText = useMemo(() => {
    try {
      return JSON.stringify(raw, null, 2);
    } catch {
      return String(raw);
    }
  }, [raw]);

  // Show the "In reply to" context when this is a threaded reply. See the
  // data-limitation note on ParsedEmail: we only have the thread the reply
  // targets (original sender = the reply's To; original subject ≈ the reply's
  // Subject sans "Re:"), plus the In-Reply-To / thread ids — the original email
  // body/snippet is not stored, so we never fabricate it.
  const isReply =
    Boolean(email.inReplyTo) ||
    Boolean(email.threadId) ||
    Boolean(email.sourceMessageId);
  const originalSender = email.to; // the person we're replying TO = original sender
  const originalSubject = email.subject
    ? email.subject.replace(/^\s*re:\s*/i, "").trim() || email.subject
    : null;

  return (
    <div className="space-y-3">
      {isReply && (
        <div
          className="space-y-1 rounded-md border border-dashed bg-muted/30 p-3"
          aria-label="Original email this reply responds to"
        >
          <p className="text-xs font-medium text-muted-foreground">In reply to</p>
          <EmailField label="Sender" value={originalSender} />
          <EmailField label="Subject" value={originalSubject} />
          <EmailField label="Thread" value={email.threadId} />
          <EmailField label="Msg id" value={email.inReplyTo ?? email.sourceMessageId} />
          {/* The original message body/snippet isn't stored in the approval
              arguments, so we surface only the thread it targets. */}
          <p className="pt-0.5 text-[11px] italic text-muted-foreground">
            The original message body isn&apos;t stored with this request; only
            the thread it replies to is shown.
          </p>
        </div>
      )}
      <p className="text-xs font-medium text-muted-foreground">
        Proposed action · {email.action}
      </p>
      <div className="space-y-2 rounded-md border bg-muted/40 p-4">
        <div className="space-y-1 border-b pb-2">
          <EmailField label="From" value={email.from} />
          <EmailField label="To" value={email.to} />
          <EmailField label="Cc" value={email.cc} />
          <EmailField label="Subject" value={email.subject ?? "(no subject)"} />
        </div>
        <div className="whitespace-pre-wrap break-words text-sm leading-relaxed">
          {email.body || (
            <span className="italic text-muted-foreground">(empty message body)</span>
          )}
        </div>
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
          View technical details
        </summary>
        <pre className="mt-2 max-h-64 overflow-auto rounded-md border bg-muted/40 p-3 leading-relaxed">
          <code>{rawText}</code>
        </pre>
      </details>
    </div>
  );
}

/** Render the proposed action: a human-readable email when possible, else JSON. */
function ArgumentsPreview({ value }: { value: unknown }) {
  const email = useMemo(() => parseEmailFromArguments(value), [value]);

  const text = useMemo(() => {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }, [value]);

  if (value === null || value === undefined || text === "{}" || text === "null") {
    return (
      <p className="text-xs italic text-muted-foreground">No arguments provided.</p>
    );
  }

  if (email) {
    return <EmailPreview email={email} raw={value} />;
  }

  return (
    <div>
      <p className="mb-1 text-xs font-medium text-muted-foreground">Proposed action</p>
      <pre className="max-h-64 overflow-auto rounded-md border bg-muted/40 p-3 text-xs leading-relaxed">
        <code>{text}</code>
      </pre>
    </div>
  );
}
