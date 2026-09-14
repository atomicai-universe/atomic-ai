/**
 * Provider & automation-rule catalog (BUILD.md).
 *
 * A static, offline-friendly catalog powering the click-to-select UX:
 *   - PROVIDER_CATALOG: every supported provider grouped by the twelve backend
 *     IntegrationCategory values (see PROJECT.md). Each provider carries a short
 *     brand label, a monogram, and a brand color used to render a logo tile so
 *     the integrations page can be "select and click" instead of free typing.
 *   - CATEGORY_META: display label + icon for each category card.
 *
 * Automation rule templates now live in src/lib/rule-templates.ts.
 *
 * No network/image dependency: tiles are pure CSS + a monogram, so they render
 * identically in local dev and inside Docker with no external asset fetches.
 */

export const CATEGORIES = [
  "email",
  "email_marketing",
  "social",
  "office",
  "developer",
  "crm",
  "support",
  "erp",
  "collaboration",
  "cloud",
  "hr",
  "calendar",
] as const;

export type Category = (typeof CATEGORIES)[number];

export interface Provider {
  /** The value stored as `provider_name` on the backend (lowercase slug). */
  name: string;
  /** Human-friendly label shown on the tile. */
  label: string;
  /** 1-2 char monogram rendered on the logo tile. */
  monogram: string;
  /** Brand-ish background color for the tile. */
  color: string;
}

export interface CategoryMeta {
  label: string;
  /** Emoji icon for the category header/card. */
  icon: string;
  description: string;
}

export const CATEGORY_META: Record<Category, CategoryMeta> = {
  email: { label: "Email", icon: "✉️", description: "Inboxes & mail servers" },
  email_marketing: {
    label: "Email Marketing",
    icon: "📣",
    description: "Campaigns & newsletters",
  },
  social: {
    label: "Social & Messaging",
    icon: "💬",
    description: "Social pages & direct messaging",
  },
  office: {
    label: "Office & Productivity",
    icon: "📄",
    description: "Docs, sheets & files",
  },
  developer: {
    label: "Developer & Issue Trackers",
    icon: "💻",
    description: "Repos, boards & pipelines",
  },
  crm: { label: "CRM & Sales", icon: "📈", description: "Pipelines & deals" },
  support: {
    label: "Support & Knowledge",
    icon: "🎧",
    description: "Tickets & knowledge bases",
  },
  erp: { label: "ERP & Financial", icon: "💳", description: "Billing & accounting" },
  collaboration: {
    label: "Team Chat & Meetings",
    icon: "👥",
    description: "Chat, calls & workspaces",
  },
  cloud: {
    label: "Cloud & DevOps",
    icon: "☁️",
    description: "Infrastructure & platforms",
  },
  hr: { label: "HR & Recruiting", icon: "🧑‍💼", description: "Hiring & onboarding" },
  calendar: {
    label: "Calendar & Scheduling",
    icon: "📅",
    description: "Meetings & availability",
  },
};

function p(name: string, label: string, monogram: string, color: string): Provider {
  return { name, label, monogram, color };
}

export const PROVIDER_CATALOG: Record<Category, Provider[]> = {
  email: [
    p("gmail", "Gmail", "G", "#EA4335"),
    p("outlook", "Microsoft Outlook", "O", "#0F6CBD"),
    p("yahoo", "Yahoo Mail", "Y", "#6001D2"),
    p("imap_smtp", "IMAP/SMTP", "@", "#475569"),
  ],
  email_marketing: [
    p("sender", "Sender.net", "S", "#22C55E"),
    p("mailgun", "Mailgun", "M", "#C02021"),
    p("sendgrid", "SendGrid", "SG", "#1A82E2"),
    p("mailchimp", "Mailchimp", "MC", "#FFE01B"),
    p("brevo", "Brevo", "B", "#0B996E"),
    p("convertkit", "ConvertKit", "CK", "#FB6970"),
    p("activecampaign", "ActiveCampaign", "AC", "#356AE6"),
  ],
  social: [
    p("facebook", "Facebook", "f", "#1877F2"),
    p("instagram", "Instagram", "IG", "#E1306C"),
    p("telegram", "Telegram", "TG", "#26A5E4"),
    p("whatsapp", "WhatsApp Business", "WA", "#25D366"),
    p("line", "LINE", "L", "#06C755"),
    p("wechat", "WeChat", "WC", "#07C160"),
    p("twilio_sms", "SMS (Twilio)", "TW", "#F22F46"),
  ],
  office: [
    p("word", "Microsoft Word", "W", "#2B579A"),
    p("powerpoint", "PowerPoint", "P", "#B7472A"),
    p("excel", "Excel", "X", "#217346"),
    p("google_docs", "Google Docs", "GD", "#4285F4"),
    p("google_sheets", "Google Sheets", "GS", "#0F9D58"),
    p("google_slides", "Google Slides", "GL", "#F4B400"),
    p("onedrive", "OneDrive", "OD", "#0364B8"),
    p("google_drive", "Google Drive", "DR", "#1FA463"),
    p("monday_workdocs", "monday WorkDocs", "mW", "#FF3D57"),
  ],
  developer: [
    p("github", "GitHub", "GH", "#181717"),
    p("gitlab", "GitLab", "GL", "#FC6D26"),
    p("jira", "Jira", "J", "#0052CC"),
    p("linear", "Linear", "Li", "#5E6AD2"),
    p("monday_dev", "monday dev", "mD", "#FF3D57"),
    p("bitbucket", "Bitbucket", "BB", "#0052CC"),
    p("azure_devops", "Azure DevOps", "AZ", "#0078D7"),
    p("asana", "Asana", "As", "#F06A6A"),
    p("trello", "Trello", "Tr", "#0079BF"),
  ],
  crm: [
    p("salesforce", "Salesforce", "SF", "#00A1E0"),
    p("hubspot", "HubSpot", "HS", "#FF7A59"),
    p("pipedrive", "Pipedrive", "PD", "#017737"),
    p("monday_crm", "monday sales CRM", "mC", "#FF3D57"),
    p("zoho_crm", "Zoho CRM", "Z", "#E42527"),
    p("close", "Close", "Cl", "#3B82F6"),
  ],
  support: [
    p("zendesk", "Zendesk", "ZD", "#03363D"),
    p("intercom", "Intercom", "IC", "#1F8DED"),
    p("freshdesk", "Freshdesk", "FD", "#25C16F"),
    p("notion", "Notion", "N", "#111111"),
    p("confluence", "Confluence", "Cf", "#172B4D"),
    p("monday_service", "monday service", "mS", "#FF3D57"),
  ],
  erp: [
    p("quickbooks", "QuickBooks", "QB", "#2CA01C"),
    p("xero", "Xero", "Xe", "#13B5EA"),
    p("stripe", "Stripe", "St", "#635BFF"),
    p("sap", "SAP", "SAP", "#0FAAFF"),
    p("netsuite", "NetSuite", "NS", "#1F3864"),
    p("paypal", "PayPal", "PP", "#003087"),
  ],
  collaboration: [
    p("slack", "Slack", "Sl", "#4A154B"),
    p("teams", "Microsoft Teams", "T", "#6264A7"),
    p("discord", "Discord", "Dc", "#5865F2"),
    p("zoom", "Zoom", "Zm", "#2D8CFF"),
    p("google_meet", "Google Meet", "GM", "#00897B"),
    p("teamviewer", "TeamViewer", "TV", "#0E88D3"),
    p("monday_workspaces", "monday Workspaces", "mW", "#FF3D57"),
  ],
  cloud: [
    p("aws", "AWS", "AW", "#FF9900"),
    p("gcp", "Google Cloud", "GC", "#4285F4"),
    p("cloudflare", "Cloudflare", "CF", "#F38020"),
    p("terraform", "Terraform", "TF", "#7B42BC"),
    p("digitalocean", "DigitalOcean", "DO", "#0080FF"),
    p("kubernetes", "Kubernetes", "K8", "#326CE5"),
  ],
  hr: [
    p("workday", "Workday", "Wd", "#F38B00"),
    p("bamboohr", "BambooHR", "BH", "#73C41D"),
    p("greenhouse", "Greenhouse", "Gh", "#24A47F"),
    p("lever", "Lever", "Lv", "#5227CC"),
  ],
  calendar: [
    p("google_calendar", "Google Calendar", "GC", "#4285F4"),
    p("outlook_calendar", "Outlook Calendar", "OC", "#0F6CBD"),
    p("calendly", "Calendly", "Cy", "#006BFF"),
  ],
};
