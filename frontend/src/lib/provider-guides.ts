/**
 * Per-provider "how to connect" guides (BUILD.md).
 *
 * When a provider is selected on the integrations page we show an ordered set of
 * steps, and EVERY step carries its own clickable link to the exact place in the
 * provider's documentation the user needs:
 *   1. Developer console  — where to sign in and create credentials
 *   2. Create app / token — the create-credentials / OAuth-app doc
 *   3. Scopes / permissions — the scopes or permission reference
 *   4. Use with Strands   — how to wire the provider's API/MCP into the Strands
 *      Agents SDK (via the Strands MCP tools guide)
 *
 * Providers without a specific entry fall back to a generic-but-real set of
 * links (still one per step) so nothing is ever a dead step.
 */

/** Canonical Strands SDK docs for connecting external tools/APIs to an agent. */
const STRANDS_MCP_TOOLS_URL =
  "https://strandsagents.com/docs/learning/give-your-agent-tools-using-mcp/";
const STRANDS_TOOLS_OVERVIEW_URL =
  "https://strandsagents.com/docs/user-guide/concepts/tools/";

export interface GuideLink {
  label: string;
  url: string;
}

export interface GuideStep {
  /** Short imperative step title. */
  title: string;
  /** One-line explanation. */
  detail: string;
  /** Clickable link for this step (every step has one). */
  link: GuideLink;
}

export interface ProviderGuide {
  /** Primary developer/console link shown at the top of the guide. */
  docsUrl: string;
  steps: GuideStep[];
}

/**
 * Per-provider link set. Each entry supplies the four URLs used by the four
 * guide steps. `strands` defaults to the Strands MCP tools guide when omitted.
 */
interface ProviderLinks {
  /** Step 1 — developer console / credentials home. */
  console: string;
  /** Step 2 — create app / generate token doc. */
  create: string;
  /** Step 3 — scopes / permissions reference. */
  scopes: string;
  /** Step 4 — how to use with Strands (defaults to the MCP tools guide). */
  strands?: string;
}

const LINKS: Record<string, ProviderLinks> = {
  // ---- Email ----
  gmail: {
    console: "https://console.cloud.google.com/apis/credentials",
    create: "https://developers.google.com/workspace/guides/create-credentials",
    scopes: "https://developers.google.com/gmail/api/auth/scopes",
  },
  outlook: {
    console: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
    create:
      "https://learn.microsoft.com/en-us/graph/auth-register-app-v2",
    scopes:
      "https://learn.microsoft.com/en-us/graph/permissions-reference",
  },
  yahoo: {
    console: "https://developer.yahoo.com/apps/",
    create: "https://developer.yahoo.com/oauth2/guide/openid_connect/getting_started.html",
    scopes: "https://developer.yahoo.com/oauth2/guide/",
  },
  imap_smtp: {
    console: "https://support.google.com/a/answer/105694",
    create: "https://datatracker.ietf.org/doc/html/rfc3501",
    scopes: "https://datatracker.ietf.org/doc/html/rfc4954",
  },

  // ---- Email marketing ----
  sender: {
    console: "https://app.sender.net/settings/tokens",
    create: "https://api.sender.net/authentication/",
    scopes: "https://api.sender.net/authentication/",
  },
  mailgun: {
    console: "https://app.mailgun.com/settings/api_security",
    create: "https://documentation.mailgun.com/docs/mailgun/api-reference/api-overview",
    scopes: "https://documentation.mailgun.com/docs/mailgun/user-manual/api-key-mgmt/rbac-mgmt",
  },
  sendgrid: {
    console: "https://app.sendgrid.com/settings/api_keys",
    create: "https://www.twilio.com/docs/sendgrid/ui/account-and-settings/api-keys",
    scopes: "https://www.twilio.com/docs/sendgrid/api-reference/api-key-permissions/api-key-permissions",
  },
  mailchimp: {
    console: "https://us1.admin.mailchimp.com/account/api/",
    create: "https://mailchimp.com/developer/marketing/guides/quick-start/",
    scopes: "https://mailchimp.com/developer/marketing/docs/fundamentals/",
  },
  brevo: {
    console: "https://app.brevo.com/settings/keys/api",
    create: "https://developers.brevo.com/docs/getting-started",
    scopes: "https://developers.brevo.com/docs/api-key-authentication",
  },
  convertkit: {
    console: "https://app.kit.com/account_settings/developer_settings",
    create: "https://developers.kit.com/#authentication",
    scopes: "https://developers.kit.com/#oauth-scopes",
  },
  activecampaign: {
    console: "https://www.activecampaign.com/",
    create: "https://developers.activecampaign.com/reference/authentication",
    scopes: "https://developers.activecampaign.com/reference/overview",
  },

  // ---- Social & messaging ----
  facebook: {
    console: "https://developers.facebook.com/apps/",
    create: "https://developers.facebook.com/docs/development/create-an-app/",
    scopes: "https://developers.facebook.com/docs/permissions",
  },
  instagram: {
    console: "https://developers.facebook.com/apps/",
    create: "https://developers.facebook.com/docs/instagram-platform",
    scopes: "https://developers.facebook.com/docs/permissions",
  },
  telegram: {
    console: "https://t.me/BotFather",
    create: "https://core.telegram.org/bots/features#botfather",
    scopes: "https://core.telegram.org/bots/api#authorizing-your-bot",
  },
  whatsapp: {
    console: "https://developers.facebook.com/apps/",
    create: "https://developers.facebook.com/docs/whatsapp/cloud-api/get-started",
    scopes: "https://developers.facebook.com/docs/permissions",
  },
  line: {
    console: "https://developers.line.biz/console/",
    create: "https://developers.line.biz/en/docs/messaging-api/getting-started/",
    scopes: "https://developers.line.biz/en/docs/messaging-api/overview/",
  },
  wechat: {
    console: "https://mp.weixin.qq.com/",
    create: "https://developers.weixin.qq.com/doc/offiaccount/en/Basic_Information/Access_Overview.html",
    scopes: "https://developers.weixin.qq.com/doc/offiaccount/en/User_Management/Get_users_basic_information_UnionID.html",
  },
  twilio_sms: {
    console: "https://console.twilio.com/",
    create: "https://www.twilio.com/docs/iam/api-keys",
    scopes: "https://www.twilio.com/docs/iam/keys/api-key",
  },

  // ---- Office & productivity ----
  word: {
    console: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
    create: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2",
    scopes: "https://learn.microsoft.com/en-us/graph/permissions-reference",
  },
  powerpoint: {
    console: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
    create: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2",
    scopes: "https://learn.microsoft.com/en-us/graph/permissions-reference",
  },
  excel: {
    console: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
    create: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2",
    scopes: "https://learn.microsoft.com/en-us/graph/permissions-reference",
  },
  google_docs: {
    console: "https://console.cloud.google.com/apis/credentials",
    create: "https://developers.google.com/workspace/guides/create-credentials",
    scopes: "https://developers.google.com/docs/api/auth",
  },
  google_sheets: {
    console: "https://console.cloud.google.com/apis/credentials",
    create: "https://developers.google.com/workspace/guides/create-credentials",
    scopes: "https://developers.google.com/sheets/api/scopes",
  },
  google_slides: {
    console: "https://console.cloud.google.com/apis/credentials",
    create: "https://developers.google.com/workspace/guides/create-credentials",
    scopes: "https://developers.google.com/workspace/slides/api/scopes",
  },
  onedrive: {
    console: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
    create: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2",
    scopes: "https://learn.microsoft.com/en-us/onedrive/developer/rest-api/getting-started/graph-oauth",
  },
  google_drive: {
    console: "https://console.cloud.google.com/apis/credentials",
    create: "https://developers.google.com/workspace/guides/create-credentials",
    scopes: "https://developers.google.com/drive/api/guides/api-specific-auth",
  },
  monday_workdocs: {
    console: "https://developer.monday.com/apps/docs/the-developer-center",
    create: "https://developer.monday.com/api-reference/docs/authentication",
    scopes: "https://developer.monday.com/apps/docs/oauth",
  },

  // ---- Developer & issue trackers ----
  github: {
    console: "https://github.com/settings/tokens",
    create: "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens",
    scopes: "https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps",
  },
  gitlab: {
    console: "https://gitlab.com/-/user_settings/personal_access_tokens",
    create: "https://docs.gitlab.com/user/profile/personal_access_tokens/",
    scopes: "https://docs.gitlab.com/user/profile/personal_access_tokens/#personal-access-token-scopes",
  },
  jira: {
    console: "https://id.atlassian.com/manage-profile/security/api-tokens",
    create: "https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/",
    scopes: "https://developer.atlassian.com/cloud/jira/platform/scopes-for-oauth-2-3LO-and-forge-apps/",
  },
  linear: {
    console: "https://linear.app/settings/api",
    create: "https://developers.linear.app/docs/graphql/working-with-the-graphql-api",
    scopes: "https://developers.linear.app/docs/oauth/authentication",
  },
  monday_dev: {
    console: "https://developer.monday.com/apps/docs/the-developer-center",
    create: "https://developer.monday.com/api-reference/docs/authentication",
    scopes: "https://developer.monday.com/apps/docs/oauth",
  },
  bitbucket: {
    console: "https://bitbucket.org/account/settings/app-passwords/",
    create: "https://support.atlassian.com/bitbucket-cloud/docs/create-an-app-password/",
    scopes: "https://developer.atlassian.com/cloud/bitbucket/rest/intro/#scopes",
  },
  azure_devops: {
    console: "https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/oauth",
    create: "https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/oauth",
    scopes: "https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/oauth#scopes",
  },
  asana: {
    console: "https://app.asana.com/0/my-apps",
    create: "https://developers.asana.com/docs/personal-access-token",
    scopes: "https://developers.asana.com/docs/oauth",
  },
  trello: {
    console: "https://trello.com/power-ups/admin",
    create: "https://developer.atlassian.com/cloud/trello/guides/rest-api/api-introduction/",
    scopes: "https://developer.atlassian.com/cloud/trello/guides/rest-api/authorization/",
  },

  // ---- CRM & sales ----
  salesforce: {
    console: "https://help.salesforce.com/s/articleView?id=sf.connected_app_create.htm",
    create: "https://help.salesforce.com/s/articleView?id=sf.connected_app_create_api_integration.htm&type=5",
    scopes: "https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_tokens_scopes.htm&type=5",
  },
  hubspot: {
    console: "https://developers.hubspot.com/docs/guides/apps/private-apps/overview",
    create: "https://developers.hubspot.com/docs/guides/apps/private-apps/overview",
    scopes: "https://developers.hubspot.com/docs/guides/apps/authentication/scopes",
  },
  pipedrive: {
    console: "https://pipedrive.readme.io/docs/how-to-find-the-api-token",
    create: "https://support.pipedrive.com/en/article/how-can-i-find-my-personal-api-key",
    scopes: "https://pipedrive.readme.io/docs/marketplace-scopes-and-permissions-explanations",
  },
  monday_crm: {
    console: "https://developer.monday.com/apps/docs/the-developer-center",
    create: "https://developer.monday.com/api-reference/docs/authentication",
    scopes: "https://developer.monday.com/apps/docs/oauth",
  },
  zoho_crm: {
    console: "https://api-console.zoho.com/",
    create: "https://www.zoho.com/crm/developer/docs/api/v7/register-client.html",
    scopes: "https://www.zoho.com/crm/developer/docs/api/v7/scopes.html",
  },
  close: {
    console: "https://app.close.com/settings/api/",
    create: "https://developer.close.com/topics/authentication/",
    scopes: "https://developer.close.com/",
  },

  // ---- Support & knowledge ----
  zendesk: {
    console: "https://developer.zendesk.com/api-reference/introduction/security-and-auth/",
    create: "https://support.zendesk.com/hc/en-us/articles/8889508417946-Managing-OAuth-token-access-to-the-API",
    scopes: "https://developer.zendesk.com/documentation/authentication/",
  },
  intercom: {
    console: "https://app.intercom.com/a/apps/_/developer-hub",
    create: "https://developers.intercom.com/docs/build-an-integration/learn-more/authentication",
    scopes: "https://developers.intercom.com/docs/build-an-integration/learn-more/authentication/setting-up-oauth",
  },
  freshdesk: {
    console: "https://developers.freshdesk.com/api/",
    create: "https://support.freshdesk.com/en/support/solutions/articles/215517-how-to-find-your-api-key",
    scopes: "https://developers.freshdesk.com/api/#authentication",
  },
  notion: {
    console: "https://www.notion.so/my-integrations",
    create: "https://developers.notion.com/docs/create-a-notion-integration",
    scopes: "https://developers.notion.com/reference/capabilities",
  },
  confluence: {
    console: "https://id.atlassian.com/manage-profile/security/api-tokens",
    create: "https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/",
    scopes: "https://developer.atlassian.com/cloud/confluence/scopes-for-oauth-2-3LO-and-forge-apps/",
  },
  monday_service: {
    console: "https://developer.monday.com/apps/docs/the-developer-center",
    create: "https://developer.monday.com/api-reference/docs/authentication",
    scopes: "https://developer.monday.com/apps/docs/oauth",
  },

  // ---- ERP & financial ----
  quickbooks: {
    console: "https://developer.intuit.com/app/developer/dashboard",
    create: "https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0",
    scopes: "https://developer.intuit.com/app/developer/qbo/docs/learn/scopes",
  },
  xero: {
    console: "https://developer.xero.com/app/manage",
    create: "https://developer.xero.com/documentation/guides/oauth2/auth-flow/",
    scopes: "https://developer.xero.com/documentation/guides/oauth2/scopes/",
  },
  stripe: {
    console: "https://dashboard.stripe.com/apikeys",
    create: "https://docs.stripe.com/keys",
    scopes: "https://docs.stripe.com/keys#limit-access",
  },
  sap: {
    console: "https://api.sap.com/",
    create: "https://help.sap.com/docs/btp/sap-business-technology-platform/creating-service-keys",
    scopes: "https://help.sap.com/docs/btp/sap-business-technology-platform/using-oauth-authorization-code-grant-flow",
  },
  netsuite: {
    console: "https://system.netsuite.com/",
    create: "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_157771733782.html",
    scopes: "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1543865613.html",
  },
  paypal: {
    console: "https://developer.paypal.com/dashboard/applications",
    create: "https://developer.paypal.com/api/rest/#link-getclientidandclientsecret",
    scopes: "https://developer.paypal.com/api/rest/authentication/",
  },

  // ---- Team chat & meetings ----
  slack: {
    console: "https://api.slack.com/apps",
    create: "https://api.slack.com/quickstart",
    scopes: "https://api.slack.com/scopes",
  },
  teams: {
    console: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
    create: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2",
    scopes: "https://learn.microsoft.com/en-us/graph/permissions-reference",
  },
  discord: {
    console: "https://discord.com/developers/applications",
    create: "https://discord.com/developers/docs/quick-start/getting-started",
    scopes: "https://discord.com/developers/docs/topics/oauth2#shared-resources-oauth2-scopes",
  },
  zoom: {
    console: "https://marketplace.zoom.us/develop/create",
    create: "https://developers.zoom.us/docs/integrations/create/",
    scopes: "https://developers.zoom.us/docs/integrations/oauth-scopes/",
  },
  google_meet: {
    console: "https://console.cloud.google.com/apis/credentials",
    create: "https://developers.google.com/workspace/guides/create-credentials",
    scopes: "https://developers.google.com/workspace/meet/api/guides/authenticate-authorize",
  },
  teamviewer: {
    console: "https://webapi.teamviewer.com/api/v1/docs/index",
    create: "https://webapi.teamviewer.com/api/v1/docs/index",
    scopes: "https://webapi.teamviewer.com/api/v1/docs/index#/",
  },
  monday_workspaces: {
    console: "https://developer.monday.com/apps/docs/the-developer-center",
    create: "https://developer.monday.com/api-reference/docs/authentication",
    scopes: "https://developer.monday.com/apps/docs/oauth",
  },

  // ---- Cloud & DevOps ----
  aws: {
    console: "https://console.aws.amazon.com/iam/home#/security_credentials",
    create: "https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html",
    scopes: "https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html",
  },
  gcp: {
    console: "https://console.cloud.google.com/apis/credentials",
    create: "https://cloud.google.com/iam/docs/creating-managing-service-account-keys",
    scopes: "https://developers.google.com/identity/protocols/oauth2/scopes",
  },
  cloudflare: {
    console: "https://dash.cloudflare.com/profile/api-tokens",
    create: "https://developers.cloudflare.com/fundamentals/api/get-started/create-token/",
    scopes: "https://developers.cloudflare.com/fundamentals/api/reference/permissions/",
  },
  terraform: {
    console: "https://app.terraform.io/app/settings/tokens",
    create: "https://developer.hashicorp.com/terraform/cloud-docs/users-teams-organizations/api-tokens",
    scopes: "https://developer.hashicorp.com/terraform/cloud-docs/api-docs#authentication",
  },
  digitalocean: {
    console: "https://cloud.digitalocean.com/account/api/tokens",
    create: "https://docs.digitalocean.com/reference/api/create-personal-access-token/",
    scopes: "https://docs.digitalocean.com/reference/api/scopes/",
  },
  kubernetes: {
    console: "https://kubernetes.io/docs/reference/access-authn-authz/authentication/",
    create: "https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/",
    scopes: "https://kubernetes.io/docs/reference/access-authn-authz/rbac/",
  },

  // ---- HR & recruiting ----
  workday: {
    console: "https://community.workday.com/",
    create: "https://community.workday.com/sites/default/files/file-hosting/restapi/index.html",
    scopes: "https://docs.workday.com/en-us/workday-apis.html",
  },
  bamboohr: {
    console: "https://documentation.bamboohr.com/docs/getting-started",
    create: "https://documentation.bamboohr.com/docs/getting-started#authentication",
    scopes: "https://documentation.bamboohr.com/reference/get-employee",
  },
  greenhouse: {
    console: "https://developers.greenhouse.io/harvest.html",
    create: "https://developers.greenhouse.io/harvest.html#authentication",
    scopes: "https://developers.greenhouse.io/harvest.html#permissions",
  },
  lever: {
    console: "https://hire.lever.co/settings/integrations",
    create: "https://hire.lever.co/developer/documentation#authentication",
    scopes: "https://hire.lever.co/developer/documentation#oauth-scopes",
  },

  // ---- Calendar & scheduling ----
  google_calendar: {
    console: "https://console.cloud.google.com/apis/credentials",
    create: "https://developers.google.com/workspace/guides/create-credentials",
    scopes: "https://developers.google.com/calendar/api/auth",
  },
  outlook_calendar: {
    console: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
    create: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2",
    scopes: "https://learn.microsoft.com/en-us/graph/permissions-reference",
  },
  calendly: {
    console: "https://calendly.com/integrations/api_webhooks",
    create: "https://developer.calendly.com/getting-started",
    scopes: "https://developer.calendly.com/api-docs/",
  },
};

/**
 * Full per-provider step overrides.
 *
 * Some providers need more than the generic four-step OAuth flow. When a
 * provider appears here, these steps replace the generated ones verbatim so the
 * guidance is exact (e.g. Gmail, where the correct credential is an OAuth client
 * ID — not an API key — and a Google Cloud project must exist first). Every step
 * still carries its own clickable link (BUILD.md).
 */
const STEP_OVERRIDES: Record<string, GuideStep[]> = {
  gmail: [
    {
      title: "Create a Google Cloud project first",
      detail:
        "Gmail credentials live inside a Google Cloud project. If you don't have one yet, create a project (name it something like Atomic AI). You'll select this project in the next steps.",
      link: { label: "Create a Google Cloud project", url: "https://console.cloud.google.com/projectcreate" },
    },
    {
      title: "Enable the Gmail API for that project",
      detail:
        "With your project selected, open the Gmail API page and click Enable so the project is allowed to call Gmail.",
      link: { label: "Enable the Gmail API", url: "https://console.cloud.google.com/apis/library/gmail.googleapis.com" },
    },
    {
      title: "Configure the OAuth consent screen",
      detail:
        "Set up the consent screen (app name, support email, and audience). Gmail is per-user data, so users must consent - this screen is what they'll see.",
      link: { label: "Configure OAuth consent", url: "https://console.cloud.google.com/auth/overview" },
    },
    {
      title: "Create an OAuth client ID (not an API key)",
      detail:
        "Do NOT create an API key - an API key cannot access a user's mailbox. Under Clients, click Create client, choose application type Web application, and add your redirect URI. This gives you a Client ID and Client Secret.",
      link: { label: "Create an OAuth client ID", url: "https://console.cloud.google.com/auth/clients" },
    },
    {
      title: "Pick the Gmail scopes your automations need",
      detail:
        "On the Data Access / scopes screen, use the filter to select Gmail API, then check the scopes you need. For most automations pick .../auth/gmail.modify - \u201cRead, compose, and send emails from your Gmail account\u201d. Use gmail.readonly for read-only, or gmail.send to only send. Avoid the full https://mail.google.com/ scope (it also permanently deletes mail). Keep scopes minimal.",
      link: { label: "Gmail API scopes reference", url: "https://developers.google.com/gmail/api/auth/scopes" },
    },
    {
      title: "Get an access token (from the OAuth flow, not the console)",
      detail:
        "The console only gives you a Client ID and Client Secret - there is no access token to copy there. The access token (and refresh token) are issued per user: your app sends the user to Google's consent screen, they approve, Google returns a code, and your backend exchanges that code + your Client ID/Secret for the tokens. In Atomic AI, click Connect below to run that flow and obtain the token automatically.",
      link: { label: "How OAuth issues tokens (Google)", url: "https://developers.google.com/identity/protocols/oauth2/web-server" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "After the consent flow completes, the access token and refresh token are stored for you (or paste them into the form below, choosing Personal or Shared). To wire Gmail's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  google_docs: [
    {
      title: "Create a Google Cloud project first",
      detail:
        "Google Docs credentials live inside a Google Cloud project. If you don't have one, create a project (name it e.g. Atomic AI). You'll select it in the next steps.",
      link: { label: "Create a Google Cloud project", url: "https://console.cloud.google.com/projectcreate" },
    },
    {
      title: "Enable the Google Docs API",
      detail:
        "With your project selected, open the Google Docs API page and click Enable.",
      link: { label: "Enable the Google Docs API", url: "https://console.cloud.google.com/apis/library/docs.googleapis.com" },
    },
    {
      title: "Configure the OAuth consent screen",
      detail:
        "Set up the consent screen (app name, support email, audience). This is per-user data, so users must consent.",
      link: { label: "Configure OAuth consent", url: "https://console.cloud.google.com/auth/overview" },
    },
    {
      title: "Create an OAuth client ID (not an API key)",
      detail:
        "Do NOT create an API key - it can't access user data. Under Clients, click Create client, choose Web application, and add your redirect URI. You'll get a Client ID and Client Secret.",
      link: { label: "Create an OAuth client ID", url: "https://console.cloud.google.com/auth/clients" },
    },
    {
      title: "Pick the Google Docs scopes you need",
      detail:
        "Add the scopes your agents need - e.g. docs.readonly / documents. Keep scopes minimal.",
      link: { label: "Google Docs scopes reference", url: "https://developers.google.com/docs/api/auth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Google Docs's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  google_sheets: [
    {
      title: "Create a Google Cloud project first",
      detail:
        "Google Sheets credentials live inside a Google Cloud project. If you don't have one, create a project (name it e.g. Atomic AI). You'll select it in the next steps.",
      link: { label: "Create a Google Cloud project", url: "https://console.cloud.google.com/projectcreate" },
    },
    {
      title: "Enable the Google Sheets API",
      detail:
        "With your project selected, open the Google Sheets API page and click Enable.",
      link: { label: "Enable the Google Sheets API", url: "https://console.cloud.google.com/apis/library/sheets.googleapis.com" },
    },
    {
      title: "Configure the OAuth consent screen",
      detail:
        "Set up the consent screen (app name, support email, audience). This is per-user data, so users must consent.",
      link: { label: "Configure OAuth consent", url: "https://console.cloud.google.com/auth/overview" },
    },
    {
      title: "Create an OAuth client ID (not an API key)",
      detail:
        "Do NOT create an API key - it can't access user data. Under Clients, click Create client, choose Web application, and add your redirect URI. You'll get a Client ID and Client Secret.",
      link: { label: "Create an OAuth client ID", url: "https://console.cloud.google.com/auth/clients" },
    },
    {
      title: "Pick the Google Sheets scopes you need",
      detail:
        "Add the scopes your agents need - e.g. spreadsheets / spreadsheets.readonly. Keep scopes minimal.",
      link: { label: "Google Sheets scopes reference", url: "https://developers.google.com/sheets/api/scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Google Sheets's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  google_slides: [
    {
      title: "Create a Google Cloud project first",
      detail:
        "Google Slides credentials live inside a Google Cloud project. If you don't have one, create a project (name it e.g. Atomic AI). You'll select it in the next steps.",
      link: { label: "Create a Google Cloud project", url: "https://console.cloud.google.com/projectcreate" },
    },
    {
      title: "Enable the Google Slides API",
      detail:
        "With your project selected, open the Google Slides API page and click Enable.",
      link: { label: "Enable the Google Slides API", url: "https://console.cloud.google.com/apis/library/slides.googleapis.com" },
    },
    {
      title: "Configure the OAuth consent screen",
      detail:
        "Set up the consent screen (app name, support email, audience). This is per-user data, so users must consent.",
      link: { label: "Configure OAuth consent", url: "https://console.cloud.google.com/auth/overview" },
    },
    {
      title: "Create an OAuth client ID (not an API key)",
      detail:
        "Do NOT create an API key - it can't access user data. Under Clients, click Create client, choose Web application, and add your redirect URI. You'll get a Client ID and Client Secret.",
      link: { label: "Create an OAuth client ID", url: "https://console.cloud.google.com/auth/clients" },
    },
    {
      title: "Pick the Google Slides scopes you need",
      detail:
        "Add the scopes your agents need - e.g. presentations / presentations.readonly. Keep scopes minimal.",
      link: { label: "Google Slides scopes reference", url: "https://developers.google.com/workspace/slides/api/scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Google Slides's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  google_drive: [
    {
      title: "Create a Google Cloud project first",
      detail:
        "Google Drive credentials live inside a Google Cloud project. If you don't have one, create a project (name it e.g. Atomic AI). You'll select it in the next steps.",
      link: { label: "Create a Google Cloud project", url: "https://console.cloud.google.com/projectcreate" },
    },
    {
      title: "Enable the Google Drive API",
      detail:
        "With your project selected, open the Google Drive API page and click Enable.",
      link: { label: "Enable the Google Drive API", url: "https://console.cloud.google.com/apis/library/drive.googleapis.com" },
    },
    {
      title: "Configure the OAuth consent screen",
      detail:
        "Set up the consent screen (app name, support email, audience). This is per-user data, so users must consent.",
      link: { label: "Configure OAuth consent", url: "https://console.cloud.google.com/auth/overview" },
    },
    {
      title: "Create an OAuth client ID (not an API key)",
      detail:
        "Do NOT create an API key - it can't access user data. Under Clients, click Create client, choose Web application, and add your redirect URI. You'll get a Client ID and Client Secret.",
      link: { label: "Create an OAuth client ID", url: "https://console.cloud.google.com/auth/clients" },
    },
    {
      title: "Pick the Google Drive scopes you need",
      detail:
        "Add the scopes your agents need - e.g. drive.file / drive.readonly. Keep scopes minimal.",
      link: { label: "Google Drive scopes reference", url: "https://developers.google.com/drive/api/guides/api-specific-auth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Google Drive's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  google_calendar: [
    {
      title: "Create a Google Cloud project first",
      detail:
        "Google Calendar credentials live inside a Google Cloud project. If you don't have one, create a project (name it e.g. Atomic AI). You'll select it in the next steps.",
      link: { label: "Create a Google Cloud project", url: "https://console.cloud.google.com/projectcreate" },
    },
    {
      title: "Enable the Google Calendar API",
      detail:
        "With your project selected, open the Google Calendar API page and click Enable.",
      link: { label: "Enable the Google Calendar API", url: "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" },
    },
    {
      title: "Configure the OAuth consent screen",
      detail:
        "Set up the consent screen (app name, support email, audience). This is per-user data, so users must consent.",
      link: { label: "Configure OAuth consent", url: "https://console.cloud.google.com/auth/overview" },
    },
    {
      title: "Create an OAuth client ID (not an API key)",
      detail:
        "Do NOT create an API key - it can't access user data. Under Clients, click Create client, choose Web application, and add your redirect URI. You'll get a Client ID and Client Secret.",
      link: { label: "Create an OAuth client ID", url: "https://console.cloud.google.com/auth/clients" },
    },
    {
      title: "Pick the Google Calendar scopes you need",
      detail:
        "Add the scopes your agents need - e.g. calendar / calendar.events. Keep scopes minimal.",
      link: { label: "Google Calendar scopes reference", url: "https://developers.google.com/calendar/api/auth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Google Calendar's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  google_meet: [
    {
      title: "Create a Google Cloud project first",
      detail:
        "Google Meet credentials live inside a Google Cloud project. If you don't have one, create a project (name it e.g. Atomic AI). You'll select it in the next steps.",
      link: { label: "Create a Google Cloud project", url: "https://console.cloud.google.com/projectcreate" },
    },
    {
      title: "Enable the Google Meet API",
      detail:
        "With your project selected, open the Google Meet API page and click Enable.",
      link: { label: "Enable the Google Meet API", url: "https://console.cloud.google.com/apis/library/meet.googleapis.com" },
    },
    {
      title: "Configure the OAuth consent screen",
      detail:
        "Set up the consent screen (app name, support email, audience). This is per-user data, so users must consent.",
      link: { label: "Configure OAuth consent", url: "https://console.cloud.google.com/auth/overview" },
    },
    {
      title: "Create an OAuth client ID (not an API key)",
      detail:
        "Do NOT create an API key - it can't access user data. Under Clients, click Create client, choose Web application, and add your redirect URI. You'll get a Client ID and Client Secret.",
      link: { label: "Create an OAuth client ID", url: "https://console.cloud.google.com/auth/clients" },
    },
    {
      title: "Pick the Google Meet scopes you need",
      detail:
        "Add the scopes your agents need - e.g. meetings.space.created / meetings.space.readonly. Keep scopes minimal.",
      link: { label: "Google Meet scopes reference", url: "https://developers.google.com/workspace/meet/api/guides/authenticate-authorize" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Google Meet's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  gcp: [
    {
      title: "Create or select a Google Cloud project",
      detail:
        "Create a project (or pick an existing one) that your agents will manage resources in.",
      link: { label: "Create a Google Cloud project", url: "https://console.cloud.google.com/projectcreate" },
    },
    {
      title: "Create a service account",
      detail:
        "In IAM & Admin then Service Accounts, create a service account for Atomic AI and grant it only the roles it needs.",
      link: { label: "Create a service account", url: "https://console.cloud.google.com/iam-admin/serviceaccounts" },
    },
    {
      title: "Generate a service account key",
      detail:
        "Open the service account then Keys then Add key then Create new key (JSON). Download the key file securely.",
      link: { label: "Service account keys guide", url: "https://cloud.google.com/iam/docs/creating-managing-service-account-keys" },
    },
    {
      title: "Grant least-privilege roles",
      detail:
        "Assign minimal IAM roles for the actions your agents perform. Avoid Owner/Editor on production projects.",
      link: { label: "IAM scopes & roles", url: "https://developers.google.com/identity/protocols/oauth2/scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Google Cloud's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  outlook: [
    {
      title: "Open Microsoft Entra App registrations",
      detail:
        "Sign in to the Azure portal and open Microsoft Entra ID then App registrations. This is where Microsoft Outlook apps are registered (Microsoft Graph).",
      link: { label: "Open App registrations", url: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps" },
    },
    {
      title: "Register a new application",
      detail:
        "Click New registration, name it Atomic AI, choose the account types, set the Redirect URI to Web with your callback URL, then Register. Copy the Application (client) ID and Directory (tenant) ID.",
      link: { label: "App registration guide", url: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2" },
    },
    {
      title: "Create a client secret",
      detail:
        "In the app then Certificates & secrets then New client secret. Copy the secret Value immediately (shown once). This is your Client Secret.",
      link: { label: "Add a client secret", url: "https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app" },
    },
    {
      title: "Add Microsoft Graph permissions for Microsoft Outlook",
      detail:
        "In API permissions then Add a permission then Microsoft Graph, add the delegated permissions you need - e.g. Mail.Read / Mail.Send - then Grant admin consent if required.",
      link: { label: "Graph permissions reference", url: "https://learn.microsoft.com/en-us/graph/permissions-reference" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Microsoft Outlook's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  outlook_calendar: [
    {
      title: "Open Microsoft Entra App registrations",
      detail:
        "Sign in to the Azure portal and open Microsoft Entra ID then App registrations. This is where Outlook Calendar apps are registered (Microsoft Graph).",
      link: { label: "Open App registrations", url: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps" },
    },
    {
      title: "Register a new application",
      detail:
        "Click New registration, name it Atomic AI, choose the account types, set the Redirect URI to Web with your callback URL, then Register. Copy the Application (client) ID and Directory (tenant) ID.",
      link: { label: "App registration guide", url: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2" },
    },
    {
      title: "Create a client secret",
      detail:
        "In the app then Certificates & secrets then New client secret. Copy the secret Value immediately (shown once). This is your Client Secret.",
      link: { label: "Add a client secret", url: "https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app" },
    },
    {
      title: "Add Microsoft Graph permissions for Outlook Calendar",
      detail:
        "In API permissions then Add a permission then Microsoft Graph, add the delegated permissions you need - e.g. Calendars.ReadWrite - then Grant admin consent if required.",
      link: { label: "Graph permissions reference", url: "https://learn.microsoft.com/en-us/graph/permissions-reference" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Outlook Calendar's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  onedrive: [
    {
      title: "Open Microsoft Entra App registrations",
      detail:
        "Sign in to the Azure portal and open Microsoft Entra ID then App registrations. This is where OneDrive apps are registered (Microsoft Graph).",
      link: { label: "Open App registrations", url: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps" },
    },
    {
      title: "Register a new application",
      detail:
        "Click New registration, name it Atomic AI, choose the account types, set the Redirect URI to Web with your callback URL, then Register. Copy the Application (client) ID and Directory (tenant) ID.",
      link: { label: "App registration guide", url: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2" },
    },
    {
      title: "Create a client secret",
      detail:
        "In the app then Certificates & secrets then New client secret. Copy the secret Value immediately (shown once). This is your Client Secret.",
      link: { label: "Add a client secret", url: "https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app" },
    },
    {
      title: "Add Microsoft Graph permissions for OneDrive",
      detail:
        "In API permissions then Add a permission then Microsoft Graph, add the delegated permissions you need - e.g. Files.ReadWrite / Files.Read.All - then Grant admin consent if required.",
      link: { label: "Graph permissions reference", url: "https://learn.microsoft.com/en-us/onedrive/developer/rest-api/getting-started/graph-oauth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire OneDrive's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  teams: [
    {
      title: "Open Microsoft Entra App registrations",
      detail:
        "Sign in to the Azure portal and open Microsoft Entra ID then App registrations. This is where Microsoft Teams apps are registered (Microsoft Graph).",
      link: { label: "Open App registrations", url: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps" },
    },
    {
      title: "Register a new application",
      detail:
        "Click New registration, name it Atomic AI, choose the account types, set the Redirect URI to Web with your callback URL, then Register. Copy the Application (client) ID and Directory (tenant) ID.",
      link: { label: "App registration guide", url: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2" },
    },
    {
      title: "Create a client secret",
      detail:
        "In the app then Certificates & secrets then New client secret. Copy the secret Value immediately (shown once). This is your Client Secret.",
      link: { label: "Add a client secret", url: "https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app" },
    },
    {
      title: "Add Microsoft Graph permissions for Microsoft Teams",
      detail:
        "In API permissions then Add a permission then Microsoft Graph, add the delegated permissions you need - e.g. Chat.ReadWrite / ChannelMessage.Send - then Grant admin consent if required.",
      link: { label: "Graph permissions reference", url: "https://learn.microsoft.com/en-us/graph/permissions-reference" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Microsoft Teams's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  word: [
    {
      title: "Open Microsoft Entra App registrations",
      detail:
        "Sign in to the Azure portal and open Microsoft Entra ID then App registrations. This is where Microsoft Word apps are registered (Microsoft Graph).",
      link: { label: "Open App registrations", url: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps" },
    },
    {
      title: "Register a new application",
      detail:
        "Click New registration, name it Atomic AI, choose the account types, set the Redirect URI to Web with your callback URL, then Register. Copy the Application (client) ID and Directory (tenant) ID.",
      link: { label: "App registration guide", url: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2" },
    },
    {
      title: "Create a client secret",
      detail:
        "In the app then Certificates & secrets then New client secret. Copy the secret Value immediately (shown once). This is your Client Secret.",
      link: { label: "Add a client secret", url: "https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app" },
    },
    {
      title: "Add Microsoft Graph permissions for Microsoft Word",
      detail:
        "In API permissions then Add a permission then Microsoft Graph, add the delegated permissions you need - e.g. Files.ReadWrite.All - then Grant admin consent if required.",
      link: { label: "Graph permissions reference", url: "https://learn.microsoft.com/en-us/graph/permissions-reference" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Microsoft Word's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  excel: [
    {
      title: "Open Microsoft Entra App registrations",
      detail:
        "Sign in to the Azure portal and open Microsoft Entra ID then App registrations. This is where Microsoft Excel apps are registered (Microsoft Graph).",
      link: { label: "Open App registrations", url: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps" },
    },
    {
      title: "Register a new application",
      detail:
        "Click New registration, name it Atomic AI, choose the account types, set the Redirect URI to Web with your callback URL, then Register. Copy the Application (client) ID and Directory (tenant) ID.",
      link: { label: "App registration guide", url: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2" },
    },
    {
      title: "Create a client secret",
      detail:
        "In the app then Certificates & secrets then New client secret. Copy the secret Value immediately (shown once). This is your Client Secret.",
      link: { label: "Add a client secret", url: "https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app" },
    },
    {
      title: "Add Microsoft Graph permissions for Microsoft Excel",
      detail:
        "In API permissions then Add a permission then Microsoft Graph, add the delegated permissions you need - e.g. Files.ReadWrite.All - then Grant admin consent if required.",
      link: { label: "Graph permissions reference", url: "https://learn.microsoft.com/en-us/graph/permissions-reference" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Microsoft Excel's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  powerpoint: [
    {
      title: "Open Microsoft Entra App registrations",
      detail:
        "Sign in to the Azure portal and open Microsoft Entra ID then App registrations. This is where Microsoft PowerPoint apps are registered (Microsoft Graph).",
      link: { label: "Open App registrations", url: "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps" },
    },
    {
      title: "Register a new application",
      detail:
        "Click New registration, name it Atomic AI, choose the account types, set the Redirect URI to Web with your callback URL, then Register. Copy the Application (client) ID and Directory (tenant) ID.",
      link: { label: "App registration guide", url: "https://learn.microsoft.com/en-us/graph/auth-register-app-v2" },
    },
    {
      title: "Create a client secret",
      detail:
        "In the app then Certificates & secrets then New client secret. Copy the secret Value immediately (shown once). This is your Client Secret.",
      link: { label: "Add a client secret", url: "https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app" },
    },
    {
      title: "Add Microsoft Graph permissions for Microsoft PowerPoint",
      detail:
        "In API permissions then Add a permission then Microsoft Graph, add the delegated permissions you need - e.g. Files.ReadWrite.All - then Grant admin consent if required.",
      link: { label: "Graph permissions reference", url: "https://learn.microsoft.com/en-us/graph/permissions-reference" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Microsoft PowerPoint's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  jira: [
    {
      title: "Open Atlassian API tokens",
      detail:
        "Sign in to your Atlassian account and open the API tokens page. Jira Cloud authenticates with your email plus an API token (Basic auth).",
      link: { label: "Open API tokens", url: "https://id.atlassian.com/manage-profile/security/api-tokens" },
    },
    {
      title: "Create an API token",
      detail:
        "Click Create API token, give it a label like Atomic AI, and copy the token - it's shown only once.",
      link: { label: "Manage API tokens guide", url: "https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/" },
    },
    {
      title: "Note your site and permissions",
      detail:
        "Your Jira access equals your account's permissions. For OAuth apps instead, see the scopes reference for granular access.",
      link: { label: "Scopes & permissions", url: "https://developer.atlassian.com/cloud/jira/platform/scopes-for-oauth-2-3LO-and-forge-apps/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the API token below (use your account email as the username in the API call), choosing Personal or Shared. Then follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  confluence: [
    {
      title: "Open Atlassian API tokens",
      detail:
        "Sign in to your Atlassian account and open the API tokens page. Confluence Cloud authenticates with your email plus an API token (Basic auth).",
      link: { label: "Open API tokens", url: "https://id.atlassian.com/manage-profile/security/api-tokens" },
    },
    {
      title: "Create an API token",
      detail:
        "Click Create API token, give it a label like Atomic AI, and copy the token - it's shown only once.",
      link: { label: "Manage API tokens guide", url: "https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/" },
    },
    {
      title: "Note your site and permissions",
      detail:
        "Your Confluence access equals your account's permissions. For OAuth apps instead, see the scopes reference for granular access.",
      link: { label: "Scopes & permissions", url: "https://developer.atlassian.com/cloud/confluence/scopes-for-oauth-2-3LO-and-forge-apps/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the API token below (use your account email as the username in the API call), choosing Personal or Shared. Then follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  bitbucket: [
    {
      title: "Open Bitbucket app passwords",
      detail:
        "Sign in to Bitbucket and open Personal settings then App passwords.",
      link: { label: "Open app passwords", url: "https://bitbucket.org/account/settings/app-passwords/" },
    },
    {
      title: "Create an app password",
      detail:
        "Click Create app password, label it Atomic AI, select the permissions you need (e.g. Repositories: Read/Write), and copy it - shown once.",
      link: { label: "Create an app password", url: "https://support.atlassian.com/bitbucket-cloud/docs/create-an-app-password/" },
    },
    {
      title: "Review the scopes you granted",
      detail:
        "Grant only the permissions your agents need. See the scopes reference for what each grants.",
      link: { label: "Scopes reference", url: "https://developer.atlassian.com/cloud/bitbucket/rest/intro/#scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Bitbucket's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  trello: [
    {
      title: "Open the Trello Power-Up admin",
      detail:
        "Sign in to Trello and open the Power-Up admin to get your API key.",
      link: { label: "Open Trello admin", url: "https://trello.com/power-ups/admin" },
    },
    {
      title: "Get your API key and token",
      detail:
        "Generate an API key, then generate a token authorizing your account. You'll use both (key plus token).",
      link: { label: "REST API intro", url: "https://developer.atlassian.com/cloud/trello/guides/rest-api/api-introduction/" },
    },
    {
      title: "Authorize the scopes you need",
      detail:
        "When generating the token, request read/write scopes for the boards your agents will act on.",
      link: { label: "Authorization & scopes", url: "https://developer.atlassian.com/cloud/trello/guides/rest-api/authorization/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Trello's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  github: [
    {
      title: "Open GitHub token settings",
      detail:
        "Sign in to GitHub and open Settings then Developer settings then Personal access tokens.",
      link: { label: "Open token settings", url: "https://github.com/settings/tokens" },
    },
    {
      title: "Generate a personal access token",
      detail:
        "Click Generate new token (fine-grained recommended), name it Atomic AI, set an expiry, and select the repositories it can access.",
      link: { label: "Manage personal access tokens", url: "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens" },
    },
    {
      title: "Select the scopes/permissions",
      detail:
        "Grant only what your agents need - e.g. contents, issues, pull requests. Copy the token - shown once.",
      link: { label: "Token scopes reference", url: "https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire GitHub's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  gitlab: [
    {
      title: "Open GitLab access tokens",
      detail:
        "Sign in to GitLab and open User settings then Access tokens.",
      link: { label: "Open access tokens", url: "https://gitlab.com/-/user_settings/personal_access_tokens" },
    },
    {
      title: "Add a personal access token",
      detail:
        "Click Add new token, name it Atomic AI, set an expiry, then select scopes.",
      link: { label: "Personal access tokens guide", url: "https://docs.gitlab.com/user/profile/personal_access_tokens/" },
    },
    {
      title: "Choose token scopes",
      detail:
        "Select minimal scopes - e.g. api or read_api, read_repository/write_repository. Copy the token - shown once.",
      link: { label: "Token scopes reference", url: "https://docs.gitlab.com/user/profile/personal_access_tokens/#personal-access-token-scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire GitLab's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  linear: [
    {
      title: "Open Linear API settings",
      detail:
        "Sign in to Linear and open Settings then API.",
      link: { label: "Open Linear API settings", url: "https://linear.app/settings/api" },
    },
    {
      title: "Create a personal API key",
      detail:
        "Under Personal API keys, click Create key, label it Atomic AI, and copy it.",
      link: { label: "Working with the API", url: "https://developers.linear.app/docs/graphql/working-with-the-graphql-api" },
    },
    {
      title: "Understand access & scopes",
      detail:
        "Personal API keys act as your user. For scoped access use OAuth; see the auth docs.",
      link: { label: "OAuth & scopes", url: "https://developers.linear.app/docs/oauth/authentication" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Linear's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  asana: [
    {
      title: "Open Asana developer console",
      detail:
        "Sign in to Asana and open My apps in the developer console.",
      link: { label: "Open My apps", url: "https://app.asana.com/0/my-apps" },
    },
    {
      title: "Create a personal access token",
      detail:
        "Click Create new token, name it Atomic AI, and copy it - shown once.",
      link: { label: "Personal access token guide", url: "https://developers.asana.com/docs/personal-access-token" },
    },
    {
      title: "Review OAuth scopes (optional)",
      detail:
        "PATs act as your user. For granular access, register an OAuth app; see the scopes docs.",
      link: { label: "OAuth & scopes", url: "https://developers.asana.com/docs/oauth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Asana's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  azure_devops: [
    {
      title: "Open Azure DevOps user settings",
      detail:
        "Sign in to your Azure DevOps organization and open User settings then Personal access tokens.",
      link: { label: "OAuth & auth guide", url: "https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/oauth" },
    },
    {
      title: "Create a personal access token",
      detail:
        "Click New Token, name it Atomic AI, choose your organization and expiry.",
      link: { label: "Authentication guide", url: "https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/oauth" },
    },
    {
      title: "Select scopes",
      detail:
        "Grant minimal scopes (e.g. Work Items Read/Write, Code Read). Copy the token - shown once.",
      link: { label: "Token scopes", url: "https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/oauth#scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Azure DevOps's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  slack: [
    {
      title: "Open the Slack apps dashboard",
      detail:
        "Go to the Slack API apps page and click Create New App then From scratch, then pick your workspace.",
      link: { label: "Open Slack apps", url: "https://api.slack.com/apps" },
    },
    {
      title: "Add bot token scopes",
      detail:
        "In OAuth & Permissions then Scopes then Bot Token Scopes, add what you need (e.g. chat:write, channels:read).",
      link: { label: "App setup basics", url: "https://api.slack.com/quickstart" },
    },
    {
      title: "Install the app to your workspace",
      detail:
        "Click Install to Workspace and approve. Copy the Bot User OAuth Token (starts with xoxb-).",
      link: { label: "OAuth scopes reference", url: "https://api.slack.com/scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Slack's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  discord: [
    {
      title: "Open the Discord developer portal",
      detail:
        "Sign in and open the Applications page, then click New Application.",
      link: { label: "Open applications", url: "https://discord.com/developers/applications" },
    },
    {
      title: "Add a bot and copy its token",
      detail:
        "In your app then Bot then Add Bot, then Reset Token and copy the bot token.",
      link: { label: "Getting started", url: "https://discord.com/developers/docs/quick-start/getting-started" },
    },
    {
      title: "Select OAuth2 scopes",
      detail:
        "Under OAuth2, select scopes (e.g. bot, applications.commands) and bot permissions, then invite the bot to your server.",
      link: { label: "OAuth2 scopes", url: "https://discord.com/developers/docs/topics/oauth2#shared-resources-oauth2-scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Discord's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  zoom: [
    {
      title: "Open the Zoom App Marketplace",
      detail:
        "Sign in and go to Develop then Build App. Choose Server-to-Server OAuth for backend automation.",
      link: { label: "Create an app", url: "https://marketplace.zoom.us/develop/create" },
    },
    {
      title: "Create the app and get credentials",
      detail:
        "Create the app, then copy the Account ID, Client ID, and Client Secret from its App Credentials.",
      link: { label: "Create-app guide", url: "https://developers.zoom.us/docs/integrations/create/" },
    },
    {
      title: "Add scopes",
      detail:
        "Under Scopes, add what your agents need (e.g. meeting:write, user:read), then Activate the app.",
      link: { label: "OAuth scopes", url: "https://developers.zoom.us/docs/integrations/oauth-scopes/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Zoom's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  teamviewer: [
    {
      title: "Open TeamViewer Management Console",
      detail:
        "Sign in to the TeamViewer Management Console then Edit profile then Apps.",
      link: { label: "Web API docs", url: "https://webapi.teamviewer.com/api/v1/docs/index" },
    },
    {
      title: "Create a script token",
      detail:
        "Create a new app/script token with the permissions you need and copy it.",
      link: { label: "Create a token", url: "https://webapi.teamviewer.com/api/v1/docs/index" },
    },
    {
      title: "Review permissions",
      detail:
        "Grant only needed permissions (e.g. account, devices).",
      link: { label: "API reference", url: "https://webapi.teamviewer.com/api/v1/docs/index#/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire TeamViewer's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  monday_workdocs: [
    {
      title: "Open the monday.com Developer Center",
      detail:
        "Sign in to monday.com and open the Developer Center. This is where monday WorkDocs apps and tokens live.",
      link: { label: "Open Developer Center", url: "https://developer.monday.com/apps/docs/the-developer-center" },
    },
    {
      title: "Get a token or build an app",
      detail:
        "For quick access, copy your personal API token (Developers then My Access Tokens). For distribution, create an app with OAuth.",
      link: { label: "Authentication guide", url: "https://developer.monday.com/api-reference/docs/authentication" },
    },
    {
      title: "Set OAuth permission scopes",
      detail:
        "If using an app, set scopes (e.g. boards:read, boards:write) so access is least-privilege.",
      link: { label: "OAuth scopes & permissions", url: "https://developer.monday.com/apps/docs/oauth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire monday WorkDocs's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  monday_dev: [
    {
      title: "Open the monday.com Developer Center",
      detail:
        "Sign in to monday.com and open the Developer Center. This is where monday dev apps and tokens live.",
      link: { label: "Open Developer Center", url: "https://developer.monday.com/apps/docs/the-developer-center" },
    },
    {
      title: "Get a token or build an app",
      detail:
        "For quick access, copy your personal API token (Developers then My Access Tokens). For distribution, create an app with OAuth.",
      link: { label: "Authentication guide", url: "https://developer.monday.com/api-reference/docs/authentication" },
    },
    {
      title: "Set OAuth permission scopes",
      detail:
        "If using an app, set scopes (e.g. boards:read, boards:write) so access is least-privilege.",
      link: { label: "OAuth scopes & permissions", url: "https://developer.monday.com/apps/docs/oauth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire monday dev's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  monday_crm: [
    {
      title: "Open the monday.com Developer Center",
      detail:
        "Sign in to monday.com and open the Developer Center. This is where monday sales CRM apps and tokens live.",
      link: { label: "Open Developer Center", url: "https://developer.monday.com/apps/docs/the-developer-center" },
    },
    {
      title: "Get a token or build an app",
      detail:
        "For quick access, copy your personal API token (Developers then My Access Tokens). For distribution, create an app with OAuth.",
      link: { label: "Authentication guide", url: "https://developer.monday.com/api-reference/docs/authentication" },
    },
    {
      title: "Set OAuth permission scopes",
      detail:
        "If using an app, set scopes (e.g. boards:read, boards:write) so access is least-privilege.",
      link: { label: "OAuth scopes & permissions", url: "https://developer.monday.com/apps/docs/oauth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire monday sales CRM's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  monday_service: [
    {
      title: "Open the monday.com Developer Center",
      detail:
        "Sign in to monday.com and open the Developer Center. This is where monday service apps and tokens live.",
      link: { label: "Open Developer Center", url: "https://developer.monday.com/apps/docs/the-developer-center" },
    },
    {
      title: "Get a token or build an app",
      detail:
        "For quick access, copy your personal API token (Developers then My Access Tokens). For distribution, create an app with OAuth.",
      link: { label: "Authentication guide", url: "https://developer.monday.com/api-reference/docs/authentication" },
    },
    {
      title: "Set OAuth permission scopes",
      detail:
        "If using an app, set scopes (e.g. boards:read, boards:write) so access is least-privilege.",
      link: { label: "OAuth scopes & permissions", url: "https://developer.monday.com/apps/docs/oauth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire monday service's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  monday_workspaces: [
    {
      title: "Open the monday.com Developer Center",
      detail:
        "Sign in to monday.com and open the Developer Center. This is where monday Workspaces apps and tokens live.",
      link: { label: "Open Developer Center", url: "https://developer.monday.com/apps/docs/the-developer-center" },
    },
    {
      title: "Get a token or build an app",
      detail:
        "For quick access, copy your personal API token (Developers then My Access Tokens). For distribution, create an app with OAuth.",
      link: { label: "Authentication guide", url: "https://developer.monday.com/api-reference/docs/authentication" },
    },
    {
      title: "Set OAuth permission scopes",
      detail:
        "If using an app, set scopes (e.g. boards:read, boards:write) so access is least-privilege.",
      link: { label: "OAuth scopes & permissions", url: "https://developer.monday.com/apps/docs/oauth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire monday Workspaces's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  facebook: [
    {
      title: "Open the Meta for Developers app dashboard",
      detail:
        "Sign in and open your apps. Facebook uses a Meta app plus access token.",
      link: { label: "Open Meta apps", url: "https://developers.facebook.com/apps/" },
    },
    {
      title: "Create an app and add the product",
      detail:
        "Click Create App, then add the product for Facebook and follow its setup to get an access token.",
      link: { label: "Create-app / setup guide", url: "https://developers.facebook.com/docs/development/create-an-app/" },
    },
    {
      title: "Request the permissions you need",
      detail:
        "Add permissions such as pages_manage_posts / pages_read_engagement. New permissions require App Review before production use.",
      link: { label: "Permissions reference", url: "https://developers.facebook.com/docs/permissions" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Facebook's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  instagram: [
    {
      title: "Open the Meta for Developers app dashboard",
      detail:
        "Sign in and open your apps. Instagram uses a Meta app plus access token.",
      link: { label: "Open Meta apps", url: "https://developers.facebook.com/apps/" },
    },
    {
      title: "Create an app and add the product",
      detail:
        "Click Create App, then add the product for Instagram and follow its setup to get an access token.",
      link: { label: "Create-app / setup guide", url: "https://developers.facebook.com/docs/instagram-platform" },
    },
    {
      title: "Request the permissions you need",
      detail:
        "Add permissions such as instagram_basic / instagram_manage_messages. New permissions require App Review before production use.",
      link: { label: "Permissions reference", url: "https://developers.facebook.com/docs/permissions" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Instagram's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  whatsapp: [
    {
      title: "Open the Meta for Developers app dashboard",
      detail:
        "Sign in and open your apps. WhatsApp Business uses a Meta app plus access token.",
      link: { label: "Open Meta apps", url: "https://developers.facebook.com/apps/" },
    },
    {
      title: "Create an app and add the product",
      detail:
        "Click Create App, then add the product for WhatsApp Business and follow its setup to get an access token.",
      link: { label: "Create-app / setup guide", url: "https://developers.facebook.com/docs/whatsapp/cloud-api/get-started" },
    },
    {
      title: "Request the permissions you need",
      detail:
        "Add permissions such as whatsapp_business_messaging. New permissions require App Review before production use.",
      link: { label: "Permissions reference", url: "https://developers.facebook.com/docs/permissions" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire WhatsApp Business's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  telegram: [
    {
      title: "Open BotFather in Telegram",
      detail:
        "In Telegram, open a chat with @BotFather.",
      link: { label: "Open BotFather", url: "https://t.me/BotFather" },
    },
    {
      title: "Create a bot with /newbot",
      detail:
        "Send /newbot, choose a name and username, and BotFather returns your bot token.",
      link: { label: "BotFather guide", url: "https://core.telegram.org/bots/features#botfather" },
    },
    {
      title: "Understand bot authorization",
      detail:
        "The token authorizes all Bot API calls. Keep it secret; you can revoke/regenerate via BotFather.",
      link: { label: "Authorizing your bot", url: "https://core.telegram.org/bots/api#authorizing-your-bot" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Telegram's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  line: [
    {
      title: "Open the LINE Developers Console",
      detail:
        "Sign in and create a provider, then a Messaging API channel.",
      link: { label: "Open LINE console", url: "https://developers.line.biz/console/" },
    },
    {
      title: "Issue a channel access token",
      detail:
        "In your channel's Messaging API tab, issue a long-lived channel access token and copy it.",
      link: { label: "Messaging API getting started", url: "https://developers.line.biz/en/docs/messaging-api/getting-started/" },
    },
    {
      title: "Review the Messaging API capabilities",
      detail:
        "Confirm the features your bot needs (reply, push, rich menus).",
      link: { label: "Messaging API overview", url: "https://developers.line.biz/en/docs/messaging-api/overview/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire LINE's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  wechat: [
    {
      title: "Open the WeChat Official Account platform",
      detail:
        "Sign in to the WeChat MP platform for your official account.",
      link: { label: "Open WeChat MP", url: "https://mp.weixin.qq.com/" },
    },
    {
      title: "Get your AppID and AppSecret",
      detail:
        "Under Basic Configuration, copy the AppID and AppSecret used to fetch an access token.",
      link: { label: "Access overview", url: "https://developers.weixin.qq.com/doc/offiaccount/en/Basic_Information/Access_Overview.html" },
    },
    {
      title: "Review API permissions",
      detail:
        "Confirm which interfaces your account type may call.",
      link: { label: "User management / UnionID", url: "https://developers.weixin.qq.com/doc/offiaccount/en/User_Management/Get_users_basic_information_UnionID.html" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire WeChat's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  twilio_sms: [
    {
      title: "Open the Twilio Console",
      detail:
        "Sign in to the Twilio Console. Note your Account SID.",
      link: { label: "Open Twilio Console", url: "https://console.twilio.com/" },
    },
    {
      title: "Create an API key",
      detail:
        "Go to Account then API keys & tokens then Create API key. Copy the SID and Secret (secret shown once).",
      link: { label: "API keys guide", url: "https://www.twilio.com/docs/iam/api-keys" },
    },
    {
      title: "Understand key permissions",
      detail:
        "Use a Standard key for most automation; keep the secret safe.",
      link: { label: "API key reference", url: "https://www.twilio.com/docs/iam/keys/api-key" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Twilio SMS's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  sender: [
    {
      title: "Open the Sender.net API settings",
      detail:
        "Sign in to Sender.net and go to Settings then API tokens.",
      link: { label: "Open Sender.net API settings", url: "https://app.sender.net/settings/tokens" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://api.sender.net/authentication/" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://api.sender.net/authentication/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Sender.net's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  mailgun: [
    {
      title: "Open the Mailgun API settings",
      detail:
        "Sign in to Mailgun and go to Settings then API keys.",
      link: { label: "Open Mailgun API settings", url: "https://app.mailgun.com/settings/api_security" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://documentation.mailgun.com/docs/mailgun/api-reference/api-overview" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://documentation.mailgun.com/docs/mailgun/user-manual/api-key-mgmt/rbac-mgmt" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Mailgun's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  sendgrid: [
    {
      title: "Open the SendGrid API settings",
      detail:
        "Sign in to SendGrid and go to Settings then API Keys.",
      link: { label: "Open SendGrid API settings", url: "https://app.sendgrid.com/settings/api_keys" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://www.twilio.com/docs/sendgrid/ui/account-and-settings/api-keys" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://www.twilio.com/docs/sendgrid/api-reference/api-key-permissions/api-key-permissions" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire SendGrid's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  mailchimp: [
    {
      title: "Open the Mailchimp API settings",
      detail:
        "Sign in to Mailchimp and go to Account then Extras then API keys.",
      link: { label: "Open Mailchimp API settings", url: "https://us1.admin.mailchimp.com/account/api/" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://mailchimp.com/developer/marketing/guides/quick-start/" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://mailchimp.com/developer/marketing/docs/fundamentals/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Mailchimp's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  brevo: [
    {
      title: "Open the Brevo API settings",
      detail:
        "Sign in to Brevo and go to SMTP & API then API Keys.",
      link: { label: "Open Brevo API settings", url: "https://app.brevo.com/settings/keys/api" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://developers.brevo.com/docs/getting-started" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://developers.brevo.com/docs/api-key-authentication" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Brevo's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  convertkit: [
    {
      title: "Open the Kit (ConvertKit) API settings",
      detail:
        "Sign in to Kit (ConvertKit) and go to Developer settings.",
      link: { label: "Open Kit (ConvertKit) API settings", url: "https://app.kit.com/account_settings/developer_settings" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://developers.kit.com/#authentication" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://developers.kit.com/#oauth-scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Kit (ConvertKit)'s API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  activecampaign: [
    {
      title: "Open the ActiveCampaign API settings",
      detail:
        "Sign in to ActiveCampaign and go to Settings then Developer.",
      link: { label: "Open ActiveCampaign API settings", url: "https://www.activecampaign.com/" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://developers.activecampaign.com/reference/authentication" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://developers.activecampaign.com/reference/overview" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire ActiveCampaign's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  digitalocean: [
    {
      title: "Open DigitalOcean API tokens",
      detail:
        "Sign in to DigitalOcean and open API then Tokens.",
      link: { label: "Open API tokens", url: "https://cloud.digitalocean.com/account/api/tokens" },
    },
    {
      title: "Create a personal access token",
      detail:
        "Click Generate New Token, name it Atomic AI, choose scopes, and copy it - shown once.",
      link: { label: "Create a token guide", url: "https://docs.digitalocean.com/reference/api/create-personal-access-token/" },
    },
    {
      title: "Choose token scopes",
      detail:
        "Grant only the scopes your agents need.",
      link: { label: "Scopes reference", url: "https://docs.digitalocean.com/reference/api/scopes/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire DigitalOcean's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  terraform: [
    {
      title: "Open Terraform Cloud tokens",
      detail:
        "Sign in to HCP Terraform and open Account settings then Tokens.",
      link: { label: "Open tokens", url: "https://app.terraform.io/app/settings/tokens" },
    },
    {
      title: "Create an API token",
      detail:
        "Create a user or team API token and copy it - shown once.",
      link: { label: "API tokens guide", url: "https://developer.hashicorp.com/terraform/cloud-docs/users-teams-organizations/api-tokens" },
    },
    {
      title: "Understand token access",
      detail:
        "Team/user tokens carry that identity's permissions.",
      link: { label: "Authentication docs", url: "https://developer.hashicorp.com/terraform/cloud-docs/api-docs#authentication" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Terraform's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  stripe: [
    {
      title: "Open the Stripe API settings",
      detail:
        "Sign in to Stripe and go to Developers then API keys.",
      link: { label: "Open Stripe API settings", url: "https://dashboard.stripe.com/apikeys" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://docs.stripe.com/keys" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://docs.stripe.com/keys#limit-access" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Stripe's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  greenhouse: [
    {
      title: "Open the Greenhouse API settings",
      detail:
        "Sign in to Greenhouse and go to Harvest API.",
      link: { label: "Open Greenhouse API settings", url: "https://developers.greenhouse.io/harvest.html" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://developers.greenhouse.io/harvest.html#authentication" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://developers.greenhouse.io/harvest.html#permissions" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Greenhouse's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  bamboohr: [
    {
      title: "Open the BambooHR API settings",
      detail:
        "Sign in to BambooHR and go to API keys.",
      link: { label: "Open BambooHR API settings", url: "https://documentation.bamboohr.com/docs/getting-started" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://documentation.bamboohr.com/docs/getting-started#authentication" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://documentation.bamboohr.com/reference/get-employee" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire BambooHR's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  close: [
    {
      title: "Open the Close API settings",
      detail:
        "Sign in to Close and go to Settings then API Keys.",
      link: { label: "Open Close API settings", url: "https://app.close.com/settings/api/" },
    },
    {
      title: "Create an API key",
      detail:
        "Generate a new API key named Atomic AI and copy it - many providers show it only once.",
      link: { label: "Create-key guide", url: "https://developer.close.com/topics/authentication/" },
    },
    {
      title: "Restrict the key's permissions",
      detail:
        "Where supported, scope the key to only the actions your agents need (least privilege).",
      link: { label: "Permissions / scopes", url: "https://developer.close.com/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Close's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  cloudflare: [
    {
      title: "Open Cloudflare API tokens",
      detail:
        "Sign in to Cloudflare and open My Profile then API Tokens.",
      link: { label: "Open API tokens", url: "https://dash.cloudflare.com/profile/api-tokens" },
    },
    {
      title: "Create a scoped API token",
      detail:
        "Click Create Token, start from a template or custom, and grant only the permissions and zones your agents need.",
      link: { label: "Create a token guide", url: "https://developers.cloudflare.com/fundamentals/api/get-started/create-token/" },
    },
    {
      title: "Review permissions",
      detail:
        "Confirm the token permissions are minimal.",
      link: { label: "Permissions reference", url: "https://developers.cloudflare.com/fundamentals/api/reference/permissions/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Cloudflare's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  freshdesk: [
    {
      title: "Open the Freshdesk API docs",
      detail:
        "Sign in to Freshdesk. The API uses your account API key (Basic auth).",
      link: { label: "Freshdesk API", url: "https://developers.freshdesk.com/api/" },
    },
    {
      title: "Find your API key",
      detail:
        "Log in, click your profile then Profile settings; your API key is on the right.",
      link: { label: "How to find your API key", url: "https://support.freshdesk.com/en/support/solutions/articles/215517-how-to-find-your-api-key" },
    },
    {
      title: "Understand authentication",
      detail:
        "Use the API key as the username with any password in Basic auth.",
      link: { label: "Authentication docs", url: "https://developers.freshdesk.com/api/#authentication" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Freshdesk's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  hubspot: [
    {
      title: "Open HubSpot private apps",
      detail:
        "In your HubSpot account, go to Settings then Integrations then Private Apps.",
      link: { label: "Private apps overview", url: "https://developers.hubspot.com/docs/guides/apps/private-apps/overview" },
    },
    {
      title: "Create a private app",
      detail:
        "Click Create private app, name it Atomic AI, and configure it.",
      link: { label: "Private apps guide", url: "https://developers.hubspot.com/docs/guides/apps/private-apps/overview" },
    },
    {
      title: "Select scopes and get the token",
      detail:
        "On the Scopes tab, select what your agents need (e.g. crm.objects.contacts.read/write), create the app, and copy the access token.",
      link: { label: "Scopes reference", url: "https://developers.hubspot.com/docs/guides/apps/authentication/scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire HubSpot's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  salesforce: [
    {
      title: "Create a Connected App",
      detail:
        "In Salesforce Setup then App Manager then New Connected App (or External Client App).",
      link: { label: "Create a Connected App", url: "https://help.salesforce.com/s/articleView?id=sf.connected_app_create.htm" },
    },
    {
      title: "Enable OAuth settings",
      detail:
        "Enable OAuth, set the callback URL, and after saving copy the Consumer Key (client id) and Consumer Secret.",
      link: { label: "Connected App for API", url: "https://help.salesforce.com/s/articleView?id=sf.connected_app_create_api_integration.htm&type=5" },
    },
    {
      title: "Select OAuth scopes",
      detail:
        "Add scopes such as api and refresh_token so agents can act and refresh tokens.",
      link: { label: "OAuth scopes reference", url: "https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_tokens_scopes.htm&type=5" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Salesforce's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  zoho_crm: [
    {
      title: "Open the Zoho API Console",
      detail:
        "Sign in and open the Zoho API Console.",
      link: { label: "Open Zoho API Console", url: "https://api-console.zoho.com/" },
    },
    {
      title: "Register a client (Self Client / Server-based)",
      detail:
        "Add a client to get your Client ID and Client Secret.",
      link: { label: "Register a client", url: "https://www.zoho.com/crm/developer/docs/api/v7/register-client.html" },
    },
    {
      title: "Choose scopes",
      detail:
        "Request scopes like ZohoCRM.modules.ALL for the modules your agents use.",
      link: { label: "Scopes reference", url: "https://www.zoho.com/crm/developer/docs/api/v7/scopes.html" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Zoho CRM's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  paypal: [
    {
      title: "Open the PayPal Developer dashboard",
      detail:
        "Sign in and open Apps & Credentials.",
      link: { label: "Open PayPal apps", url: "https://developer.paypal.com/dashboard/applications" },
    },
    {
      title: "Create an app for credentials",
      detail:
        "Create an app to get your Client ID and Secret (Sandbox and Live).",
      link: { label: "Get client id & secret", url: "https://developer.paypal.com/api/rest/#link-getclientidandclientsecret" },
    },
    {
      title: "Understand authentication",
      detail:
        "PayPal uses OAuth 2.0 client credentials to mint access tokens.",
      link: { label: "Authentication guide", url: "https://developer.paypal.com/api/rest/authentication/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire PayPal's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  xero: [
    {
      title: "Open the Xero developer portal",
      detail:
        "Sign in and open My Apps.",
      link: { label: "Open My Apps", url: "https://developer.xero.com/app/manage" },
    },
    {
      title: "Create an app (get client id/secret)",
      detail:
        "Create an app, set the redirect URI, and copy the Client ID and Client Secret.",
      link: { label: "OAuth 2.0 auth flow", url: "https://developer.xero.com/documentation/guides/oauth2/auth-flow/" },
    },
    {
      title: "Choose scopes",
      detail:
        "Request scopes like accounting.transactions and offline_access for refresh tokens.",
      link: { label: "Scopes reference", url: "https://developer.xero.com/documentation/guides/oauth2/scopes/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Xero's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  quickbooks: [
    {
      title: "Open the Intuit developer dashboard",
      detail:
        "Sign in and open your app dashboard.",
      link: { label: "Open Intuit dashboard", url: "https://developer.intuit.com/app/developer/dashboard" },
    },
    {
      title: "Create an app and get keys",
      detail:
        "Create an app and copy the Client ID and Client Secret; set the redirect URI.",
      link: { label: "OAuth 2.0 guide", url: "https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0" },
    },
    {
      title: "Choose scopes",
      detail:
        "Request com.intuit.quickbooks.accounting for accounting data.",
      link: { label: "Scopes reference", url: "https://developer.intuit.com/app/developer/qbo/docs/learn/scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire QuickBooks's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  intercom: [
    {
      title: "Open the Intercom Developer Hub",
      detail:
        "Sign in and open the Developer Hub to create an app.",
      link: { label: "Open Developer Hub", url: "https://app.intercom.com/a/apps/_/developer-hub" },
    },
    {
      title: "Create an app / get an access token",
      detail:
        "Create an app; for your own workspace you can copy an access token directly.",
      link: { label: "Authentication guide", url: "https://developers.intercom.com/docs/build-an-integration/learn-more/authentication" },
    },
    {
      title: "Set up OAuth & scopes (for distribution)",
      detail:
        "For third-party installs, configure OAuth and scopes.",
      link: { label: "OAuth setup", url: "https://developers.intercom.com/docs/build-an-integration/learn-more/authentication/setting-up-oauth" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Intercom's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  zendesk: [
    {
      title: "Open Zendesk API settings",
      detail:
        "In Zendesk Admin Center then Apps and integrations then APIs then Zendesk API.",
      link: { label: "Security & auth", url: "https://developer.zendesk.com/api-reference/introduction/security-and-auth/" },
    },
    {
      title: "Create an OAuth client or API token",
      detail:
        "Create an OAuth client (recommended) or an API token for your account.",
      link: { label: "Manage OAuth token access", url: "https://support.zendesk.com/hc/en-us/articles/8889508417946-Managing-OAuth-token-access-to-the-API" },
    },
    {
      title: "Understand scopes",
      detail:
        "Configure allowed scopes on the OAuth client for least privilege.",
      link: { label: "Authentication docs", url: "https://developer.zendesk.com/documentation/authentication/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Zendesk's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  notion: [
    {
      title: "Open your Notion integrations",
      detail:
        "Go to My integrations in Notion.",
      link: { label: "Open My integrations", url: "https://www.notion.so/my-integrations" },
    },
    {
      title: "Create an internal integration",
      detail:
        "Click New integration, choose Internal, associate a workspace, and copy the Internal Integration Secret.",
      link: { label: "Create an integration", url: "https://developers.notion.com/docs/create-a-notion-integration" },
    },
    {
      title: "Set capabilities and share pages",
      detail:
        "Choose read/update/insert capabilities, then share the specific pages/databases with the integration.",
      link: { label: "Capabilities reference", url: "https://developers.notion.com/reference/capabilities" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Notion's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  aws: [
    {
      title: "Open AWS IAM",
      detail:
        "Sign in to the AWS console and open IAM. Prefer creating a dedicated IAM user/role for Atomic AI.",
      link: { label: "Open IAM credentials", url: "https://console.aws.amazon.com/iam/home#/security_credentials" },
    },
    {
      title: "Create an access key",
      detail:
        "For the IAM user, create an access key. Copy the Access Key ID and Secret Access Key (secret shown once).",
      link: { label: "Access keys guide", url: "https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html" },
    },
    {
      title: "Attach least-privilege policies",
      detail:
        "Attach IAM policies granting only the actions your agents need. Avoid AdministratorAccess.",
      link: { label: "IAM policies guide", url: "https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire AWS's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  kubernetes: [
    {
      title: "Review Kubernetes authentication",
      detail:
        "Decide how your agent authenticates to the cluster (service account token or kubeconfig).",
      link: { label: "Authentication overview", url: "https://kubernetes.io/docs/reference/access-authn-authz/authentication/" },
    },
    {
      title: "Create a service account",
      detail:
        "Create a ServiceAccount and generate a token for it (kubectl create token).",
      link: { label: "Configure a service account", url: "https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/" },
    },
    {
      title: "Bind least-privilege RBAC",
      detail:
        "Bind Roles/ClusterRoles to the service account granting only needed verbs/resources.",
      link: { label: "RBAC reference", url: "https://kubernetes.io/docs/reference/access-authn-authz/rbac/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Kubernetes's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  workday: [
    {
      title: "Review Workday API access",
      detail:
        "Workday access is admin-configured. Work with your Workday admin to register an API client (OAuth).",
      link: { label: "Workday community", url: "https://community.workday.com/" },
    },
    {
      title: "Register an API client / get credentials",
      detail:
        "Your admin registers an API Client for Integrations and provides the client id/secret and token endpoint.",
      link: { label: "REST API reference", url: "https://community.workday.com/sites/default/files/file-hosting/restapi/index.html" },
    },
    {
      title: "Confirm scopes / functional areas",
      detail:
        "Confirm which Workday APIs and functional areas the client may access.",
      link: { label: "Workday APIs docs", url: "https://docs.workday.com/en-us/workday-apis.html" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Workday's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  lever: [
    {
      title: "Open Lever integrations settings",
      detail:
        "Sign in to Lever and open Settings then Integrations and API.",
      link: { label: "Open integrations", url: "https://hire.lever.co/settings/integrations" },
    },
    {
      title: "Create an API key / OAuth app",
      detail:
        "Generate an API key (or set up OAuth for distribution) and copy it.",
      link: { label: "Authentication guide", url: "https://hire.lever.co/developer/documentation#authentication" },
    },
    {
      title: "Choose OAuth scopes",
      detail:
        "For OAuth, request only the scopes your agents need.",
      link: { label: "OAuth scopes", url: "https://hire.lever.co/developer/documentation#oauth-scopes" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Lever's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  pipedrive: [
    {
      title: "Find your Pipedrive API token",
      detail:
        "Sign in and open Settings then Personal preferences then API.",
      link: { label: "How to find the API token", url: "https://pipedrive.readme.io/docs/how-to-find-the-api-token" },
    },
    {
      title: "Copy your personal API token",
      detail:
        "Copy the token shown on the API tab.",
      link: { label: "Find your personal API key", url: "https://support.pipedrive.com/en/article/how-can-i-find-my-personal-api-key" },
    },
    {
      title: "For apps, review OAuth scopes",
      detail:
        "To distribute an app, use OAuth and request scoped access.",
      link: { label: "Scopes & permissions", url: "https://pipedrive.readme.io/docs/marketplace-scopes-and-permissions-explanations" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Pipedrive's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  sap: [
    {
      title: "Open SAP API Business Hub / BTP",
      detail:
        "Access your SAP BTP subaccount (or the SAP API Business Hub) for the service you're integrating.",
      link: { label: "SAP API hub", url: "https://api.sap.com/" },
    },
    {
      title: "Create a service key",
      detail:
        "Create a service instance and a service key to obtain client credentials and the token URL.",
      link: { label: "Create service keys", url: "https://help.sap.com/docs/btp/sap-business-technology-platform/creating-service-keys" },
    },
    {
      title: "Use the OAuth flow",
      detail:
        "Exchange the client credentials for an access token via the OAuth grant.",
      link: { label: "OAuth grant flow", url: "https://help.sap.com/docs/btp/sap-business-technology-platform/using-oauth-authorization-code-grant-flow" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire SAP's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  netsuite: [
    {
      title: "Open NetSuite",
      detail:
        "Sign in to NetSuite as an administrator.",
      link: { label: "Open NetSuite", url: "https://system.netsuite.com/" },
    },
    {
      title: "Create an integration record (get keys)",
      detail:
        "Under Setup then Integration then Manage Integrations, create a record with token-based auth to get the Consumer Key/Secret.",
      link: { label: "Integration / auth setup", url: "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_157771733782.html" },
    },
    {
      title: "Assign roles & permissions",
      detail:
        "Assign a role with least-privilege permissions and create access tokens for it.",
      link: { label: "Roles & permissions", url: "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1543865613.html" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire NetSuite's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  calendly: [
    {
      title: "Open Calendly integrations",
      detail:
        "Sign in and open Integrations & apps then API & webhooks.",
      link: { label: "Open API & webhooks", url: "https://calendly.com/integrations/api_webhooks" },
    },
    {
      title: "Create a personal access token",
      detail:
        "Under API keys / personal access tokens, generate a token and copy it.",
      link: { label: "Getting started", url: "https://developer.calendly.com/getting-started" },
    },
    {
      title: "Review the API capabilities",
      detail:
        "For distribution use OAuth; personal tokens act as your user.",
      link: { label: "API docs", url: "https://developer.calendly.com/api-docs/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Calendly's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  yahoo: [
    {
      title: "Open the Yahoo Developer apps page",
      detail:
        "Sign in and open your Yahoo developer apps.",
      link: { label: "Open Yahoo apps", url: "https://developer.yahoo.com/apps/" },
    },
    {
      title: "Create an app (OAuth client)",
      detail:
        "Create a project/app to get your Client ID (Consumer Key) and Client Secret.",
      link: { label: "OAuth getting started", url: "https://developer.yahoo.com/oauth2/guide/openid_connect/getting_started.html" },
    },
    {
      title: "Choose OAuth scopes",
      detail:
        "Request the scopes your automations need (e.g. mail).",
      link: { label: "OAuth guide & scopes", url: "https://developer.yahoo.com/oauth2/guide/" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire Yahoo Mail's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
  imap_smtp: [
    {
      title: "Enable IMAP/SMTP on your mailbox",
      detail:
        "Turn on IMAP access in your mail provider's settings (e.g. Gmail: Settings then Forwarding and POP/IMAP). Use an app password if 2FA is on.",
      link: { label: "Enable IMAP (example)", url: "https://support.google.com/a/answer/105694" },
    },
    {
      title: "Get your server host, port, and credentials",
      detail:
        "Note the IMAP/SMTP host and ports and your username/app-password. IMAP is defined by RFC 3501.",
      link: { label: "IMAP protocol (RFC 3501)", url: "https://datatracker.ietf.org/doc/html/rfc3501" },
    },
    {
      title: "Use SMTP AUTH to send",
      detail:
        "Sending uses SMTP with authentication (RFC 4954).",
      link: { label: "SMTP AUTH (RFC 4954)", url: "https://datatracker.ietf.org/doc/html/rfc4954" },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Paste the credential(s) into the form below, choosing Personal or Shared. To wire IMAP/SMTP's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: STRANDS_MCP_TOOLS_URL },
    },
  ],
};

/** Sensible generic fallback so every step still has a real link. */
const GENERIC: ProviderLinks = {
  console: "https://oauth.net/2/",
  create: "https://oauth.net/2/grant-types/authorization-code/",
  scopes: "https://oauth.net/2/scope/",
};

/**
 * Build the guide for a provider. Every step carries its own documentation link
 * (BUILD.md): console, create-app/token, scopes/permissions, and how to use the
 * provider with the Strands Agents SDK.
 */
export function getProviderGuide(
  providerName: string,
  providerLabel: string,
): ProviderGuide {
  const links = LINKS[providerName] ?? GENERIC;
  const strandsUrl = links.strands ?? STRANDS_MCP_TOOLS_URL;

  // Providers with an exact, hand-written flow (e.g. Gmail) use it verbatim.
  const override = STEP_OVERRIDES[providerName];
  if (override) {
    return { docsUrl: links.console, steps: override };
  }

  const steps: GuideStep[] = [
    {
      title: `Open the ${providerLabel} developer console`,
      detail: `Sign in and go to the API / app credentials area for ${providerLabel}.`,
      link: { label: `Open ${providerLabel} console`, url: links.console },
    },
    {
      title: "Create an app or generate a token",
      detail:
        "Register a new application (for OAuth) or create an API key / personal access token. Give it a recognizable name like “Atomic AI”.",
      link: { label: "Create app / token guide", url: links.create },
    },
    {
      title: "Grant the scopes your automations need",
      detail:
        "Select the read/write scopes for the actions your agents will perform (for example, read email, create issues, send messages). Keep scopes minimal.",
      link: { label: "Scopes & permissions reference", url: links.scopes },
    },
    {
      title: "Connect it to your Strands agent",
      detail:
        "Copy the access token (and refresh token if provided) and paste them below, choosing Personal or Shared. To wire this provider's API into an agent, follow the Strands MCP tools guide.",
      link: { label: "Use with Strands Agents SDK", url: strandsUrl },
    },
  ];

  return { docsUrl: links.console, steps };
}

export { STRANDS_MCP_TOOLS_URL, STRANDS_TOOLS_OVERVIEW_URL };
