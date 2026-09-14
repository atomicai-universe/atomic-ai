"""Per-provider credential specifications (BUILD.md).

The single source of truth for *which* credentials each of the 74 supported
providers needs to actually operate its API. Different providers use different
credential shapes:

- Pure OAuth (Google, Microsoft/Entra, Zoom, Salesforce, Xero, QuickBooks,
  PayPal, Zoho, Yahoo, Calendly, Lever): client id + secret, and/or a
  refresh/access token, sometimes a tenant/instance/account id (non-secret).
- API key (Stripe, SendGrid, Mailgun, Mailchimp, Brevo, Notion, Cloudflare,
  DigitalOcean, Terraform, Sender, ConvertKit, ActiveCampaign, Close, Freshdesk,
  Greenhouse, BambooHR, Linear, Asana): a single secret key.
- Bot token (Slack, Discord, Telegram, LINE): one secret token.
- Key + secret pairs (AWS, Twilio, Trello, NetSuite): two distinct secrets plus
  non-secret config (region/account/consumer key).
- Basic auth (Jira, Confluence, Bitbucket, Freshdesk, IMAP/SMTP): username/email
  plus a token/password, and a base URL/host for self-hosted or per-site APIs.

Each provider maps to an ordered list of :class:`CredentialField`. A field is
either **secret** (encrypted at rest in the vault) or **config** (non-secret
operational value like a region, host, base URL, or account id). The frontend
renders exactly these fields; the vault encrypts every secret field.

This module is pure data + helpers (no I/O), so it is safe to import anywhere
and easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CredentialField:
    """One input a provider needs to authenticate.

    Attributes:
        name: Machine key stored in the credential map (snake_case).
        label: Human-friendly label for the UI.
        secret: True if the value must be encrypted at rest (tokens, keys,
            secrets, passwords). False for non-secret operational config
            (region, host, base URL, account/tenant id).
        required: Whether the field must be provided to connect.
        placeholder: Optional example/hint shown in the UI.
        help: Optional one-line guidance for the field.
    """

    name: str
    label: str
    secret: bool = True
    required: bool = True
    placeholder: str = ""
    help: str = ""


# Reusable field builders -----------------------------------------------------

def _secret(name: str, label: str, *, required: bool = True, placeholder: str = "", help: str = "") -> CredentialField:
    return CredentialField(name=name, label=label, secret=True, required=required, placeholder=placeholder, help=help)


def _config(name: str, label: str, *, required: bool = True, placeholder: str = "", help: str = "") -> CredentialField:
    return CredentialField(name=name, label=label, secret=False, required=required, placeholder=placeholder, help=help)


# Common credential shapes ----------------------------------------------------

def _oauth_client(*, with_refresh: bool = True) -> list[CredentialField]:
    """OAuth app: client id/secret (+ optional refresh token minted at consent)."""
    fields = [
        _secret("client_id", "Client ID", help="From the OAuth app / client you created."),
        _secret("client_secret", "Client Secret"),
    ]
    if with_refresh:
        fields.append(
            _secret(
                "refresh_token",
                "Refresh Token",
                required=False,
                help="Obtained after a user completes the OAuth consent flow. Leave blank to authorize via Connect.",
            )
        )
        fields.append(
            _secret("access_token", "Access Token", required=False, help="Optional; issued by the OAuth flow.")
        )
    return fields


def _api_key(label: str = "API Key", *, name: str = "api_key") -> list[CredentialField]:
    return [_secret(name, label)]


def _bot_token(label: str = "Bot Token", *, name: str = "bot_token") -> list[CredentialField]:
    return [_secret(name, label)]


# Microsoft Entra / Graph (delegated OAuth; needs tenant) ---------------------
def _entra() -> list[CredentialField]:
    return [
        _config("tenant_id", "Directory (tenant) ID", help="From your Entra app registration overview."),
        _secret("client_id", "Application (client) ID"),
        _secret("client_secret", "Client Secret", help="From Certificates & secrets (value shown once)."),
        _secret("refresh_token", "Refresh Token", required=False, help="From the user consent flow; or authorize via Connect."),
    ]


# Google OAuth (client id/secret + per-user refresh token) --------------------
def _google() -> list[CredentialField]:
    return [
        _secret("client_id", "Client ID"),
        _secret("client_secret", "Client Secret"),
        _secret("refresh_token", "Refresh Token", required=False, help="Issued when a user completes consent; or authorize via Connect."),
    ]


# Atlassian basic auth (email + API token + site base URL) --------------------
def _atlassian_basic() -> list[CredentialField]:
    return [
        _config("base_url", "Site base URL", placeholder="https://your-domain.atlassian.net"),
        _config("email", "Account email", help="Used as the Basic-auth username."),
        _secret("api_token", "API Token"),
    ]


# The full registry -----------------------------------------------------------

PROVIDER_CREDENTIALS: dict[str, list[CredentialField]] = {
    # ---- Email ----
    "gmail": _google(),
    "outlook": _entra(),
    "yahoo": _oauth_client(),
    "imap_smtp": [
        _config("host", "IMAP/SMTP host", placeholder="imap.example.com"),
        _config("port", "Port", placeholder="993"),
        _config("username", "Username / email"),
        _secret("password", "Password / app password"),
    ],
    # ---- Email marketing ----
    "sender": _api_key(),
    "mailgun": [
        _secret("api_key", "API Key"),
        _config("domain", "Sending domain", required=False, placeholder="mg.example.com"),
    ],
    "sendgrid": _api_key(),
    "mailchimp": [
        _secret("api_key", "API Key"),
        _config("server_prefix", "Server prefix", required=False, placeholder="us21", help="The data center in your API key suffix (e.g. us21)."),
    ],
    "brevo": _api_key(),
    "convertkit": _api_key(),
    "activecampaign": [
        _config("account_url", "Account API URL", placeholder="https://<account>.api-us1.com"),
        _secret("api_key", "API Key"),
    ],
    # ---- Social & messaging ----
    "facebook": [_secret("access_token", "Page/User Access Token"), _config("app_id", "App ID", required=False)],
    "instagram": [_secret("access_token", "Access Token"), _config("ig_business_account_id", "IG Business Account ID", required=False)],
    "telegram": _bot_token(),
    "whatsapp": [
        _secret("access_token", "Access Token"),
        _config("phone_number_id", "Phone number ID"),
    ],
    "line": [_secret("channel_access_token", "Channel Access Token"), _secret("channel_secret", "Channel Secret", required=False)],
    "wechat": [_secret("app_id", "AppID"), _secret("app_secret", "AppSecret")],
    "twilio_sms": [
        _config("account_sid", "Account SID"),
        _secret("api_key_sid", "API Key SID"),
        _secret("api_key_secret", "API Key Secret"),
    ],
    # ---- Office & productivity ----
    "word": _entra(),
    "powerpoint": _entra(),
    "excel": _entra(),
    "google_docs": _google(),
    "google_sheets": _google(),
    "google_slides": _google(),
    "onedrive": _entra(),
    "google_drive": _google(),
    "monday_workdocs": _api_key("API Token"),
    # ---- Developer & issue trackers ----
    "github": [_secret("personal_access_token", "Personal Access Token")],
    "gitlab": [_secret("personal_access_token", "Personal Access Token"), _config("base_url", "GitLab URL", required=False, placeholder="https://gitlab.com")],
    "jira": _atlassian_basic(),
    "linear": _api_key(),
    "monday_dev": _api_key("API Token"),
    "bitbucket": [_config("username", "Bitbucket username"), _secret("app_password", "App Password")],
    "azure_devops": [_config("organization", "Organization", placeholder="https://dev.azure.com/your-org"), _secret("personal_access_token", "Personal Access Token")],
    "asana": [_secret("personal_access_token", "Personal Access Token")],
    "trello": [_secret("api_key", "API Key"), _secret("token", "Token")],
    # ---- CRM & sales ----
    "salesforce": [
        _config("instance_url", "Instance URL", placeholder="https://your-domain.my.salesforce.com"),
        _secret("client_id", "Consumer Key (client id)"),
        _secret("client_secret", "Consumer Secret"),
        _secret("refresh_token", "Refresh Token", required=False),
    ],
    "hubspot": [_secret("access_token", "Private App Access Token")],
    "pipedrive": [_secret("api_token", "API Token"), _config("company_domain", "Company domain", required=False, placeholder="yourcompany")],
    "monday_crm": _api_key("API Token"),
    "zoho_crm": [
        _config("accounts_url", "Accounts URL", required=False, placeholder="https://accounts.zoho.com"),
        _secret("client_id", "Client ID"),
        _secret("client_secret", "Client Secret"),
        _secret("refresh_token", "Refresh Token", required=False),
    ],
    "close": _api_key(),
    # ---- Support & knowledge ----
    "zendesk": [
        _config("subdomain", "Subdomain", placeholder="yourcompany", help="From yourcompany.zendesk.com."),
        _config("email", "Agent email", required=False),
        _secret("api_token", "API Token"),
    ],
    "intercom": [_secret("access_token", "Access Token")],
    "freshdesk": [_config("domain", "Domain", placeholder="yourcompany.freshdesk.com"), _secret("api_key", "API Key")],
    "notion": [_secret("integration_secret", "Internal Integration Secret")],
    "confluence": _atlassian_basic(),
    "monday_service": _api_key("API Token"),
    # ---- ERP & financial ----
    "quickbooks": [
        _secret("client_id", "Client ID"),
        _secret("client_secret", "Client Secret"),
        _secret("refresh_token", "Refresh Token", required=False),
        _config("realm_id", "Realm (company) ID", required=False),
    ],
    "xero": _oauth_client(),
    "stripe": [_secret("secret_key", "Secret Key", placeholder="sk_live_...")],
    "sap": [
        _config("token_url", "Token URL"),
        _secret("client_id", "Client ID"),
        _secret("client_secret", "Client Secret"),
    ],
    "netsuite": [
        _config("account_id", "Account ID"),
        _secret("consumer_key", "Consumer Key"),
        _secret("consumer_secret", "Consumer Secret"),
        _secret("token_id", "Token ID"),
        _secret("token_secret", "Token Secret"),
    ],
    "paypal": [_secret("client_id", "Client ID"), _secret("client_secret", "Client Secret")],
    # ---- Team chat & meetings ----
    "slack": [_secret("bot_token", "Bot User OAuth Token", placeholder="xoxb-...")],
    "teams": _entra(),
    "discord": [_secret("bot_token", "Bot Token")],
    "zoom": [
        _config("account_id", "Account ID"),
        _secret("client_id", "Client ID"),
        _secret("client_secret", "Client Secret"),
    ],
    "google_meet": _google(),
    "teamviewer": [_secret("script_token", "Script Token")],
    "monday_workspaces": _api_key("API Token"),
    # ---- Cloud & DevOps ----
    "aws": [
        _secret("access_key_id", "Access Key ID"),
        _secret("secret_access_key", "Secret Access Key"),
        _config("region", "Region", placeholder="us-east-1"),
        _secret("session_token", "Session Token", required=False, help="Only for temporary/STS credentials."),
    ],
    "gcp": [_secret("service_account_json", "Service Account Key (JSON)", help="Paste the downloaded JSON key.")],
    "cloudflare": [_secret("api_token", "API Token"), _config("account_id", "Account ID", required=False)],
    "terraform": [_secret("api_token", "API Token"), _config("organization", "Organization", required=False)],
    "digitalocean": [_secret("api_token", "Personal Access Token")],
    "kubernetes": [
        _config("api_server", "API server URL", placeholder="https://cluster.example.com:6443"),
        _secret("service_account_token", "Service Account Token"),
        _config("ca_cert", "CA certificate (PEM)", required=False),
    ],
    # ---- HR & recruiting ----
    "workday": [
        _config("token_url", "Token URL"),
        _secret("client_id", "Client ID"),
        _secret("client_secret", "Client Secret"),
        _secret("refresh_token", "Refresh Token", required=False),
    ],
    "bamboohr": [_config("subdomain", "Company subdomain"), _secret("api_key", "API Key")],
    "greenhouse": [_secret("api_key", "Harvest API Key")],
    "lever": _api_key(),
    # ---- Calendar & scheduling ----
    "google_calendar": _google(),
    "outlook_calendar": _entra(),
    "calendly": [_secret("access_token", "Personal Access Token")],
}


# Generic fallback for any provider not explicitly specified.
GENERIC_CREDENTIALS: list[CredentialField] = [
    _secret("access_token", "Access Token / API Key"),
    _secret("refresh_token", "Refresh Token", required=False),
]


def fields_for(provider_name: str) -> list[CredentialField]:
    """Return the credential fields for a provider (generic fallback if unknown)."""
    return PROVIDER_CREDENTIALS.get(provider_name, GENERIC_CREDENTIALS)


def secret_field_names(provider_name: str) -> set[str]:
    """Names of fields that must be encrypted at rest for a provider."""
    return {f.name for f in fields_for(provider_name) if f.secret}


def required_field_names(provider_name: str) -> set[str]:
    """Names of fields that are required to connect a provider."""
    return {f.name for f in fields_for(provider_name) if f.required}


def spec_json() -> dict[str, list[dict]]:
    """Serialize the whole registry for the API (frontend form rendering)."""
    out: dict[str, list[dict]] = {}
    for provider, fields in PROVIDER_CREDENTIALS.items():
        out[provider] = [
            {
                "name": f.name,
                "label": f.label,
                "secret": f.secret,
                "required": f.required,
                "placeholder": f.placeholder,
                "help": f.help,
            }
            for f in fields
        ]
    return out


__all__ = [
    "CredentialField",
    "PROVIDER_CREDENTIALS",
    "GENERIC_CREDENTIALS",
    "fields_for",
    "secret_field_names",
    "required_field_names",
    "spec_json",
]
