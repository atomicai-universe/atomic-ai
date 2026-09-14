import { cn } from "@/lib/utils";
import type { Provider } from "@/lib/providers";
import { PROVIDER_ICONS } from "@/lib/provider-icons";

/**
 * Renders a provider's official brand SVG logo (from simple-icons) when one is
 * available, falling back to a branded monogram tile for providers the icon
 * package does not ship (BUILD.md: "latest SVG logo for all the Providers").
 *
 * Pure inline SVG / CSS — no external asset fetches, so it renders identically
 * in local dev and inside Docker.
 */
export function ProviderLogo({
  provider,
  size = "md",
  className,
}: {
  provider: Provider;
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const dims =
    size === "lg"
      ? "h-12 w-12"
      : size === "sm"
        ? "h-8 w-8"
        : "h-10 w-10";
  const textSize =
    size === "lg" ? "text-lg" : size === "sm" ? "text-xs" : "text-sm";

  const icon = PROVIDER_ICONS[provider.name];

  if (icon) {
    return (
      <span
        className={cn(
          "flex shrink-0 items-center justify-center rounded-xl bg-white p-2 shadow-sm ring-1 ring-black/10 dark:bg-white",
          dims,
          className,
        )}
      >
        <svg
          role="img"
          aria-hidden
          viewBox="0 0 24 24"
          className="h-full w-full"
          fill={icon.hex}
        >
          <path d={icon.path} />
        </svg>
      </span>
    );
  }

  // Fallback: brand-colored monogram tile.
  return (
    <span
      aria-hidden
      style={{ backgroundColor: provider.color }}
      className={cn(
        "flex shrink-0 items-center justify-center rounded-xl font-bold text-white shadow-sm ring-1 ring-black/10",
        dims,
        textSize,
        className,
      )}
    >
      {provider.monogram}
    </span>
  );
}
