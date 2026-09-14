"use client";

import { PROVIDER_CATALOG, type Category, type Provider } from "@/lib/providers";
import { ProviderLogo } from "@/components/integrations/provider-logo";
import { cn } from "@/lib/utils";

/**
 * Compact provider picker used by the rules form (BUILD.md): the optional
 * provider scope is chosen by clicking an SVG logo + name rather than typing.
 * Includes an "Any provider" option (null) so a rule can apply category-wide.
 */
export function ProviderPicker({
  category,
  value,
  onChange,
}: {
  category: Category;
  value: string | null;
  onChange: (providerName: string | null) => void;
}) {
  const providers: Provider[] = PROVIDER_CATALOG[category] ?? [];

  return (
    <div className="flex flex-wrap gap-2">
      <button
        type="button"
        onClick={() => onChange(null)}
        aria-pressed={value === null}
        className={cn(
          "flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm transition-colors",
          value === null
            ? "border-primary bg-primary/10 font-medium"
            : "border-border hover:bg-accent",
        )}
      >
        <span
          aria-hidden
          className="flex h-6 w-6 items-center justify-center rounded-md border text-xs"
        >
          ∗
        </span>
        Any provider
      </button>

      {providers.map((provider) => {
        const selected = value === provider.name;
        return (
          <button
            key={provider.name}
            type="button"
            onClick={() => onChange(provider.name)}
            aria-pressed={selected}
            className={cn(
              "flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm transition-colors",
              selected
                ? "border-primary bg-primary/10 font-medium"
                : "border-border hover:bg-accent",
            )}
          >
            <ProviderLogo provider={provider} size="sm" />
            {provider.label}
          </button>
        );
      })}
    </div>
  );
}
