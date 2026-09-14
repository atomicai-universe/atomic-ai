import { getProviderGuide } from "@/lib/provider-guides";
import type { Provider } from "@/lib/providers";
import { ProviderLogo } from "@/components/integrations/provider-logo";

/**
 * Graphical, step-by-step integration guide shown when a provider is selected
 * (BUILD.md). Numbered steps with clickable links to the provider's own
 * developer console / docs so the user can follow along.
 */
export function ProviderGuide({ provider }: { provider: Provider }) {
  const guide = getProviderGuide(provider.name, provider.label);

  return (
    <div className="rounded-xl border bg-muted/20 p-5">
      <div className="mb-4 flex items-center gap-3">
        <ProviderLogo provider={provider} size="md" />
        <div>
          <p className="text-sm font-semibold">
            How to connect {provider.label}
          </p>
          <p className="text-xs text-muted-foreground">
            Follow these steps, then paste your token below.
          </p>
        </div>
        {guide.docsUrl ? (
          <a
            href={guide.docsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="ml-auto text-xs font-medium text-primary underline underline-offset-2 hover:no-underline"
          >
            Open developer console ↗
          </a>
        ) : null}
      </div>

      <ol className="space-y-3">
        {guide.steps.map((step, i) => (
          <li key={step.title} className="flex gap-3">
            <span
              aria-hidden
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary text-xs font-bold text-primary-foreground"
            >
              {i + 1}
            </span>
            <div className="min-w-0">
              <p className="text-sm font-medium">{step.title}</p>
              <p className="text-xs text-muted-foreground">{step.detail}</p>
              {step.link ? (
                <a
                  href={step.link.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-block text-xs font-medium text-primary underline underline-offset-2 hover:no-underline"
                >
                  {step.link.label} ↗
                </a>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
