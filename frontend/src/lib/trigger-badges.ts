/**
 * Trigger classification badges (generated from the backend registry).
 *
 * This map is the authoritative "instant vs scheduled" classification for every
 * supported provider, derived from the verified backend webhook-registration
 * registry (webhook_registration.REGISTRATIONS + provider_webhooks.trigger_for):
 *
 *   - "instant"   → the provider delivers push/pubsub webhooks that fire agents
 *                   immediately (auto/partial/manual registration of a push or
 *                   Pub/Sub trigger).
 *   - "scheduled" → the provider has no usable native webhook for our flow, so
 *                   it is polled on a schedule.
 *
 * DO NOT hand-edit provider entries: regenerate from the backend to keep this in
 * sync (see the dump script used in tooling). Keeping it static avoids an extra
 * round-trip and renders identically in local dev and Docker.
 */

export type TriggerBadge = "instant" | "scheduled";

export interface TriggerInfo {
  /** Registration mode: auto | partial | manual | poll. */
  mode: string;
  /** Trigger delivery kind: push | pubsub | poll. */
  kind: string;
  /** UI badge: instant (push/pubsub) or scheduled (poll). */
  badge: TriggerBadge;
}

export const TRIGGER_BADGES: Record<string, TriggerInfo> = {
  activecampaign: { mode: "auto", kind: "push", badge: "instant" },
  asana: { mode: "partial", kind: "push", badge: "instant" },
  aws: { mode: "poll", kind: "poll", badge: "scheduled" },
  azure_devops: { mode: "partial", kind: "push", badge: "instant" },
  bamboohr: { mode: "poll", kind: "poll", badge: "scheduled" },
  bitbucket: { mode: "partial", kind: "push", badge: "instant" },
  brevo: { mode: "auto", kind: "push", badge: "instant" },
  calendly: { mode: "auto", kind: "push", badge: "instant" },
  close: { mode: "auto", kind: "push", badge: "instant" },
  cloudflare: { mode: "partial", kind: "poll", badge: "scheduled" },
  confluence: { mode: "manual", kind: "push", badge: "instant" },
  convertkit: { mode: "auto", kind: "push", badge: "instant" },
  digitalocean: { mode: "poll", kind: "poll", badge: "scheduled" },
  discord: { mode: "poll", kind: "poll", badge: "scheduled" },
  excel: { mode: "auto", kind: "push", badge: "instant" },
  facebook: { mode: "manual", kind: "push", badge: "instant" },
  freshdesk: { mode: "manual", kind: "push", badge: "instant" },
  gcp: { mode: "manual", kind: "push", badge: "instant" },
  github: { mode: "partial", kind: "push", badge: "instant" },
  gitlab: { mode: "partial", kind: "push", badge: "instant" },
  gmail: { mode: "auto", kind: "pubsub", badge: "instant" },
  google_calendar: { mode: "auto", kind: "push", badge: "instant" },
  google_docs: { mode: "poll", kind: "poll", badge: "scheduled" },
  google_drive: { mode: "auto", kind: "push", badge: "instant" },
  google_meet: { mode: "poll", kind: "poll", badge: "scheduled" },
  google_sheets: { mode: "poll", kind: "poll", badge: "scheduled" },
  google_slides: { mode: "poll", kind: "poll", badge: "scheduled" },
  greenhouse: { mode: "manual", kind: "push", badge: "instant" },
  hubspot: { mode: "manual", kind: "push", badge: "instant" },
  imap_smtp: { mode: "poll", kind: "poll", badge: "scheduled" },
  instagram: { mode: "manual", kind: "push", badge: "instant" },
  intercom: { mode: "manual", kind: "push", badge: "instant" },
  jira: { mode: "manual", kind: "push", badge: "instant" },
  kubernetes: { mode: "poll", kind: "poll", badge: "scheduled" },
  lever: { mode: "manual", kind: "push", badge: "instant" },
  line: { mode: "auto", kind: "push", badge: "instant" },
  linear: { mode: "manual", kind: "push", badge: "instant" },
  mailchimp: { mode: "partial", kind: "push", badge: "instant" },
  mailgun: { mode: "partial", kind: "push", badge: "instant" },
  monday_crm: { mode: "partial", kind: "push", badge: "instant" },
  monday_dev: { mode: "partial", kind: "push", badge: "instant" },
  monday_service: { mode: "partial", kind: "push", badge: "instant" },
  monday_workdocs: { mode: "partial", kind: "push", badge: "instant" },
  monday_workspaces: { mode: "partial", kind: "push", badge: "instant" },
  netsuite: { mode: "poll", kind: "poll", badge: "scheduled" },
  notion: { mode: "poll", kind: "poll", badge: "scheduled" },
  onedrive: { mode: "auto", kind: "push", badge: "instant" },
  outlook: { mode: "auto", kind: "push", badge: "instant" },
  outlook_calendar: { mode: "auto", kind: "push", badge: "instant" },
  paypal: { mode: "auto", kind: "push", badge: "instant" },
  pipedrive: { mode: "auto", kind: "push", badge: "instant" },
  powerpoint: { mode: "auto", kind: "push", badge: "instant" },
  quickbooks: { mode: "manual", kind: "push", badge: "instant" },
  salesforce: { mode: "manual", kind: "poll", badge: "scheduled" },
  sap: { mode: "poll", kind: "poll", badge: "scheduled" },
  sender: { mode: "auto", kind: "push", badge: "instant" },
  sendgrid: { mode: "auto", kind: "push", badge: "instant" },
  slack: { mode: "manual", kind: "push", badge: "instant" },
  stripe: { mode: "auto", kind: "push", badge: "instant" },
  teams: { mode: "auto", kind: "push", badge: "instant" },
  teamviewer: { mode: "poll", kind: "poll", badge: "scheduled" },
  telegram: { mode: "auto", kind: "push", badge: "instant" },
  terraform: { mode: "partial", kind: "push", badge: "instant" },
  trello: { mode: "partial", kind: "push", badge: "instant" },
  twilio_sms: { mode: "manual", kind: "push", badge: "instant" },
  wechat: { mode: "manual", kind: "push", badge: "instant" },
  whatsapp: { mode: "manual", kind: "push", badge: "instant" },
  word: { mode: "auto", kind: "push", badge: "instant" },
  workday: { mode: "poll", kind: "poll", badge: "scheduled" },
  xero: { mode: "manual", kind: "push", badge: "instant" },
  yahoo: { mode: "poll", kind: "poll", badge: "scheduled" },
  zendesk: { mode: "auto", kind: "push", badge: "instant" },
  zoho_crm: { mode: "auto", kind: "push", badge: "instant" },
  zoom: { mode: "manual", kind: "push", badge: "instant" },
};

/** Look up the trigger badge for a provider slug (defaults to scheduled). */
export function triggerBadgeFor(providerName: string): TriggerBadge {
  return TRIGGER_BADGES[providerName]?.badge ?? "scheduled";
}
