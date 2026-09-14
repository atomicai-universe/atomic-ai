"use client";

/**
 * Automation Rules page (task 17.3, Req 8.1; BUILD.md layout + templates).
 *
 * Full CRUD over workspace automation rules:
 *   - List   -> GET    /api/v1/rules?workspace_id=...
 *   - Create -> POST   /api/v1/rules
 *   - Edit   -> PATCH  /api/v1/rules/{id}
 *   - Delete -> DELETE /api/v1/rules/{id}?workspace_id=...
 *
 * BUILD.md UX:
 *   - The create form sits on the left; the "Quick-add rules" gallery sits
 *     beside it on the right (two-column on large screens). Every category has
 *     at least 20 click-to-add rules (see src/lib/rule-templates.ts).
 *   - The optional provider scope is chosen from an SVG-logo + name picker
 *     rather than a free-text field.
 *
 * Each rule carries a category, optional provider scope, the rule prompt, and
 * the `is_workspace_wide` / `is_active` flags. Workspace-wide create/modify is
 * Owner/Admin-only on the backend (Req 8.5) — a 403 surfaces as the ApiError
 * message. Workspace scoping uses the active workspace id (see lib/workspace.ts).
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { getActiveWorkspaceId } from "@/lib/workspace";
import { CATEGORIES, CATEGORY_META, type Category } from "@/lib/providers";
import {
  RULE_TEMPLATES_BY_CATEGORY,
  type RuleTemplate,
} from "@/lib/rule-templates";
import { ProviderPicker } from "@/components/integrations/provider-picker";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

interface Rule {
  id: string;
  workspace_id: string;
  created_by_user_id: string;
  category: string;
  provider_name: string | null;
  rule_prompt: string;
  is_workspace_wide: boolean;
  is_active: boolean;
}

interface RuleListResponse {
  rules: Rule[];
}

function categoryLabel(category: string): string {
  const meta = (CATEGORY_META as Record<string, { label: string }>)[category];
  if (meta) return meta.label;
  return category
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export default function RulesPage() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Create form state.
  const [category, setCategory] = useState<Category>("email");
  const [providerName, setProviderName] = useState<string | null>(null);
  const [rulePrompt, setRulePrompt] = useState("");
  const [isWorkspaceWide, setIsWorkspaceWide] = useState(false);
  const [isActive, setIsActive] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  // Per-row busy + edit state.
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Rule | null>(null);
  // Highlights the most recently applied template for quick visual feedback.
  const [appliedTitle, setAppliedTitle] = useState<string | null>(null);

  const templates = useMemo<RuleTemplate[]>(
    () => RULE_TEMPLATES_BY_CATEGORY[category] ?? [],
    [category],
  );

  function applyTemplate(template: RuleTemplate) {
    setCategory(template.category);
    setRulePrompt(template.prompt);
    setAppliedTitle(template.title);
    setError(null);
    requestAnimationFrame(() => {
      const el = document.getElementById("prompt");
      el?.scrollIntoView({ behavior: "smooth", block: "center" });
      (el as HTMLTextAreaElement | null)?.focus();
    });
  }

  function reportError(err: unknown, fallback: string) {
    if (err instanceof ApiError || err instanceof Error) {
      setError(err.message);
    } else {
      setError(fallback);
    }
  }

  const loadRules = useCallback(async () => {
    setError(null);
    const workspaceId = getActiveWorkspaceId();
    if (!workspaceId) {
      setLoading(false);
      setError("No active workspace selected. Create or select a workspace first.");
      return;
    }
    setLoading(true);
    try {
      const data = await api.get<RuleListResponse>(
        `/api/v1/rules?workspace_id=${encodeURIComponent(workspaceId)}`,
      );
      setRules(data.rules);
    } catch (err) {
      reportError(err, "Failed to load rules.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRules();
  }, [loadRules]);

  // When the category changes, clear a provider scope that no longer belongs.
  function changeCategory(next: Category) {
    setCategory(next);
    setProviderName(null);
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const workspaceId = getActiveWorkspaceId();
    if (!workspaceId) {
      setError("No active workspace selected. Create or select a workspace first.");
      return;
    }
    if (!rulePrompt.trim()) {
      setError("Rule prompt is required.");
      return;
    }
    setSubmitting(true);
    try {
      const created = await api.post<Rule>("/api/v1/rules", {
        workspace_id: workspaceId,
        category,
        provider_name: providerName ?? undefined,
        rule_prompt: rulePrompt.trim(),
        is_workspace_wide: isWorkspaceWide,
        is_active: isActive,
      });
      setRules((prev) => [...prev, created]);
      setProviderName(null);
      setRulePrompt("");
      setIsWorkspaceWide(false);
      setIsActive(true);
      setAppliedTitle(null);
    } catch (err) {
      reportError(err, "Failed to create rule.");
    } finally {
      setSubmitting(false);
    }
  }

  function startEdit(rule: Rule) {
    setEditingId(rule.id);
    setDraft({ ...rule });
    setError(null);
  }

  function cancelEdit() {
    setEditingId(null);
    setDraft(null);
  }

  async function saveEdit() {
    if (!draft) return;
    setError(null);
    const workspaceId = getActiveWorkspaceId() ?? draft.workspace_id;
    if (!draft.rule_prompt.trim()) {
      setError("Rule prompt is required.");
      return;
    }
    setBusyId(draft.id);
    try {
      const updated = await api.patch<Rule>(`/api/v1/rules/${draft.id}`, {
        workspace_id: workspaceId,
        category: draft.category,
        provider_name: draft.provider_name,
        update_provider_name: true,
        rule_prompt: draft.rule_prompt.trim(),
        is_workspace_wide: draft.is_workspace_wide,
        is_active: draft.is_active,
      });
      setRules((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
      cancelEdit();
    } catch (err) {
      reportError(err, "Failed to update rule.");
    } finally {
      setBusyId(null);
    }
  }

  async function toggleActive(rule: Rule, active: boolean) {
    setError(null);
    const workspaceId = getActiveWorkspaceId() ?? rule.workspace_id;
    setBusyId(rule.id);
    try {
      const updated = await api.patch<Rule>(`/api/v1/rules/${rule.id}`, {
        workspace_id: workspaceId,
        is_active: active,
      });
      setRules((prev) => prev.map((r) => (r.id === updated.id ? updated : r)));
    } catch (err) {
      reportError(err, "Failed to update rule.");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(rule: Rule) {
    setError(null);
    const workspaceId = getActiveWorkspaceId() ?? rule.workspace_id;
    setBusyId(rule.id);
    try {
      await api.del(
        `/api/v1/rules/${rule.id}?workspace_id=${encodeURIComponent(workspaceId)}`,
      );
      setRules((prev) => prev.filter((r) => r.id !== rule.id));
    } catch (err) {
      reportError(err, "Failed to delete rule.");
    } finally {
      setBusyId(null);
    }
  }

  const activeMeta = CATEGORY_META[category];

  return (
    <main className="mx-auto max-w-6xl space-y-8 p-8">
      <header>
        <h1 className="text-2xl font-semibold">Automation Rules</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Define natural-language rules that steer agent behavior. Pick a
          category, click a ready-made rule to load it, choose the provider it
          applies to, then save. Workspace-wide rules apply to everyone.
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

      {/* Two-column workspace: create form (left) + quick-add gallery (right). */}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
        {/* LEFT: create form */}
        <Card className="lg:sticky lg:top-6 lg:self-start">
          <CardHeader>
            <CardTitle>Create a rule</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleCreate} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="category">Category</Label>
                <Select
                  id="category"
                  value={category}
                  onChange={(e) => changeCategory(e.target.value as Category)}
                >
                  {CATEGORIES.map((c) => (
                    <option key={c} value={c}>
                      {categoryLabel(c)}
                    </option>
                  ))}
                </Select>
              </div>

              <div className="space-y-2">
                <Label>Provider (optional)</Label>
                <p className="text-xs text-muted-foreground">
                  Scope this rule to one provider, or leave as “Any provider”.
                </p>
                <ProviderPicker
                  category={category}
                  value={providerName}
                  onChange={setProviderName}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="prompt">Rule prompt</Label>
                <Textarea
                  id="prompt"
                  data-voice-field="rule_prompt"
                  value={rulePrompt}
                  onChange={(e) => setRulePrompt(e.target.value)}
                  rows={5}
                  placeholder="Click a quick-add rule on the right, or write your own. e.g. Always summarize incoming emails and never send replies without approval."
                />
              </div>

              <div className="flex flex-wrap gap-6">
                <div className="flex items-center gap-2">
                  <Switch
                    id="workspace-wide"
                    checked={isWorkspaceWide}
                    onCheckedChange={setIsWorkspaceWide}
                    aria-label="Workspace-wide"
                  />
                  <Label htmlFor="workspace-wide">Workspace-wide</Label>
                </div>
                <div className="flex items-center gap-2">
                  <Switch
                    id="active"
                    checked={isActive}
                    onCheckedChange={setIsActive}
                    aria-label="Active"
                  />
                  <Label htmlFor="active">Active</Label>
                </div>
              </div>

              <Button
                type="submit"
                data-voice-form="create_rule"
                disabled={submitting}
              >
                {submitting ? "Creating…" : "Create rule"}
              </Button>
            </form>
          </CardContent>
        </Card>

        {/* RIGHT: quick-add rules gallery for the selected category. */}
        <section className="space-y-3">
          <div>
            <h2 className="flex items-center gap-2 text-lg font-semibold">
              <span aria-hidden>{activeMeta.icon}</span>
              Quick-add rules — {activeMeta.label}
            </h2>
            <p className="text-sm text-muted-foreground">
              {templates.length} ready-made rules for this category. Click one to
              load it into the form.
            </p>
          </div>
          <div className="grid max-h-[70vh] gap-3 overflow-y-auto rounded-lg border bg-muted/20 p-3 sm:grid-cols-2">
            {templates.map((template) => {
              const applied = appliedTitle === template.title;
              return (
                <button
                  key={template.title}
                  type="button"
                  onClick={() => applyTemplate(template)}
                  aria-pressed={applied}
                  className={cn(
                    "flex h-full flex-col gap-1 rounded-xl border bg-card p-4 text-left transition-all hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-md",
                    applied ? "border-primary ring-2 ring-primary/40" : "border-border",
                  )}
                >
                  <span className="text-sm font-semibold">{template.title}</span>
                  <span className="text-xs text-muted-foreground">
                    {template.prompt}
                  </span>
                  <span className="mt-auto pt-1 text-xs font-medium text-primary">
                    {applied ? "Loaded into form ←" : "Click to add"}
                  </span>
                </button>
              );
            })}
          </div>
        </section>
      </div>

      {/* Existing rules list */}
      <section className="space-y-4">
        <h2 className="text-lg font-semibold">Your rules</h2>
        {loading ? (
          <p className="text-sm text-muted-foreground">Loading rules…</p>
        ) : rules.length === 0 ? (
          <p className="text-sm text-muted-foreground">No rules yet.</p>
        ) : (
          <ul className="space-y-3">
            {rules.map((rule) => {
              const busy = busyId === rule.id;
              const isEditing = editingId === rule.id && draft !== null;
              return (
                <li key={rule.id}>
                  <Card>
                    <CardContent className="space-y-3 p-4">
                      {isEditing && draft ? (
                        <div className="space-y-3">
                          <div className="space-y-1">
                            <Label htmlFor={`cat-${rule.id}`}>Category</Label>
                            <Select
                              id={`cat-${rule.id}`}
                              value={draft.category}
                              onChange={(e) =>
                                setDraft({
                                  ...draft,
                                  category: e.target.value,
                                  provider_name: null,
                                })
                              }
                            >
                              {CATEGORIES.map((c) => (
                                <option key={c} value={c}>
                                  {categoryLabel(c)}
                                </option>
                              ))}
                            </Select>
                          </div>
                          <div className="space-y-1">
                            <Label>Provider</Label>
                            <ProviderPicker
                              category={draft.category as Category}
                              value={draft.provider_name}
                              onChange={(name) =>
                                setDraft({ ...draft, provider_name: name })
                              }
                            />
                          </div>
                          <div className="space-y-1">
                            <Label htmlFor={`prompt-${rule.id}`}>Rule prompt</Label>
                            <Textarea
                              id={`prompt-${rule.id}`}
                              value={draft.rule_prompt}
                              onChange={(e) =>
                                setDraft({ ...draft, rule_prompt: e.target.value })
                              }
                            />
                          </div>
                          <div className="flex flex-wrap gap-6">
                            <div className="flex items-center gap-2">
                              <Switch
                                checked={draft.is_workspace_wide}
                                onCheckedChange={(v) =>
                                  setDraft({ ...draft, is_workspace_wide: v })
                                }
                                aria-label="Workspace-wide"
                              />
                              <span className="text-sm">Workspace-wide</span>
                            </div>
                            <div className="flex items-center gap-2">
                              <Switch
                                checked={draft.is_active}
                                onCheckedChange={(v) =>
                                  setDraft({ ...draft, is_active: v })
                                }
                                aria-label="Active"
                              />
                              <span className="text-sm">Active</span>
                            </div>
                          </div>
                          <div className="flex gap-2">
                            <Button
                              size="sm"
                              disabled={busy}
                              onClick={() => void saveEdit()}
                            >
                              {busy ? "Saving…" : "Save"}
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busy}
                              onClick={cancelEdit}
                            >
                              Cancel
                            </Button>
                          </div>
                        </div>
                      ) : (
                        <div className="space-y-3">
                          <div className="flex flex-wrap items-start justify-between gap-3">
                            <div className="min-w-0">
                              <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
                                {categoryLabel(rule.category)}
                                {rule.provider_name ? (
                                  <span className="text-muted-foreground">
                                    · {rule.provider_name}
                                  </span>
                                ) : null}
                                {rule.is_workspace_wide ? (
                                  <span className="rounded-full border px-2 py-0.5 text-xs">
                                    Workspace-wide
                                  </span>
                                ) : (
                                  <span className="rounded-full border px-2 py-0.5 text-xs">
                                    Personal
                                  </span>
                                )}
                              </p>
                              <p className="mt-1 whitespace-pre-wrap text-sm text-muted-foreground">
                                {rule.rule_prompt}
                              </p>
                            </div>
                            <div className="flex items-center gap-2">
                              <span className="text-xs text-muted-foreground">
                                {rule.is_active ? "Active" : "Inactive"}
                              </span>
                              <Switch
                                checked={rule.is_active}
                                disabled={busy}
                                onCheckedChange={(active) =>
                                  toggleActive(rule, active)
                                }
                                aria-label={`Toggle active for rule ${rule.id}`}
                              />
                            </div>
                          </div>
                          <div className="flex gap-2">
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busy}
                              onClick={() => startEdit(rule)}
                            >
                              Edit
                            </Button>
                            <Button
                              size="sm"
                              variant="destructive"
                              disabled={busy}
                              onClick={() => handleDelete(rule)}
                            >
                              Delete
                            </Button>
                          </div>
                        </div>
                      )}
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
