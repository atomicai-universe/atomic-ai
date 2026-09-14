"""Provider API profiles — base URL + auth style per provider (BUILD.md).

To let a Strands agent actually call any of the 74 providers without hand-writing
74 SDKs, each provider maps to an :class:`ApiProfile` describing:

- ``base_url`` — the REST API root (may contain ``{placeholders}`` filled from
  the integration's non-secret ``config`` / credentials, e.g. a Zendesk
  subdomain or a Jira site base URL).
- ``auth`` — how to turn the decrypted credential map into request auth
  (bearer token, API key header, basic auth, or query param).

The generic authenticated HTTP tool (``app.services.agent_tools``) uses this to
build a correctly-authenticated ``httpx`` request for a provider. Credentials are
resolved per call and never logged. Providers whose ``base_url`` is unknown/
tenant-specific expose the tool with the credential attached but require the
agent to pass a full URL.

This module is pure data + a small resolver (no network, no secrets at import).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AuthStyle(StrEnum):
    """How a provider's credential is applied to an outbound request."""

    BEARER = "bearer"           # Authorization: Bearer <token>
    HEADER = "header"           # <header_name>: <prefix?><token>
    BASIC = "basic"             # HTTP Basic (username:password)
    QUERY = "query"             # ?<param>=<token>
    TOKEN_HEADER = "token"      # Authorization: token <token> (GitHub-style)
    NONE = "none"               # no automatic auth (agent supplies full request)


@dataclass(frozen=True)
class ApiProfile:
    """Describes how to call a provider's API with a stored credential.

    Attributes:
        base_url: REST root; may contain ``{field}`` placeholders resolved from
            the integration config/credentials (e.g. ``{subdomain}``).
        auth: The auth style to apply.
        cred_key: Which credential-map key holds the primary secret for auth
            (e.g. ``access_token``, ``api_key``, ``bot_token``, ``secret_key``).
        header_name: Header name for HEADER auth (e.g. ``X-Api-Key``).
        header_prefix: Optional prefix before the token for HEADER auth.
        query_param: Query parameter name for QUERY auth.
        basic_user_key: For BASIC auth, the cred/config key holding the username
            (e.g. ``email``); the password is ``cred_key``.
        docs: Human note about the provider's auth (not used at runtime).
    """

    base_url: str = ""
    auth: AuthStyle = AuthStyle.BEARER
    cred_key: str = "access_token"
    header_name: str = ""
    header_prefix: str = ""
    query_param: str = ""
    basic_user_key: str = ""
    docs: str = ""


B = AuthStyle.BEARER
H = AuthStyle.HEADER
BASIC = AuthStyle.BASIC
Q = AuthStyle.QUERY
TOK = AuthStyle.TOKEN_HEADER


PROVIDER_API: dict[str, ApiProfile] = {
    # ---- Email ----
    "gmail": ApiProfile("https://gmail.googleapis.com", B, "access_token"),
    "outlook": ApiProfile("https://graph.microsoft.com/v1.0", B, "access_token"),
    "yahoo": ApiProfile("https://api.mail.yahoo.com", B, "access_token"),
    "imap_smtp": ApiProfile("", AuthStyle.NONE, "password"),
    # ---- Email marketing ----
    "sender": ApiProfile("https://api.sender.net/v2", B, "api_key"),
    "mailgun": ApiProfile("https://api.mailgun.net/v3", BASIC, "api_key", basic_user_key="__literal_api"),
    "sendgrid": ApiProfile("https://api.sendgrid.com/v3", B, "api_key"),
    "mailchimp": ApiProfile("https://{server_prefix}.api.mailchimp.com/3.0", B, "api_key"),
    "brevo": ApiProfile("https://api.brevo.com/v3", H, "api_key", header_name="api-key"),
    "convertkit": ApiProfile("https://api.kit.com/v4", H, "api_key", header_name="X-Kit-Api-Key"),
    "activecampaign": ApiProfile("{account_url}", H, "api_key", header_name="Api-Token"),
    # ---- Social & messaging ----
    "facebook": ApiProfile("https://graph.facebook.com/v21.0", Q, "access_token", query_param="access_token"),
    "instagram": ApiProfile("https://graph.facebook.com/v21.0", Q, "access_token", query_param="access_token"),
    "telegram": ApiProfile("https://api.telegram.org", AuthStyle.NONE, "bot_token"),
    "whatsapp": ApiProfile("https://graph.facebook.com/v21.0", B, "access_token"),
    "line": ApiProfile("https://api.line.me/v2", B, "channel_access_token"),
    "wechat": ApiProfile("https://api.weixin.qq.com/cgi-bin", Q, "access_token", query_param="access_token"),
    "twilio_sms": ApiProfile("https://api.twilio.com/2010-04-01", BASIC, "api_key_secret", basic_user_key="api_key_sid"),
    # ---- Office & productivity ----
    "word": ApiProfile("https://graph.microsoft.com/v1.0", B, "access_token"),
    "powerpoint": ApiProfile("https://graph.microsoft.com/v1.0", B, "access_token"),
    "excel": ApiProfile("https://graph.microsoft.com/v1.0", B, "access_token"),
    "google_docs": ApiProfile("https://docs.googleapis.com/v1", B, "access_token"),
    "google_sheets": ApiProfile("https://sheets.googleapis.com/v4", B, "access_token"),
    "google_slides": ApiProfile("https://slides.googleapis.com/v1", B, "access_token"),
    "onedrive": ApiProfile("https://graph.microsoft.com/v1.0", B, "access_token"),
    "google_drive": ApiProfile("https://www.googleapis.com/drive/v3", B, "access_token"),
    "monday_workdocs": ApiProfile("https://api.monday.com/v2", B, "api_key"),
    # ---- Developer & issue trackers ----
    "github": ApiProfile("https://api.github.com", TOK, "personal_access_token"),
    "gitlab": ApiProfile("https://gitlab.com/api/v4", H, "personal_access_token", header_name="PRIVATE-TOKEN"),
    "jira": ApiProfile("{base_url}/rest/api/3", BASIC, "api_token", basic_user_key="email"),
    "linear": ApiProfile("https://api.linear.app", H, "api_key", header_name="Authorization"),
    "monday_dev": ApiProfile("https://api.monday.com/v2", B, "api_key"),
    "bitbucket": ApiProfile("https://api.bitbucket.org/2.0", BASIC, "app_password", basic_user_key="username"),
    "azure_devops": ApiProfile("{organization}", BASIC, "personal_access_token", basic_user_key="__pat_user"),
    "asana": ApiProfile("https://app.asana.com/api/1.0", B, "personal_access_token"),
    "trello": ApiProfile("https://api.trello.com/1", Q, "token", query_param="token"),
    # ---- CRM & sales ----
    "salesforce": ApiProfile("{instance_url}/services/data/v60.0", B, "access_token"),
    "hubspot": ApiProfile("https://api.hubapi.com", B, "access_token"),
    "pipedrive": ApiProfile("https://api.pipedrive.com/v1", Q, "api_token", query_param="api_token"),
    "monday_crm": ApiProfile("https://api.monday.com/v2", B, "api_key"),
    "zoho_crm": ApiProfile("https://www.zohoapis.com/crm/v7", H, "access_token", header_name="Authorization", header_prefix="Zoho-oauthtoken "),
    "close": ApiProfile("https://api.close.com/api/v1", BASIC, "api_key", basic_user_key="__literal_empty"),
    # ---- Support & knowledge ----
    "zendesk": ApiProfile("https://{subdomain}.zendesk.com/api/v2", BASIC, "api_token", basic_user_key="__zendesk_email_token"),
    "intercom": ApiProfile("https://api.intercom.io", B, "access_token"),
    "freshdesk": ApiProfile("https://{domain}/api/v2", BASIC, "api_key", basic_user_key="__literal_x"),
    "notion": ApiProfile("https://api.notion.com/v1", B, "integration_secret"),
    "confluence": ApiProfile("{base_url}/wiki/rest/api", BASIC, "api_token", basic_user_key="email"),
    "monday_service": ApiProfile("https://api.monday.com/v2", B, "api_key"),
    # ---- ERP & financial ----
    "quickbooks": ApiProfile("https://quickbooks.api.intuit.com/v3", B, "access_token"),
    "xero": ApiProfile("https://api.xero.com/api.xro/2.0", B, "access_token"),
    "stripe": ApiProfile("https://api.stripe.com/v1", B, "secret_key"),
    "sap": ApiProfile("{token_url}", B, "access_token"),
    "netsuite": ApiProfile("https://{account_id}.suitetalk.api.netsuite.com/services/rest", B, "token_id"),
    "paypal": ApiProfile("https://api-m.paypal.com", B, "access_token"),
    # ---- Team chat & meetings ----
    "slack": ApiProfile("https://slack.com/api", B, "bot_token"),
    "teams": ApiProfile("https://graph.microsoft.com/v1.0", B, "access_token"),
    "discord": ApiProfile("https://discord.com/api/v10", H, "bot_token", header_name="Authorization", header_prefix="Bot "),
    "zoom": ApiProfile("https://api.zoom.us/v2", B, "access_token"),
    "google_meet": ApiProfile("https://meet.googleapis.com/v2", B, "access_token"),
    "teamviewer": ApiProfile("https://webapi.teamviewer.com/api/v1", B, "script_token"),
    "monday_workspaces": ApiProfile("https://api.monday.com/v2", B, "api_key"),
    # ---- Cloud & DevOps ----
    "aws": ApiProfile("", AuthStyle.NONE, "access_key_id"),  # SigV4 handled specially
    "gcp": ApiProfile("https://cloudresourcemanager.googleapis.com/v1", B, "access_token"),
    "cloudflare": ApiProfile("https://api.cloudflare.com/client/v4", B, "api_token"),
    "terraform": ApiProfile("https://app.terraform.io/api/v2", B, "api_token"),
    "digitalocean": ApiProfile("https://api.digitalocean.com/v2", B, "api_token"),
    "kubernetes": ApiProfile("{api_server}", B, "service_account_token"),
    # ---- HR & recruiting ----
    "workday": ApiProfile("{token_url}", B, "access_token"),
    "bamboohr": ApiProfile("https://api.bamboohr.com/api/gateway.php/{subdomain}/v1", BASIC, "api_key", basic_user_key="__literal_x"),
    "greenhouse": ApiProfile("https://harvest.greenhouse.io/v1", BASIC, "api_key", basic_user_key="__literal_empty"),
    "lever": ApiProfile("https://api.lever.co/v1", BASIC, "api_key", basic_user_key="__literal_empty"),
    # ---- Calendar & scheduling ----
    "google_calendar": ApiProfile("https://www.googleapis.com/calendar/v3", B, "access_token"),
    "outlook_calendar": ApiProfile("https://graph.microsoft.com/v1.0", B, "access_token"),
    "calendly": ApiProfile("https://api.calendly.com", B, "access_token"),
}


# ---------------------------------------------------------------------------
# Verified per-provider API path hints ("no guessing" for the agent).
#
# Each string lists the exact, doc-verified endpoint paths the agent should use
# with the generic provider tool, so a model does not have to guess REST paths
# (which produced 404s). Kept short and copy-paste-ready. Paths are relative to
# the provider's ApiProfile base_url. EVERY provider in PROVIDER_API has a hint;
# each one names the READ endpoint(s) to call first for context and the WRITE
# endpoint(s) with the full request-body shape, so the agent produces complete,
# non-empty, contextually-relevant actions (real recipients, subjects, bodies)
# rather than fragments or bare URLs. GraphQL providers (linear, monday_*) and
# auth-special providers (imap_smtp, aws, sap, workday, kubernetes, azure_devops)
# state their constraints explicitly.
# ---------------------------------------------------------------------------
API_HINTS: dict[str, str] = {
    # ================= Email =================
    "gmail": (
        "Base https://gmail.googleapis.com. READ FIRST to gather context: "
        "list messages GET /gmail/v1/users/me/messages (unread: q=is:unread or labelIds=UNREAD); "
        "get full message GET /gmail/v1/users/me/messages/{id}?format=full (read the From, Subject, "
        "threadId and the Message-ID header before replying). "
        "REPLY (do this, not an empty send): build a COMPLETE RFC 2822 message with headers "
        "To: <original sender>, Subject: Re: <original subject>, In-Reply-To: <original Message-ID>, "
        "References: <original Message-ID>, then a blank line, then a real prose reply body; "
        "base64url-encode the whole message and POST /gmail/v1/users/me/drafts with "
        '{"message":{"raw":"<b64url>","threadId":"<threadId>"}} '
        "(or POST /gmail/v1/users/me/messages/send with the same message object to send immediately). "
        "Never send an empty body or a bare URL. Modify labels: POST /gmail/v1/users/me/messages/{id}/modify."
    ),
    "outlook": (
        "Microsoft Graph (base https://graph.microsoft.com/v1.0). READ FIRST: list mail "
        "GET /me/messages (unread: $filter=isRead eq false); get one GET /me/messages/{id} "
        "(read sender, subject, body). REPLY: POST /me/messages/{id}/reply with body "
        '{"comment":"<real prose reply>"} (Graph builds the threaded reply and recipients). '
        "New mail: POST /me/sendMail with "
        '{"message":{"subject":"...","body":{"contentType":"Text","content":"<real body>"},'
        '"toRecipients":[{"emailAddress":{"address":"..."}}]},"saveToSentItems":true}. '
        "Always include a real subject/body and the correct recipient; never send empty."
    ),
    "yahoo": (
        "Base https://api.mail.yahoo.com (OAuth2 bearer). Yahoo Mail Web Service is JSON-RPC "
        "style and access is restricted; endpoints are not publicly documented as simple REST paths. "
        "READ mail before acting and build a COMPLETE message (recipient, subject Re: <subject>, "
        "real body) rather than guessing paths. If the exact endpoint cannot be resolved, explain the "
        "limitation to the user instead of fabricating a call or sending an empty message."
    ),
    "imap_smtp": (
        "This is a raw IMAP/SMTP mailbox (host/port/username/password), NOT a REST API. The generic "
        "HTTP provider tool CANNOT make IMAP/SMTP calls. Do not fabricate REST paths. If asked to "
        "read or send mail here, explain that direct IMAP/SMTP access is not available through this "
        "tool and describe the complete message you would send (recipient, subject, real body)."
    ),
    # ================= Email marketing =================
    "sender": (
        "Base https://api.sender.net/v2 (Bearer api_key). READ FIRST: list groups GET /groups, "
        "list subscribers GET /subscribers. Add subscriber: POST /subscribers with "
        '{"email":"...","firstname":"...","groups":["<groupId>"]}. '
        "Send a transactional email: POST /transactional-campaigns with a COMPLETE payload "
        '(from, to: [{"email":"..."}], subject, and real html/text content). '
        "Always supply real recipient, subject and body; never an empty send."
    ),
    "mailgun": (
        "Base https://api.mailgun.net/v3 (HTTP Basic, user 'api'). READ FIRST: GET /{domain}/events "
        "or GET /domains to confirm the sending domain. Send email: POST /{domain}/messages "
        "(form-encoded) with a COMPLETE body: from, to, subject, and text (or html). "
        "Fill every field with real content addressed to the intended recipient; never send empty."
    ),
    "sendgrid": (
        "Base https://api.sendgrid.com/v3 (Bearer api_key). Send email: POST /mail/send with a "
        'COMPLETE JSON body {"personalizations":[{"to":[{"email":"..."}]}],'
        '"from":{"email":"<verified sender>"},"subject":"<real subject>",'
        '"content":[{"type":"text/plain","value":"<real body>"}]}. '
        "List templates: GET /templates. Always include real recipient, subject and body."
    ),
    "mailchimp": (
        "Base https://{server_prefix}.api.mailchimp.com/3.0 (Bearer api_key; server_prefix like us21). "
        "READ FIRST: list audiences GET /lists; list members GET /lists/{list_id}/members. "
        "Add/update member: PUT /lists/{list_id}/members/{subscriber_hash} with "
        '{"email_address":"...","status":"subscribed","merge_fields":{"FNAME":"..."}}. '
        "Create campaign: POST /campaigns then set content PUT /campaigns/{id}/content "
        "with a real subject and HTML; send POST /campaigns/{id}/actions/send. Never send empty content."
    ),
    "brevo": (
        "Base https://api.brevo.com/v3 (header api-key). READ FIRST: list contacts GET /contacts. "
        "Send transactional email: POST /smtp/email with a COMPLETE body "
        '{"sender":{"email":"..."},"to":[{"email":"..."}],"subject":"<real subject>",'
        '"htmlContent":"<real body>"}. Create contact: POST /contacts. '
        "Always fill recipient, subject and body with real content."
    ),
    "convertkit": (
        "Kit (ConvertKit) v4, base https://api.kit.com/v4 (header X-Kit-Api-Key). READ FIRST: "
        "GET /subscribers, GET /forms, GET /sequences. Create subscriber: POST /subscribers with "
        '{"email_address":"...","first_name":"...","state":"active"}. '
        "Add to form: POST /forms/{form_id}/subscribers {\"email_address\":\"...\"}. "
        "Create broadcast (email): POST /broadcasts with a real subject and content. Never send empty."
    ),
    "activecampaign": (
        "Base {account_url} (config account_url, e.g. https://<acct>.api-us1.com; header Api-Token). "
        "API path prefix is /api/3. READ FIRST: list contacts GET /api/3/contacts. "
        'Create contact: POST /api/3/contacts with {"contact":{"email":"...","firstName":"..."}}. '
        "Add a note: POST /api/3/notes. Provide real field values, not placeholders."
    ),
    # ================= Social & messaging =================
    "facebook": (
        "Graph API base https://graph.facebook.com/v21.0 (access_token as query param). "
        "READ FIRST: GET /me/accounts (pages), GET /{page_id}/feed. Publish a post: "
        'POST /{page_id}/feed with {"message":"<real post text>"}. Comment: '
        'POST /{object_id}/comments {"message":"<real comment>"}. Always include real message text.'
    ),
    "instagram": (
        "Instagram Graph via https://graph.facebook.com/v21.0 (access_token query param). "
        "Publishing is two steps: POST /{ig_business_account_id}/media with "
        '{"image_url":"...","caption":"<real caption>"} to create a container, then '
        "POST /{ig_business_account_id}/media_publish with {\"creation_id\":\"<id>\"}. "
        "READ FIRST: GET /{ig_business_account_id}/media. Reply to comment: POST /{comment_id}/replies "
        '{"message":"<real reply>"}. Always include a real caption/message.'
    ),
    "telegram": (
        "Base https://api.telegram.org; ALL calls are /bot<bot_token>/<method> (token in the path, "
        "no header auth). READ: GET /bot<token>/getUpdates or /getChat?chat_id=... . "
        "Send message: POST /bot<token>/sendMessage with "
        '{"chat_id":"<id>","text":"<real message>"}. Always send a real, non-empty text.'
    ),
    "whatsapp": (
        "WhatsApp Cloud API via https://graph.facebook.com/v21.0 (Bearer access_token). "
        "Send message: POST /{phone_number_id}/messages with "
        '{"messaging_product":"whatsapp","to":"<E164 number>","type":"text",'
        '"text":{"body":"<real message>"}}. For first-contact use type:"template". '
        "Always include the recipient number and a real message body."
    ),
    "line": (
        "Base https://api.line.me (Bearer channel_access_token). Push message: "
        'POST /v2/bot/message/push with {"to":"<userId>","messages":[{"type":"text",'
        '"text":"<real message>"}]}. Reply (within a webhook): POST /v2/bot/message/reply with '
        '{"replyToken":"<token>","messages":[...]}. Get profile: GET /v2/bot/profile/{userId}. '
        "Always include a real message; messages array must be non-empty."
    ),
    "wechat": (
        "Base https://api.weixin.qq.com/cgi-bin (access_token as query param; obtain via "
        "GET /token?grant_type=client_credential&appid=...&secret=...). Send customer-service message: "
        'POST /message/custom/send?access_token=... with {"touser":"<openid>","msgtype":"text",'
        '"text":{"content":"<real message>"}}. Always include a real, non-empty content.'
    ),
    "twilio_sms": (
        "Base https://api.twilio.com/2010-04-01 (HTTP Basic). Send SMS: "
        "POST /Accounts/{account_sid}/Messages.json (form-encoded) with To=<E164>, "
        "From=<your Twilio number>, Body=<real message text>. List messages: "
        "GET /Accounts/{account_sid}/Messages.json. Always include To, From and a real Body."
    ),
    # ================= Office & productivity =================
    "word": (
        "Microsoft Graph (base https://graph.microsoft.com/v1.0). Word .docx files live in OneDrive. "
        "READ FIRST: list files GET /me/drive/root/children; get item GET /me/drive/items/{item-id}; "
        "download content GET /me/drive/items/{item-id}/content. Create/replace a document: "
        "PUT /me/drive/root:/{filename}.docx:/content with the file bytes. Provide real, "
        "meaningful document content, not an empty file."
    ),
    "powerpoint": (
        "Microsoft Graph (base https://graph.microsoft.com/v1.0). .pptx files live in OneDrive. "
        "READ FIRST: GET /me/drive/root/children, GET /me/drive/items/{item-id}. Download: "
        "GET /me/drive/items/{item-id}/content. Upload/replace: "
        "PUT /me/drive/root:/{filename}.pptx:/content with real slide content bytes."
    ),
    "excel": (
        "Microsoft Graph workbook API (base https://graph.microsoft.com/v1.0). READ FIRST: locate the "
        "workbook GET /me/drive/root/children, list worksheets "
        "GET /me/drive/items/{item-id}/workbook/worksheets, read a range GET "
        "/me/drive/items/{item-id}/workbook/worksheets/{name}/range(address='A1:C10'). "
        "Write cells: PATCH that range with "
        '{"values":[[..real rows..]]}. Add rows to a table: POST '
        "/me/drive/items/{item-id}/workbook/tables/{table}/rows with {\"values\":[[...]]}. "
        "Always write real data with correct shapes."
    ),
    "google_docs": (
        "Base https://docs.googleapis.com/v1 (Bearer). READ FIRST: GET /documents/{documentId} to read "
        "structure/content. Edit: POST /documents/{documentId}:batchUpdate with a real requests array, "
        'e.g. {"requests":[{"insertText":{"location":{"index":1},"text":"<real text>"}}]}. '
        "Create a doc: POST /documents with {\"title\":\"...\"} then batchUpdate to add content. "
        "Never submit an empty requests array."
    ),
    "google_sheets": (
        "Base https://sheets.googleapis.com/v4 (Bearer). READ FIRST: GET /spreadsheets/{id} and "
        "GET /spreadsheets/{id}/values/{range} (e.g. Sheet1!A1:C10). Write values: "
        "PUT /spreadsheets/{id}/values/{range}?valueInputOption=USER_ENTERED with "
        '{"values":[[..real rows..]]}. Append: POST /spreadsheets/{id}/values/{range}:append. '
        "Always send real, correctly shaped row data."
    ),
    "google_slides": (
        "Base https://slides.googleapis.com/v1 (Bearer). READ FIRST: GET /presentations/{id} to read "
        "slides/elements. Edit: POST /presentations/{id}:batchUpdate with a real requests array "
        '(e.g. createSlide, insertText with real content). Create: POST /presentations {"title":"..."}. '
        "Never submit an empty requests array."
    ),
    "onedrive": (
        "Microsoft Graph (base https://graph.microsoft.com/v1.0). READ FIRST: list "
        "GET /me/drive/root/children, get item GET /me/drive/items/{item-id}, download "
        "GET /me/drive/items/{item-id}/content. Upload small file: "
        "PUT /me/drive/root:/{path/filename}:/content with real bytes. Create folder: "
        'POST /me/drive/root/children {"name":"...","folder":{}}.'
    ),
    "google_drive": (
        "Base https://www.googleapis.com/drive/v3 (Bearer). READ FIRST: list GET /files "
        "(q=name contains '...'), get metadata GET /files/{fileId}, download GET "
        "/files/{fileId}?alt=media. Create/rename: POST /files or PATCH /files/{fileId} with real "
        'metadata {"name":"..."}. Share: POST /files/{fileId}/permissions with a real role/type.'
    ),
    "monday_workdocs": (
        "monday.com GraphQL — single endpoint POST https://api.monday.com/v2 (Bearer api_key). Send "
        '{"query":"<graphql>","variables":{...}}. READ FIRST with a query, e.g. '
        '{"query":"query{docs{id name}}"}. Create a doc via mutation, e.g. '
        '{"query":"mutation($t:String!){create_doc(location:{workspace:{workspace_id:0,name:$t,'
        'kind:public}}){id}}","variables":{"t":"..."}}. Build complete queries/mutations with real '
        "variables; the API is GraphQL only (no REST paths)."
    ),
    # ================= Developer & issue trackers =================
    "github": (
        "Base https://api.github.com (token auth; send Accept: application/vnd.github+json). "
        "READ FIRST: GET /repos/{owner}/{repo}/issues, GET /repos/{owner}/{repo}/issues/{number} "
        "(read the title/body before replying). Comment on an issue/PR: "
        'POST /repos/{owner}/{repo}/issues/{number}/comments with {"body":"<real markdown reply>"}. '
        'Create issue: POST /repos/{owner}/{repo}/issues with {"title":"...","body":"..."}. '
        "Always send a real, non-empty body relevant to the item you read."
    ),
    "gitlab": (
        "Base https://gitlab.com/api/v4 (header PRIVATE-TOKEN). Project id is URL-encoded path or "
        "numeric id. READ FIRST: GET /projects/{id}/issues, GET /projects/{id}/issues/{iid}. "
        'Comment: POST /projects/{id}/issues/{iid}/notes with {"body":"<real note>"}. '
        'Create issue: POST /projects/{id}/issues with {"title":"...","description":"..."}. '
        "Always include a real, non-empty body."
    ),
    "jira": (
        "Base {base_url}/rest/api/3 (Basic email:api_token). READ FIRST: search "
        "GET /search?jql=..., get issue GET /issue/{key} (read summary/description). Add comment: "
        'POST /issue/{key}/comment with an Atlassian Document Format body '
        '{"body":{"type":"doc","version":1,"content":[{"type":"paragraph","content":['
        '{"type":"text","text":"<real comment>"}]}]}}. Create issue: POST /issue with '
        '{"fields":{"project":{"key":"..."},"summary":"...","issuetype":{"name":"Task"}}}. '
        "Always provide real, non-empty content."
    ),
    "linear": (
        "Linear GraphQL — single endpoint POST https://api.linear.app/graphql (header Authorization: "
        '<api_key>). Send {"query":"<graphql>","variables":{...}}. READ FIRST with a query, e.g. '
        '{"query":"{ issues(first:20){ nodes{ id identifier title } } }"}. Comment: '
        '{"query":"mutation($id:String!,$b:String!){ commentCreate(input:{issueId:$id,body:$b}){ '
        'success } }","variables":{"id":"<issueId>","b":"<real comment>"}}. Create issue: '
        "issueCreate(input:{teamId,title,description}). Build complete GraphQL with real variables; "
        "no REST paths."
    ),
    "monday_dev": (
        "monday.com GraphQL — single endpoint POST https://api.monday.com/v2 (Bearer api_key). Send "
        '{"query":"<graphql>","variables":{...}}. READ FIRST: {"query":"query{boards{id name '
        "items_page{items{id name}}}}\"}. Create item: {\"query\":\"mutation($b:ID!,$n:String!)"
        '{create_item(board_id:$b,item_name:$n){id}}","variables":{"b":"<boardId>","n":"..."}}. '
        "Add update/comment: create_update(item_id,body). Build complete mutations with real "
        "variables; GraphQL only."
    ),
    "bitbucket": (
        "Base https://api.bitbucket.org/2.0 (Basic username:app_password). READ FIRST: "
        "GET /repositories/{workspace}/{repo_slug}/issues, GET .../issues/{id}, "
        "GET .../pullrequests/{id}. Comment on a PR: "
        "POST /repositories/{workspace}/{repo_slug}/pullrequests/{id}/comments with "
        '{"content":{"raw":"<real comment>"}}. Always include a real, non-empty comment.'
    ),
    "azure_devops": (
        "Azure DevOps REST, base is your org URL {organization} (e.g. https://dev.azure.com/{org}); "
        "most calls need ?api-version=7.1 and a project. Basic auth uses an empty username + PAT. "
        "READ FIRST: query work items POST /{project}/_apis/wit/wiql, get one "
        "GET /{project}/_apis/wit/workitems/{id}. Add a comment: "
        "POST /{project}/_apis/wit/workItems/{id}/comments with {\"text\":\"<real comment>\"}. "
        "The agent must supply the full org/project host; always send real content."
    ),
    "asana": (
        "Base https://app.asana.com/api/1.0 (Bearer). Bodies wrap fields in a top-level 'data'. "
        "READ FIRST: GET /tasks?project={id}, GET /tasks/{task_gid}. Comment (story): "
        'POST /tasks/{task_gid}/stories with {"data":{"text":"<real comment>"}}. Create task: '
        'POST /tasks with {"data":{"name":"...","notes":"...","projects":["<id>"]}}. '
        "Always include real, non-empty text."
    ),
    "trello": (
        "Base https://api.trello.com/1 (key+token as query params). READ FIRST: "
        "GET /boards/{id}/cards, GET /cards/{id}, GET /lists/{id}/cards. Create card: "
        "POST /cards with idList=<listId>&name=<real title>&desc=<real description>. Comment: "
        "POST /cards/{id}/actions/comments?text=<real comment>. Always include real title/comment text."
    ),
    # ================= CRM & sales =================
    "salesforce": (
        "Base {instance_url}/services/data/v60.0 (Bearer). READ FIRST: query with SOQL "
        "GET /query?q=SELECT+Id,Name+FROM+Account, get record GET /sobjects/{Object}/{id}. "
        'Create record: POST /sobjects/{Object} with real fields, e.g. {"Subject":"...",'
        '"Description":"...","WhatId":"<id>"} for a Task. Update: PATCH /sobjects/{Object}/{id}. '
        "Always populate meaningful fields relevant to the record you read; no empty writes."
    ),
    "hubspot": (
        "Base https://api.hubapi.com (Bearer). READ FIRST: list GET /crm/v3/objects/contacts "
        "(or deals/companies/tickets), get one GET /crm/v3/objects/contacts/{id}. Create: "
        'POST /crm/v3/objects/contacts with {"properties":{"email":"...","firstname":"..."}}. '
        "Log a note engagement: POST /crm/v3/objects/notes with properties.hs_note_body plus an "
        "associations array to the record. Always supply real property values."
    ),
    "pipedrive": (
        "Base https://api.pipedrive.com/v1 (api_token as query param). READ FIRST: GET /deals, "
        "GET /deals/{id}, GET /persons/{id}. Create deal: POST /deals with "
        '{"title":"<real title>","value":..,"person_id":..}. Add a note: POST /notes with '
        '{"content":"<real note>","deal_id":<id>}. Always include real, non-empty content.'
    ),
    "monday_crm": (
        "monday.com GraphQL — single endpoint POST https://api.monday.com/v2 (Bearer api_key). Send "
        '{"query":"<graphql>","variables":{...}}. READ FIRST: query boards/items. Create lead item: '
        '{"query":"mutation($b:ID!,$n:String!){create_item(board_id:$b,item_name:$n){id}}",'
        '"variables":{"b":"<boardId>","n":"..."}}. Add note via create_update(item_id,body). '
        "GraphQL only; build complete mutations with real variables."
    ),
    "zoho_crm": (
        "Base https://www.zohoapis.com/crm/v7 (header Authorization: Zoho-oauthtoken <token>). "
        "READ FIRST: list GET /{Module} (e.g. /Leads, /Contacts, /Deals), get one GET /{Module}/{id}. "
        'Create: POST /{Module} with {"data":[{..real fields..}]}. Add a note: POST /Notes with '
        '{"data":[{"Note_Title":"...","Note_Content":"...","Parent_Id":"<id>","se_module":"Leads"}]}. '
        "Always send real field values inside the data array."
    ),
    "close": (
        "Base https://api.close.com/api/v1 (Basic api_key as username, empty password). READ FIRST: "
        "list leads GET /lead/, get lead GET /lead/{id}/. Add a note activity: POST /activity/note/ "
        'with {"lead_id":"<id>","note":"<real note>"}. Log an email: POST /activity/email/. '
        "Always include a real lead_id and non-empty note/body."
    ),
    # ================= Support & knowledge =================
    "zendesk": (
        "Base https://{subdomain}.zendesk.com/api/v2 (Basic email/token). READ FIRST: list "
        "GET /tickets.json, get GET /tickets/{id}.json (read requester + description). Add a public "
        'reply: PUT /tickets/{id}.json with {"ticket":{"comment":{"body":"<real reply>","public":true}}}. '
        'Create ticket: POST /tickets.json with {"ticket":{"subject":"...","comment":{"body":"..."}}}. '
        "Always include a real, non-empty comment body."
    ),
    "intercom": (
        "Base https://api.intercom.io (Bearer; send Intercom-Version header, e.g. 2.11). READ FIRST: "
        "list GET /conversations, get GET /conversations/{id} (read the last message). Reply: "
        'POST /conversations/{id}/reply with {"message_type":"comment","type":"admin",'
        '"admin_id":"<id>","body":"<real reply>"}. Always include a real, non-empty body.'
    ),
    "freshdesk": (
        "Base https://{domain}/api/v2 (Basic api_key as username, X as password). READ FIRST: list "
        "GET /tickets, get GET /tickets/{id} (read subject/description). Reply to requester: "
        'POST /tickets/{id}/reply with {"body":"<real reply html>"}. Add a note: '
        'POST /tickets/{id}/notes with {"body":"...","private":true}. Create ticket: POST /tickets '
        'with subject/description/email/status/priority. Always include a real, non-empty body.'
    ),
    "notion": (
        "Base https://api.notion.com/v1 (Bearer; REQUIRED header Notion-Version, e.g. 2022-06-28). "
        "READ FIRST: query DB POST /databases/{database_id}/query, get page GET /pages/{page_id}, "
        "read blocks GET /blocks/{block_id}/children. Create page: POST /pages with a real parent and "
        "properties (e.g. a Title). Append content: PATCH /blocks/{block_id}/children with a real "
        "children array of paragraph blocks. Always send meaningful, non-empty content."
    ),
    "confluence": (
        "Base {base_url}/wiki/rest/api (Basic email:api_token). READ FIRST: GET /content?spaceKey=..., "
        "get page GET /content/{id}?expand=body.storage,version. Create page: POST /content with "
        '{"type":"page","title":"<real title>","space":{"key":"..."},"body":{"storage":'
        '{"value":"<real HTML>","representation":"storage"}}}. Update: PUT /content/{id} (increment '
        "version.number). Always include real title and body."
    ),
    "monday_service": (
        "monday.com GraphQL — single endpoint POST https://api.monday.com/v2 (Bearer api_key). Send "
        '{"query":"<graphql>","variables":{...}}. READ FIRST: query boards/items. Create a ticket '
        'item: {"query":"mutation($b:ID!,$n:String!){create_item(board_id:$b,item_name:$n){id}}",'
        '"variables":{"b":"<boardId>","n":"..."}}. Reply via create_update(item_id,body). GraphQL '
        "only; build complete mutations with real variables."
    ),
    # ================= ERP & financial =================
    "quickbooks": (
        "Base https://quickbooks.api.intuit.com/v3 (Bearer; company = realm_id). READ FIRST: query "
        "GET /company/{realm_id}/query?query=SELECT * FROM Invoice, get GET "
        "/company/{realm_id}/{entity}/{id}. Create/update entity: POST "
        "/company/{realm_id}/{entity} (e.g. invoice, customer) with the full required field set "
        "(sparse updates need Id + SyncToken). Send Accept: application/json. Always provide real, "
        "complete field values."
    ),
    "xero": (
        "Base https://api.xero.com/api.xro/2.0 (Bearer; send xero-tenant-id header). READ FIRST: "
        "GET /Invoices, GET /Contacts, GET /Invoices/{id}. Create: POST /Invoices (or /Contacts) with "
        'a COMPLETE body {"Type":"ACCREC","Contact":{"ContactID":"..."},"LineItems":[{..real..}]}. '
        "Send Accept: application/json. Always populate real, required fields."
    ),
    "stripe": (
        "Base https://api.stripe.com/v1 (Bearer secret_key; all bodies are form-encoded, not JSON). "
        "READ FIRST: GET /charges, GET /customers/{id}, GET /payment_intents/{id}. Create refund: "
        "POST /refunds with charge=<id> (or payment_intent=<id>) and optional amount. Create customer: "
        "POST /customers with email=... . Always include the required, real parameters."
    ),
    "sap": (
        "SAP host is tenant-specific; base is resolved from {token_url}/service config and the agent "
        "must supply the full OData service URL (e.g. .../sap/opu/odata/sap/<SERVICE>). READ FIRST via "
        "GET on the entity set (e.g. GET /<EntitySet>?$top=10). Create: POST /<EntitySet> with a "
        "complete JSON entity; OData often needs an X-CSRF-Token fetched via a GET with "
        "X-CSRF-Token: Fetch. Supply the full URL and real entity fields; do not guess a shared host."
    ),
    "netsuite": (
        "NetSuite SuiteQL/REST, base https://{account_id}.suitetalk.api.netsuite.com/services/rest "
        "(OAuth). READ FIRST: query POST /query/v1/suiteql with {\"q\":\"SELECT id FROM transaction\"} "
        "(header Prefer: transient), or GET /record/v1/{recordType}/{id}. Create: POST "
        "/record/v1/{recordType} with a complete JSON record. Supply the account-specific host and "
        "real field values."
    ),
    "paypal": (
        "Base https://api-m.paypal.com (Bearer). READ FIRST: GET /v2/checkout/orders/{id}, list "
        "invoices GET /v2/invoicing/invoices. Create order: POST /v2/checkout/orders with "
        '{"intent":"CAPTURE","purchase_units":[{"amount":{"currency_code":"USD","value":"10.00"}}]}. '
        "Capture: POST /v2/checkout/orders/{id}/capture. Refund: POST /v2/payments/captures/{id}/refund. "
        "Always send real, complete amounts/fields."
    ),
    # ================= Team chat & meetings =================
    "slack": (
        "Base https://slack.com/api (Bearer bot_token). READ FIRST: GET /conversations.list, "
        "GET /conversations.history?channel=<id> (read recent messages for context). Post a message: "
        'POST /chat.postMessage with {"channel":"<id>","text":"<real message>"} (reply in thread: add '
        'thread_ts). Always send a real, non-empty, contextually relevant message.'
    ),
    "teams": (
        "Microsoft Graph (base https://graph.microsoft.com/v1.0). READ FIRST: GET /teams/{team-id}/"
        "channels, GET /teams/{team-id}/channels/{channel-id}/messages. Post a channel message: "
        "POST /teams/{team-id}/channels/{channel-id}/messages with "
        '{"body":{"contentType":"html","content":"<real message>"}}. Reply: '
        ".../messages/{message-id}/replies. Always include real, non-empty content."
    ),
    "discord": (
        "Base https://discord.com/api/v10 (header Authorization: Bot <token>). READ FIRST: "
        "GET /channels/{channel_id}/messages. Send message: POST /channels/{channel_id}/messages with "
        '{"content":"<real message>"}. Always include real, non-empty content addressed to the '
        "right channel."
    ),
    "zoom": (
        "Base https://api.zoom.us/v2 (Bearer). READ FIRST: GET /users/me, GET /users/me/meetings. "
        "Create meeting: POST /users/me/meetings with a COMPLETE body "
        '{"topic":"<real topic>","type":2,"start_time":"2024-01-01T10:00:00Z","duration":30,'
        '"timezone":"UTC"}. Get: GET /meetings/{meetingId}. Always include a real topic and time.'
    ),
    "google_meet": (
        "Google Meet REST v2, base https://meet.googleapis.com/v2 (Bearer). Create a meeting space: "
        "POST /spaces (body may be {} or include config); the response has meetingUri/meetingCode. "
        "Get space: GET /spaces/{name}. READ conference history: GET /conferenceRecords. To invite "
        "people, create a calendar event with a Meet link via the Calendar API. Return the real "
        "meeting link, not a placeholder."
    ),
    "teamviewer": (
        "Base https://webapi.teamviewer.com/api/v1 (Bearer script_token). READ FIRST: GET /devices, "
        "GET /contacts, GET /account. Create a contact: POST /contacts with real name/email. "
        "Send a message/create a session as documented. Always supply real field values; do not guess "
        "undocumented paths."
    ),
    "monday_workspaces": (
        "monday.com GraphQL — single endpoint POST https://api.monday.com/v2 (Bearer api_key). Send "
        '{"query":"<graphql>","variables":{...}}. READ FIRST: {"query":"query{workspaces{id name}}"}. '
        'Create workspace: {"query":"mutation($n:String!){create_workspace(name:$n,kind:open){id}}",'
        '"variables":{"n":"..."}}. GraphQL only; build complete mutations with real variables.'
    ),
    # ================= Cloud & DevOps =================
    "aws": (
        "AWS APIs require SigV4 request signing, which the generic bearer/API-key HTTP tool CANNOT "
        "produce. Do not fabricate signed REST calls. If asked to act on AWS, explain that direct "
        "signed API calls are not available through this tool and describe the exact operation "
        "(service, action, parameters) that would be needed instead."
    ),
    "gcp": (
        "Base https://cloudresourcemanager.googleapis.com/v1 (Bearer from service account). Other GCP "
        "services use their own hosts (e.g. compute.googleapis.com) — the agent must pass the full "
        "URL. READ FIRST: GET /projects. Act via the specific service's REST method with a complete "
        "JSON body. Always supply real, complete request bodies for the target service."
    ),
    "cloudflare": (
        "Base https://api.cloudflare.com/client/v4 (Bearer api_token). READ FIRST: GET /zones, "
        "GET /zones/{zone_id}/dns_records. Create DNS record: POST /zones/{zone_id}/dns_records with "
        '{"type":"A","name":"<fqdn>","content":"<ip>","ttl":3600,"proxied":true}. Update: PUT the '
        "record. Always include real, complete record fields."
    ),
    "terraform": (
        "Terraform Cloud, base https://app.terraform.io/api/v2 (Bearer; JSON:API format — "
        "Content-Type: application/vnd.api+json, bodies wrapped in {\"data\":{\"type\":...,"
        '"attributes":{...}}}). READ FIRST: GET /organizations/{org}/workspaces, GET '
        "/workspaces/{id}. Queue a run: POST /runs with a data object referencing the workspace. "
        "Always send correctly-shaped JSON:API bodies with real attributes."
    ),
    "digitalocean": (
        "Base https://api.digitalocean.com/v2 (Bearer). READ FIRST: GET /droplets, GET /droplets/{id}, "
        "GET /account. Create droplet: POST /droplets with "
        '{"name":"...","region":"nyc3","size":"s-1vcpu-1gb","image":"ubuntu-22-04-x64"}. '
        "Always include real, complete required fields."
    ),
    "kubernetes": (
        "Kubernetes API server host is cluster-specific ({api_server}); the agent must use that full "
        "base and Bearer service_account_token. READ FIRST: GET /api/v1/namespaces/{ns}/pods, "
        "GET /apis/apps/v1/namespaces/{ns}/deployments. Create: POST the collection URL with a "
        "complete manifest JSON (apiVersion, kind, metadata, spec). Update: PATCH with the correct "
        "Content-Type. Always send a complete, valid manifest."
    ),
    # ================= HR & recruiting =================
    "workday": (
        "Workday host is tenant-specific; the base is derived from {token_url} and the agent must "
        "supply the full REST/RaaS or WQL service URL for the tenant (e.g. .../ccx/api/...). READ "
        "FIRST via GET on the resource collection. Act via the documented POST/PUT with a complete "
        "JSON body. Supply the full tenant URL and real fields; do not guess a shared host."
    ),
    "bamboohr": (
        "Base https://api.bamboohr.com/api/gateway.php/{subdomain}/v1 (Basic api_key as username, x as "
        "password; send Accept: application/json). READ FIRST: GET /employees/directory, get employee "
        "GET /employees/{id}?fields=firstName,lastName,workEmail. Add employee: POST /employees with "
        "real fields. Add a time-off/note per docs. Always include real, complete field values."
    ),
    "greenhouse": (
        "Greenhouse Harvest, base https://harvest.greenhouse.io/v1 (Basic api_key as username, empty "
        "password; writes need On-Behalf-Of: <user_id> header). READ FIRST: GET /candidates, "
        "GET /candidates/{id}, GET /candidates/{id}/activity_feed. Add a note: "
        'POST /candidates/{id}/activity_feed_notes with {"user_id":<id>,"body":"<real note>",'
        '"visibility":"public"}. Always include a real, non-empty note body.'
    ),
    "lever": (
        "Base https://api.lever.co/v1 (Basic api_key as username, empty password). READ FIRST: "
        "GET /opportunities, GET /opportunities/{id}. Add a note: POST /opportunities/{id}/notes with "
        '{"value":"<real note>"}. Create opportunity: POST /opportunities with real candidate fields. '
        "Always include a real, non-empty note."
    ),
    # ================= Calendar & scheduling =================
    "google_calendar": (
        "Base https://www.googleapis.com/calendar/v3 (Bearer). READ FIRST: GET /calendars/primary/"
        "events (use timeMin/timeMax, singleEvents=true, orderBy=startTime), get GET "
        "/calendars/primary/events/{eventId}. Create event: POST /calendars/primary/events with a "
        'COMPLETE body {"summary":"<real title>","start":{"dateTime":"...","timeZone":"..."},'
        '"end":{...},"attendees":[{"email":"..."}]}. Add ?sendUpdates=all to notify. Always include '
        "real title, times and attendees."
    ),
    "outlook_calendar": (
        "Microsoft Graph (base https://graph.microsoft.com/v1.0). READ FIRST: GET /me/events or "
        "GET /me/calendarView?startDateTime=..&endDateTime=.. , get GET /me/events/{id}. Create event: "
        'POST /me/events with {"subject":"<real title>","start":{"dateTime":"...","timeZone":"UTC"},'
        '"end":{...},"attendees":[{"emailAddress":{"address":"..."},"type":"required"}]}. Always '
        "include real subject, times and attendees."
    ),
    "calendly": (
        "Base https://api.calendly.com (Bearer). READ FIRST: GET /users/me (get your user URI), list "
        "GET /scheduled_events?user=<uri>, invitees GET /scheduled_events/{uuid}/invitees. Create a "
        "single-use scheduling link: POST /scheduling_links with a real owner (event type URI) and "
        "max_event_count. Cancel: POST /scheduled_events/{uuid}/cancellation with a real reason. "
        "Provide real URIs and values, not placeholders."
    ),
}


def api_hint(provider_name: str) -> str:
    """Return a verified API path hint for a provider, or '' if none is known."""
    return API_HINTS.get(provider_name, "")


def profile_for(provider_name: str) -> ApiProfile:
    """Return the API profile for a provider (a NONE-auth empty profile if unknown)."""
    return PROVIDER_API.get(provider_name, ApiProfile("", AuthStyle.NONE))


def resolve_base_url(profile: ApiProfile, values: dict[str, str]) -> str:
    """Fill ``{placeholder}`` tokens in ``base_url`` from config/credential values.

    Missing placeholders are left empty so a misconfiguration is visible rather
    than silently calling the wrong host.
    """
    url = profile.base_url
    if not url or "{" not in url:
        return url
    out = url
    import re

    for token in re.findall(r"\{([a-z0-9_]+)\}", url):
        out = out.replace("{" + token + "}", str(values.get(token, "")).rstrip("/"))
    return out


__all__ = ["AuthStyle", "ApiProfile", "PROVIDER_API", "API_HINTS", "profile_for", "resolve_base_url", "api_hint"]
