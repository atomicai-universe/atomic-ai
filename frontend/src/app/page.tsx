"use client";

import { oauthLoginUrl } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

/** Landing / login entry. Sign-in buttons hand off to the backend OAuth flow. */
export default function HomePage() {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-2xl">Atomic AI</CardTitle>
          <p className="text-sm text-muted-foreground">
            Sign in to your team workspace to orchestrate autonomous agents.
          </p>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <a href={oauthLoginUrl("google")} className="w-full">
            <Button className="w-full">Sign in with Google</Button>
          </a>
          <a href={oauthLoginUrl("github")} className="w-full">
            <Button variant="outline" className="w-full">
              Sign in with GitHub
            </Button>
          </a>
        </CardContent>
      </Card>
    </main>
  );
}
