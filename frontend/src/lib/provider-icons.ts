/**
 * Provider -> simple-icons SVG mapping (BUILD.md).
 *
 * Only providers whose brand ships an icon in the MIT-licensed
 * `simple-icons` package appear here; each entry carries the official SVG
 * path + brand hex. Providers not present (brands that opted out of the
 * package, brands whose only icon is a generic "@" glyph, or generic
 * protocols like IMAP/SMTP) are absent and the UI falls back to a branded
 * monogram tile. Icons are imported by name so the bundler
 * tree-shakes the ~3400 unused icons. Fully offline / Docker-safe.
 */

import {
  siAsana,
  siBitbucket,
  siBrevo,
  siCalendly,
  siCloudflare,
  siConfluence,
  siDigitalocean,
  siDiscord,
  siFacebook,
  siGithub,
  siGitlab,
  siGmail,
  siGooglecalendar,
  siGooglecloud,
  siGoogledocs,
  siGoogledrive,
  siGooglemeet,
  siGooglesheets,
  siGoogleslides,
  siGreenhouse,
  siHubspot,
  siInstagram,
  siIntercom,
  siJira,
  siKit,
  siKubernetes,
  siLine,
  siLinear,
  siMailchimp,
  siNotion,
  siPaypal,
  siQuickbooks,
  siSap,
  siStripe,
  siTeamviewer,
  siTelegram,
  siTerraform,
  siTrello,
  siWechat,
  siWhatsapp,
  siXero,
  siZendesk,
  siZoho,
  siZoom,
} from "simple-icons";

export interface ProviderIcon {
  /** Raw inner SVG path data for a 24x24 viewBox. */
  path: string;
  /** Brand hex color (no leading #). */
  hex: string;
}

/** Provider `name` -> its official brand icon, when simple-icons ships one. */
export const PROVIDER_ICONS: Record<string, ProviderIcon> = {
  gmail: { path: siGmail.path, hex: `#${siGmail.hex}` },
  mailchimp: { path: siMailchimp.path, hex: `#${siMailchimp.hex}` },
  brevo: { path: siBrevo.path, hex: `#${siBrevo.hex}` },
  convertkit: { path: siKit.path, hex: `#${siKit.hex}` },
  facebook: { path: siFacebook.path, hex: `#${siFacebook.hex}` },
  instagram: { path: siInstagram.path, hex: `#${siInstagram.hex}` },
  telegram: { path: siTelegram.path, hex: `#${siTelegram.hex}` },
  whatsapp: { path: siWhatsapp.path, hex: `#${siWhatsapp.hex}` },
  line: { path: siLine.path, hex: `#${siLine.hex}` },
  wechat: { path: siWechat.path, hex: `#${siWechat.hex}` },
  google_docs: { path: siGoogledocs.path, hex: `#${siGoogledocs.hex}` },
  google_sheets: { path: siGooglesheets.path, hex: `#${siGooglesheets.hex}` },
  google_slides: { path: siGoogleslides.path, hex: `#${siGoogleslides.hex}` },
  google_drive: { path: siGoogledrive.path, hex: `#${siGoogledrive.hex}` },
  github: { path: siGithub.path, hex: `#${siGithub.hex}` },
  gitlab: { path: siGitlab.path, hex: `#${siGitlab.hex}` },
  jira: { path: siJira.path, hex: `#${siJira.hex}` },
  linear: { path: siLinear.path, hex: `#${siLinear.hex}` },
  bitbucket: { path: siBitbucket.path, hex: `#${siBitbucket.hex}` },
  asana: { path: siAsana.path, hex: `#${siAsana.hex}` },
  trello: { path: siTrello.path, hex: `#${siTrello.hex}` },
  hubspot: { path: siHubspot.path, hex: `#${siHubspot.hex}` },
  zoho_crm: { path: siZoho.path, hex: `#${siZoho.hex}` },
  zendesk: { path: siZendesk.path, hex: `#${siZendesk.hex}` },
  intercom: { path: siIntercom.path, hex: `#${siIntercom.hex}` },
  notion: { path: siNotion.path, hex: `#${siNotion.hex}` },
  confluence: { path: siConfluence.path, hex: `#${siConfluence.hex}` },
  quickbooks: { path: siQuickbooks.path, hex: `#${siQuickbooks.hex}` },
  xero: { path: siXero.path, hex: `#${siXero.hex}` },
  stripe: { path: siStripe.path, hex: `#${siStripe.hex}` },
  sap: { path: siSap.path, hex: `#${siSap.hex}` },
  paypal: { path: siPaypal.path, hex: `#${siPaypal.hex}` },
  discord: { path: siDiscord.path, hex: `#${siDiscord.hex}` },
  zoom: { path: siZoom.path, hex: `#${siZoom.hex}` },
  google_meet: { path: siGooglemeet.path, hex: `#${siGooglemeet.hex}` },
  teamviewer: { path: siTeamviewer.path, hex: `#${siTeamviewer.hex}` },
  gcp: { path: siGooglecloud.path, hex: `#${siGooglecloud.hex}` },
  cloudflare: { path: siCloudflare.path, hex: `#${siCloudflare.hex}` },
  terraform: { path: siTerraform.path, hex: `#${siTerraform.hex}` },
  digitalocean: { path: siDigitalocean.path, hex: `#${siDigitalocean.hex}` },
  kubernetes: { path: siKubernetes.path, hex: `#${siKubernetes.hex}` },
  greenhouse: { path: siGreenhouse.path, hex: `#${siGreenhouse.hex}` },
  google_calendar: { path: siGooglecalendar.path, hex: `#${siGooglecalendar.hex}` },
  calendly: { path: siCalendly.path, hex: `#${siCalendly.hex}` },
};
