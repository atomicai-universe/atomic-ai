"use client";

/**
 * Integrations page (task 17.3, Req 7.3; BUILD.md logo-grid UX).
 *
 * Core lifecycle actions (unchanged backend contract):
 *   - Connect an integration            -> POST   /api/v1/integrations
 *   - Toggle Personal/Shared sharing    -> PATCH  /api/v1/integrations/{id}/sharing
 *   - Disconnect an integration         -> DELETE /api/v1/integrations/{id}
 *
 * BUILD.md UX: instead of typing a provider name, the user picks a category and
 * clicks a provider logo tile. That selection prefills the connect form (the
 * provider slug is what the backend stores). After at least one integration is
 * connected, a "next step" card nudges the user toward the Rules page so they
 * are not confused about what to do next.
 *
 * LIST behavior: the page hydrates the connected-integrations list from
 * GET /api/v1/integrations?workspace_id=... on load and on workspace switch,
 * so connections persist across refreshes (the DB is the source of truth).
 *
 * Secret hygiene: access/refresh tokens are write-only; responses never contain
 * tokens and none are stored or displayed back (Req 6.4).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import { API_BASE_URL, ApiError, api } from "@/lib/api";
import { getActiveWorkspaceId } from "@/lib/workspace";
import { filterOAuthFields, isOAuthFamily } from "@/lib/oauth-providers";
import { ACTIVE_WORKSPACE_EVENT } from "@/components/workspace/workspace-switcher";
import {
  CATEGORIES,
  CATEGORY_META,
  PROVIDER_CATALOG,
  type Category,
  type Provider,
} from "@/lib/providers";
import { Badge } from "@/components/ui/badge";
import { ProviderLogo } from "@/components/integrations/provider-logo";
import { TriggerBadge } from "@/components/integrations/trigger-badge";
import { ProviderGuide } from "@/components/integrations/provider-guide";
import { WebhookStatus, type WebhookInfo } from "@/components/integrations/webhook-status";
import {
  type CredentialFieldSpec,
  type CredentialSpec,
  GENERIC_FIELDS,
  fetchCredentialSpec,
} from "@/lib/credential-spec";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { buttonClasses, cn } from "@/lib/utils";

/** Token-free integration view returned by the backend (never includes tokens). */
interface IntegrationView {
  id: string;
  workspace_id: string;
  category: Category;
  provider_name: string;
  is_shared_with_workspace: boolean;
  status: string;
  webhook?: WebhookInfo | null;
  /** True once the OAuth authorize flow completed and a refresh token is stored. */
  authorized?: boolean;
}

export default function IntegrationsPage() {
  const [integrations, setIntegrations] = useState<IntegrationView[]>([]);

  // Selection + connect form state.
  const [category, setCategory] = useState<Category>("email");
  const [selectedProvider, setSelectedProvider] = useState<Provider | null>(null);
  const [sharedWithWorkspace, setSharedWithWorkspace] = useState(false);

  // Per-provider credential spec (fetched once) + the current field values.
  const [spec, setSpec] = useState<CredentialSpec>({});
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});

  useEffect(() => {
    void fetchCredentialSpec().then(setSpec).catch(() => setSpec({}));
  }, []);

  // Hydrate the connected-integrations list from the backend so it survives a
  // page refresh (the list is the source of truth, not local session state).
  const loadIntegrations = useCallback(async () => {
    const workspaceId = getActiveWorkspaceId();
    if (!workspaceId) {
      setIntegrations([]);
      return;
    }
    try {
      const res = await api.get<{ integrations: IntegrationView[] }>(
        `/api/v1/integrations?workspace_id=${encodeURIComponent(workspaceId)}`,
      );
      setIntegrations(res.integrations ?? []);
    } catch {
      // Non-fatal: leave the list as-is if the fetch fails (e.g. no workspace).
    }
  }, []);

  useEffect(() => {
    void loadIntegrations();
    const onSwitch = () => void loadIntegrations();
    window.addEventListener(ACTIVE_WORKSPACE_EVENT, onSwitch);
    return () => window.removeEventListener(ACTIVE_WORKSPACE_EVENT, onSwitch);
  }, [loadIntegrations]);

  // Returning from the provider authorize flow: the backend callback redirects
  // here with ?oauth=success|error (Req 12.4). Reload the list from the API so
  // the freshly-authorized integration's status/pill reflects the new state,
  // then strip the query params so a refresh doesn't re-trigger the banner.
  const [oauthNotice, setOauthNotice] = useState<"success" | "error" | null>(null);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get("oauth");
    if (outcome !== "success" && outcome !== "error") return;

    setOauthNotice(outcome);
    if (outcome === "error") {
      const reason = params.get("reason");
      setError(
        reason
          ? `Authorization failed (${reason}). Please try authorizing again.`
          : "Authorization failed. Please try authorizing again.",
      );
    }
    void loadIntegrations();

    // Clean the URL so a reload doesn't replay the notice.
    params.delete("oauth");
    params.delete("reason");
    params.delete("integration");
    const query = params.toString();
    const cleaned = `${window.location.pathname}${query ? `?${query}` : ""}`;
    window.history.replaceState(null, "", cleaned);
  }, [loadIntegrations]);

  const fields: CredentialFieldSpec[] = useMemo(() => {
    if (!selectedProvider) return [];
    const base = spec[selectedProvider.name] ?? GENERIC_FIELDS;
    // OAuth-family providers obtain refresh/access tokens through the in-app
    // authorize flow, so hide those manual inputs while keeping the
    // client_id/client_secret the server-side exchange needs (Req 12.1/12.2).
    return filterOAuthFields(selectedProvider.name, base);
  }, [selectedProvider, spec]);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  function reportError(err: unknown, fallback: string) {
    if (err instanceof ApiError || err instanceof Error) {
      setError(err.message);
    } else {
      setError(fallback);
    }
  }

  function chooseProvider(provider: Provider) {
    setSelectedProvider(provider);
    setFieldValues({});
    setError(null);
    // Focus the first credential field so the flow stays click-then-paste.
    requestAnimationFrame(() => {
      const first = (spec[provider.name] ?? GENERIC_FIELDS)[0];
      if (first) document.getElementById(`cred-${first.name}`)?.focus();
    });
  }

  function setField(name: string, value: string) {
    setFieldValues((prev) => ({ ...prev, [name]: value }));
  }

  async function handleConnect(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    const workspaceId = getActiveWorkspaceId();
    if (!workspaceId) {
      setError("No active workspace selected. Create or select a workspace first.");
      return;
    }
    if (!selectedProvider) {
      setError("Pick a provider from the grid above first.");
      return;
    }

    // Split values into secret credentials vs. non-secret config, per the spec.
    const credentials: Record<string, string> = {};
    const config: Record<string, string> = {};
    for (const f of fields) {
      const v = (fieldValues[f.name] ?? "").trim();
      if (!v) continue;
      if (f.secret) credentials[f.name] = v;
      else config[f.name] = v;
    }

    // Client-side required-field check (backend re-validates).
    const missing = fields
      .filter((f) => f.required)
      .filter((f) => !(f.secret ? credentials[f.name] : config[f.name]))
      .map((f) => f.label);
    if (missing.length > 0) {
      setError(`Please fill in: ${missing.join(", ")}.`);
      return;
    }

    setSubmitting(true);
    try {
      const view = await api.post<IntegrationView>("/api/v1/integrations", {
        workspace_id: workspaceId,
        category,
        provider_name: selectedProvider.name,
        credentials,
        config: Object.keys(config).length ? config : undefined,
        is_shared_with_workspace: sharedWithWorkspace,
      });
      setIntegrations((prev) => [...prev, view]);
      // Reset write-only credential fields (never keep secrets around).
      setFieldValues({});
      setSharedWithWorkspace(false);
      setSelectedProvider(null);
    } catch (err) {
      reportError(err, "Failed to connect integration.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleToggleSharing(integration: IntegrationView, shared: boolean) {
    setError(null);
    const workspaceId = getActiveWorkspaceId() ?? integration.workspace_id;
    setBusyId(integration.id);
    try {
      const updated = await api.patch<IntegrationView>(
        `/api/v1/integrations/${integration.id}/sharing`,
        { workspace_id: workspaceId, shared },
      );
      setIntegrations((prev) => prev.map((i) => (i.id === updated.id ? updated : i)));
    } catch (err) {
      reportError(err, "Failed to update sharing.");
    } finally {
      setBusyId(null);
    }
  }

  // Begin the in-app OAuth authorize flow for an existing OAuth-family row
  // (Req 12.3). This is a full-page browser navigation (not a fetch) because
  // the backend responds with a 302 to the provider consent screen and the
  // session cookie must ride along; the provider later redirects back here.
  function handleAuthorize(integration: IntegrationView) {
    window.location.href = `${API_BASE_URL}/api/v1/integrations/${integration.id}/oauth/authorize`;
  }

  async function handleDisconnect(integration: IntegrationView) {
    setError(null);
    const workspaceId = getActiveWorkspaceId() ?? integration.workspace_id;
    setBusyId(integration.id);
    try {
      await api.del(`/api/v1/integrations/${integration.id}`, {
        workspace_id: workspaceId,
      });
      setIntegrations((prev) => prev.filter((i) => i.id !== integration.id));
    } catch (err) {
      reportError(err, "Failed to disconnect integration.");
    } finally {
      setBusyId(null);
    }
  }

  const activeMeta = CATEGORY_META[category];
  const providers = PROVIDER_CATALOG[category];

  return (
    <main className="mx-auto max-w-5xl space-y-8 p-8">
      <header>
        <h1 className="text-2xl font-semibold">Integrations</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Pick a category, click the provider you want to automate, then enter the
          exact credentials that provider needs. Everything secret is encrypted
          at rest. Choose whether the connection is personal or shared.
        </p>
      </header>

      {error ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          {error}
        </div>
      ) : null}

      {oauthNotice === "success" ? (
        <div
          role="status"
          className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-600 dark:text-emerald-400"
        >
          Authorization complete — the integration is now connected. Its status
          is shown below.
        </div>
      ) : null}

      {/* Category chips */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium text-muted-foreground">
          1. Choose a category
        </h2>
        <div className="flex flex-wrap gap-2">
          {CATEGORIES.map((c) => {
            const meta = CATEGORY_META[c];
            const active = c === category;
            return (
              <button
                key={c}
                type="button"
                onClick={() => {
                  setCategory(c);
                  setSelectedProvider(null);
                }}
                aria-pressed={active}
                className={cn(
                  "flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors",
                  active
                    ? "border-primary bg-primary text-primary-foreground shadow-sm"
                    : "border-border bg-card text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                )}
              >
                <span aria-hidden>{meta.icon}</span>
                {meta.label}
              </button>
            );
          })}
        </div>
      </section>

      {/* Provider logo grid */}
      <section className="space-y-3">
        <div>
          <h2 className="text-sm font-medium text-muted-foreground">
            2. Select a provider
          </h2>
          <p className="text-xs text-muted-foreground">
            {activeMeta.icon} {activeMeta.label} — {activeMeta.description}
          </p>
        </div>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
          {providers.map((provider) => {
            const selected = selectedProvider?.name === provider.name;
            return (
              <button
                key={provider.name}
                type="button"
                onClick={() => chooseProvider(provider)}
                aria-pressed={selected}
                className={cn(
                  "group flex items-center gap-3 rounded-xl border bg-card p-3 text-left transition-all hover:-translate-y-0.5 hover:shadow-md",
                  selected
                    ? "border-primary ring-2 ring-primary/40"
                    : "border-border hover:border-primary/40",
                )}
              >
                <ProviderLogo provider={provider} size="md" />
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium">
                    {provider.label}
                  </span>
                  <span className="mt-0.5 flex items-center gap-1.5">
                    <TriggerBadge provider={provider.name} />
                    <span className="truncate text-xs text-muted-foreground">
                      {selected ? "Selected" : "Click to connect"}
                    </span>
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </section>

      {/* Connect form (revealed once a provider is chosen) */}
      {selectedProvider ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-3">
              <ProviderLogo provider={selectedProvider} size="md" />
              Connect {selectedProvider.label}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-5">
            <ProviderGuide provider={selectedProvider} />
            <form onSubmit={handleConnect} className="space-y-4">
              {fields.map((f) => (
                <div key={f.name} className="space-y-2">
                  <Label htmlFor={`cred-${f.name}`}>
                    {f.label}
                    {f.required ? null : (
                      <span className="ml-1 text-xs text-muted-foreground">
                        (optional)
                      </span>
                    )}
                  </Label>
                  <Input
                    id={`cred-${f.name}`}
                    data-voice-field={`credential:${f.name}`}
                    type={f.secret ? "password" : "text"}
                    value={fieldValues[f.name] ?? ""}
                    onChange={(e) => setField(f.name, e.target.value)}
                    placeholder={f.placeholder}
                    autoComplete="off"
                  />
                  {f.help ? (
                    <p className="text-xs text-muted-foreground">{f.help}</p>
                  ) : null}
                  {f.secret ? (
                    <p className="text-xs text-muted-foreground">
                      Encrypted at rest by the backend and never displayed again.
                    </p>
                  ) : null}
                </div>
              ))}

              <div className="flex items-center justify-between rounded-md border p-3">
                <div>
                  <Label htmlFor="shared">Share with workspace</Label>
                  <p className="text-xs text-muted-foreground">
                    {sharedWithWorkspace
                      ? "Shared — usable by workspace members."
                      : "Personal — usable only by you."}
                  </p>
                </div>
                <Switch
                  id="shared"
                  checked={sharedWithWorkspace}
                  onCheckedChange={setSharedWithWorkspace}
                  aria-label="Share with workspace"
                />
              </div>

              <div className="flex gap-2">
                <Button
                  type="submit"
                  data-voice-form="connect_integration"
                  disabled={submitting}
                >
                  {submitting ? "Connecting…" : `Connect ${selectedProvider.label}`}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setSelectedProvider(null)}
                  disabled={submitting}
                >
                  Cancel
                </Button>
              </div>
            </form>
          </CardContent>
        </Card>
      ) : null}

      {/* Next-step nudge toward Rules once something is connected (BUILD.md) */}
      {integrations.length > 0 ? (
        <Card className="border-primary/40 bg-primary/5">
          <CardContent className="flex flex-wrap items-center justify-between gap-4 p-6">
            <div className="min-w-0">
              <p className="flex items-center gap-2 text-base font-semibold">
                <span aria-hidden>✅</span> Provider connected — what&apos;s next?
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
                Add automation rules for this workload so your agents know exactly
                how to behave. Pick from ready-made rules or write your own.
              </p>
            </div>
            <Link href="/dashboard/rules" className={buttonClasses()}>
              Add automation rules →
            </Link>
          </CardContent>
        </Card>
      ) : null}

      <section className="space-y-4">
        <h2 className="text-lg font-semibold">Connected integrations</h2>
        {integrations.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No integrations connected yet. Pick a provider above to get started.
          </p>
        ) : (
          <ul className="space-y-3">
            {integrations.map((integration) => {
              const busy = busyId === integration.id;
              const meta = CATEGORY_META[integration.category];
              return (
                <li key={integration.id}>
                  <Card>
                    <CardContent className="p-4">
                      <div className="flex flex-wrap items-center justify-between gap-4">
                        <div className="min-w-0">
                          <p className="font-medium">{integration.provider_name}</p>
                          <p className="text-xs text-muted-foreground">
                            {meta?.label ?? integration.category} · status:{" "}
                            {integration.status}
                          </p>
                        </div>
                        <div className="flex items-center gap-4">
                          <div className="flex items-center gap-2">
                            <span className="text-xs text-muted-foreground">
                              {integration.is_shared_with_workspace
                                ? "Shared"
                                : "Personal"}
                            </span>
                            <Switch
                              checked={integration.is_shared_with_workspace}
                              disabled={busy}
                              onCheckedChange={(shared) =>
                                handleToggleSharing(integration, shared)
                              }
                              aria-label={`Toggle sharing for ${integration.provider_name}`}
                            />
                          </div>
                          {isOAuthFamily(integration.provider_name) ? (
                            integration.authorized ? (
                              <div className="flex items-center gap-2">
                                <Badge variant="success">✓ Authorized</Badge>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  disabled={busy}
                                  onClick={() => handleAuthorize(integration)}
                                >
                                  Re-authorize
                                </Button>
                              </div>
                            ) : (
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={busy}
                                onClick={() => handleAuthorize(integration)}
                              >
                                Authorize with {integration.provider_name}
                              </Button>
                            )
                          ) : null}
                          <Button
                            variant="destructive"
                            size="sm"
                            disabled={busy}
                            onClick={() => handleDisconnect(integration)}
                          >
                            Disconnect
                          </Button>
                        </div>
                      </div>
                      <WebhookStatus info={integration.webhook} />
                    </CardContent>
                  </Card>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </main>
  );
}
