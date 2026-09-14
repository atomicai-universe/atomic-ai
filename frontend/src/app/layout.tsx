import type { Metadata } from "next";
import type { ReactNode } from "react";

import { SessionProvider } from "@/lib/auth";
import { THEME_INIT_SCRIPT } from "@/components/chrome/theme-toggle";
import "./globals.css";

export const metadata: Metadata = {
  title: "Atomic AI",
  description: "Team workspaces and autonomous AI automation.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-screen bg-background text-foreground antialiased">
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
