"use client";

/**
 * Per-provider credential field specification (BUILD.md).
 *
 * Mirrors the backend `GET /api/v1/integrations/spec` response so the connect
 * form can render exactly the fields each provider needs. Secret fields are
 * masked in the UI and encrypted at rest by the backend vault; config fields
 * are non-secret operational values (region, host, base_url, tenant id, …).
 */

import { api } from "@/lib/api";

export interface CredentialFieldSpec {
  name: string;
  label: string;
  secret: boolean;
  required: boolean;
  placeholder: string;
  help: string;
}

export type CredentialSpec = Record<string, CredentialFieldSpec[]>;

interface SpecResponse {
  providers: CredentialSpec;
}

let cached: CredentialSpec | null = null;

/** Fetch (and cache) the per-provider credential spec from the backend. */
export async function fetchCredentialSpec(): Promise<CredentialSpec> {
  if (cached) return cached;
  const res = await api.get<SpecResponse>("/api/v1/integrations/spec");
  cached = res.providers ?? {};
  return cached;
}

/** Generic fallback used when a provider has no explicit spec entry. */
export const GENERIC_FIELDS: CredentialFieldSpec[] = [
  {
    name: "access_token",
    label: "Access Token / API Key",
    secret: true,
    required: true,
    placeholder: "",
    help: "",
  },
  {
    name: "refresh_token",
    label: "Refresh Token",
    secret: true,
    required: false,
    placeholder: "",
    help: "",
  },
];
