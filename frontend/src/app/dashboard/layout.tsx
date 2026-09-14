"use client";

/**
 * Dashboard shell (task 17.2).
 *
 * Wraps every `/dashboard/*` page with a header that carries the workspace
 * switcher (Req 3.7) and primary navigation to the workspace/team, integrations,
 * approvals, and rules areas. The switcher persists the active workspace id that
 * the API client sends as `X-Workspace-Id`, so switching here re-scopes the
 * pages rendered below.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { WorkspaceSwitcher } from "@/components/workspace/workspace-switcher";
import { ThemeToggle } from "@/components/chrome/theme-toggle";
import { UserMenu } from "@/components/chrome/user-menu";
import { AccessibilityWidget } from "@/components/chrome/accessibility-widget";
import { cn } from "@/lib/utils";

const NAV_LINKS = [
  { href: "/dashboard/workspace", label: "Team" },
  { href: "/dashboard/integrations", label: "Integrations" },
  { href: "/dashboard/rules", label: "Rules" },
  { href: "/dashboard/approvals", label: "Approvals" },
] as const;

export default function DashboardLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-b bg-card">
        <div className="flex flex-wrap items-center justify-between gap-4 px-6 py-3">
          <div className="flex items-center gap-6">
            <Link href="/dashboard/workspace" className="text-lg font-semibold">
              Atomic AI
            </Link>
            <nav className="flex items-center gap-1" aria-label="Primary">
              {NAV_LINKS.map((link) => {
                const active =
                  pathname === link.href || pathname.startsWith(`${link.href}/`);
                return (
                  <Link
                    key={link.href}
                    href={link.href}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "rounded-md px-3 py-2 text-sm font-medium transition-colors",
                      active
                        ? "bg-accent text-accent-foreground"
                        : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                    )}
                  >
                    {link.label}
                  </Link>
                );
              })}
            </nav>
          </div>
          <div className="flex items-center gap-3">
            <WorkspaceSwitcher />
            <ThemeToggle />
            <UserMenu />
          </div>
        </div>
      </header>
      <div className="flex-1">{children}</div>
      <AccessibilityWidget />
    </div>
  );
}
