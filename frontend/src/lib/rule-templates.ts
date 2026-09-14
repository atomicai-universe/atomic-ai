/**
 * Curated automation-rule templates (BUILD.md).
 *
 * At least 20 high-value, click-to-add rules for every one of the twelve
 * categories, spanning the work, activities, and applications that matter to
 * teams, developers, and workers. Users click a rule to load it into the
 * create form instead of typing.
 */

import type { Category } from "@/lib/providers";

export interface RuleTemplate {
  title: string;
  category: Category;
  prompt: string;
}

export const RULE_TEMPLATES_BY_CATEGORY: Record<Category, RuleTemplate[]> = {
  email: [
    {
      title: "Draft-only replies",
      category: "email",
      prompt:
        "When replying to any incoming email, save the response as a draft and never send it without explicit human approval.",
    },
    {
      title: "Summarize long threads",
      category: "email",
      prompt:
        "For email threads longer than five messages, generate a concise summary with the key decisions and open questions.",
    },
    {
      title: "Priority triage",
      category: "email",
      prompt:
        "Classify every new email as Urgent, Normal, or Low based on sender, subject, and content, and label it accordingly.",
    },
    {
      title: "VIP fast-track",
      category: "email",
      prompt:
        "When email arrives from a known VIP contact or domain, flag it immediately and draft a prompt, courteous reply.",
    },
    {
      title: "Auto-categorize by topic",
      category: "email",
      prompt:
        "Sort incoming email into folders or labels by topic (billing, support, sales, internal) using the message content.",
    },
    {
      title: "Detect and flag phishing",
      category: "email",
      prompt:
        "Scan incoming email for suspicious links, spoofed senders, and urgent payment requests, and quarantine anything risky.",
    },
    {
      title: "Meeting request handling",
      category: "email",
      prompt:
        "When an email requests a meeting, propose available time slots from the calendar instead of committing automatically.",
    },
    {
      title: "Out-of-office coverage",
      category: "email",
      prompt:
        "During a configured out-of-office window, auto-acknowledge new email and route anything urgent to the backup contact.",
    },
    {
      title: "Unsubscribe from noise",
      category: "email",
      prompt:
        "Identify recurring low-value newsletters and promotional email and suggest unsubscribing rather than archiving repeatedly.",
    },
    {
      title: "Extract action items",
      category: "email",
      prompt:
        "From each email, extract explicit action items with owners and due dates and add them to the task list.",
    },
    {
      title: "No mass send without approval",
      category: "email",
      prompt:
        "Never send a single email to more than 20 recipients without approval from a Workspace Admin.",
    },
    {
      title: "Attachment safety",
      category: "email",
      prompt:
        "Do not open or forward email attachments from unknown senders; flag them for manual review instead.",
    },
    {
      title: "Tone and grammar check",
      category: "email",
      prompt:
        "Before saving any drafted reply, check tone and grammar and keep it professional and concise.",
    },
    {
      title: "Follow-up reminders",
      category: "email",
      prompt:
        "If an important sent email gets no reply within three business days, create a follow-up reminder.",
    },
    {
      title: "Redact sensitive data",
      category: "email",
      prompt:
        "Never include passwords, full card numbers, or secrets in any drafted email; redact them automatically.",
    },
    {
      title: "Localize replies",
      category: "email",
      prompt:
        "Detect the sender's language and draft replies in that same language when confident.",
    },
    {
      title: "Thread-aware context",
      category: "email",
      prompt:
        "When drafting a reply, read the full thread first so the response reflects the latest context.",
    },
    {
      title: "Bounce handling",
      category: "email",
      prompt:
        "When an email bounces, log the failure, mark the address as invalid, and notify the sender's owner.",
    },
    {
      title: "Signature consistency",
      category: "email",
      prompt:
        "Apply the correct team or personal signature block to every drafted email.",
    },
    {
      title: "Archive resolved threads",
      category: "email",
      prompt:
        "Once a thread is marked resolved, archive it and remove it from the active inbox view.",
    },
  ],
  email_marketing: [
    {
      title: "Campaign approval gate",
      category: "email_marketing",
      prompt:
        "Before launching any campaign to more than 100 recipients, require Workspace Admin review of audience and content.",
    },
    {
      title: "Respect unsubscribes",
      category: "email_marketing",
      prompt:
        "Never send marketing email to unsubscribed addresses and always include a working unsubscribe link.",
    },
    {
      title: "Segment before sending",
      category: "email_marketing",
      prompt:
        "Send campaigns only to the intended segment and confirm the audience filter before dispatch.",
    },
    {
      title: "A/B subject testing",
      category: "email_marketing",
      prompt:
        "For new campaigns, propose two subject-line variants and split-test them on a small sample first.",
    },
    {
      title: "Send-time optimization",
      category: "email_marketing",
      prompt:
        "Schedule campaigns for each recipient's likely-best local send time rather than one global time.",
    },
    {
      title: "Suppress recent contacts",
      category: "email_marketing",
      prompt:
        "Do not email a contact who received a campaign in the last 48 hours unless explicitly approved.",
    },
    {
      title: "Spam-score check",
      category: "email_marketing",
      prompt:
        "Run every campaign through a spam-score check and revise copy that risks landing in spam.",
    },
    {
      title: "Personalization tokens",
      category: "email_marketing",
      prompt:
        "Verify that all personalization tokens resolve correctly before sending; never send a broken merge field.",
    },
    {
      title: "List hygiene",
      category: "email_marketing",
      prompt:
        "Regularly flag hard-bounced and inactive addresses for removal to protect sender reputation.",
    },
    {
      title: "Consent compliance",
      category: "email_marketing",
      prompt:
        "Only email contacts with recorded opt-in consent and honor regional consent rules.",
    },
    {
      title: "Throttle large sends",
      category: "email_marketing",
      prompt:
        "Send large campaigns in throttled batches to protect deliverability and monitor bounce rates.",
    },
    {
      title: "Track and report",
      category: "email_marketing",
      prompt:
        "After each campaign, summarize open, click, bounce, and unsubscribe rates for the team.",
    },
    {
      title: "Reengagement flow",
      category: "email_marketing",
      prompt:
        "For contacts inactive over 90 days, run a reengagement sequence before removing them.",
    },
    {
      title: "Brand and legal footer",
      category: "email_marketing",
      prompt:
        "Ensure every campaign includes the required legal footer, physical address, and brand styling.",
    },
    {
      title: "Link integrity",
      category: "email_marketing",
      prompt:
        "Verify all campaign links resolve and use tracking parameters before sending.",
    },
    {
      title: "Frequency cap",
      category: "email_marketing",
      prompt:
        "Never exceed the configured maximum number of marketing emails per contact per week.",
    },
    {
      title: "Preview across clients",
      category: "email_marketing",
      prompt:
        "Preview each campaign in major email clients to catch rendering issues before send.",
    },
    {
      title: "Hold on high complaints",
      category: "email_marketing",
      prompt:
        "If spam-complaint rate exceeds the threshold, pause sending and alert a Workspace Admin.",
    },
    {
      title: "Localized campaigns",
      category: "email_marketing",
      prompt:
        "Send region-specific content and language variants to the matching audience segments.",
    },
    {
      title: "Post-send follow-up",
      category: "email_marketing",
      prompt:
        "Trigger an automated follow-up to engaged openers who did not click within two days.",
    },
  ],
  social: [
    {
      title: "Escalate complaints",
      category: "social",
      prompt:
        "When a social message is a complaint or refund request, escalate to a human reviewer and do not auto-resolve.",
    },
    {
      title: "After-hours acknowledgment",
      category: "social",
      prompt:
        "Outside business hours, acknowledge new direct messages with a polite holding reply and queue for morning.",
    },
    {
      title: "Brand-safe tone",
      category: "social",
      prompt:
        "Keep all social replies friendly, professional, and on-brand, and never argue publicly.",
    },
    {
      title: "No public PII",
      category: "social",
      prompt:
        "Never post personal or account details in public replies; move those conversations to private messages.",
    },
    {
      title: "Sentiment routing",
      category: "social",
      prompt:
        "Route strongly negative messages to a human and positive testimonials to the marketing team.",
    },
    {
      title: "Response SLA",
      category: "social",
      prompt:
        "Draft a first response to new direct messages within the configured SLA window.",
    },
    {
      title: "Approval for public posts",
      category: "social",
      prompt:
        "Never publish a public post or reply on an official page without human approval.",
    },
    {
      title: "FAQ auto-answers",
      category: "social",
      prompt:
        "Answer common questions with approved FAQ responses and offer a human handoff option.",
    },
    {
      title: "Crisis keyword watch",
      category: "social",
      prompt:
        "Watch for crisis or legal-risk keywords and immediately alert a Workspace Admin.",
    },
    {
      title: "Language matching",
      category: "social",
      prompt:
        "Detect the message language and reply in the same language when confident.",
    },
    {
      title: "Spam and bot filtering",
      category: "social",
      prompt:
        "Filter out spam and bot messages and do not engage with obvious scams.",
    },
    {
      title: "Consistent handles",
      category: "social",
      prompt:
        "Reference correct official account handles and links in every reply.",
    },
    {
      title: "Rate-limit outreach",
      category: "social",
      prompt:
        "Do not send more than the configured number of proactive outreach messages per hour.",
    },
    {
      title: "Log every interaction",
      category: "social",
      prompt:
        "Record each social interaction with topic, sentiment, and outcome for reporting.",
    },
    {
      title: "Influencer flagging",
      category: "social",
      prompt:
        "Flag messages from high-follower or verified accounts for priority human review.",
    },
    {
      title: "No unapproved promises",
      category: "social",
      prompt:
        "Never promise refunds, discounts, or timelines that require approval.",
    },
    {
      title: "Media moderation",
      category: "social",
      prompt:
        "Do not share user-submitted images or media without moderation approval.",
    },
    {
      title: "Handoff context",
      category: "social",
      prompt:
        "When handing a conversation to a human, include a summary and prior context.",
    },
    {
      title: "Scheduled content review",
      category: "social",
      prompt:
        "Hold scheduled social posts for review if they mention pricing, legal, or partnerships.",
    },
    {
      title: "Follow-up on unresolved",
      category: "social",
      prompt:
        "If a customer issue is unresolved after the first reply, create a follow-up task.",
    },
  ],
  office: [
    {
      title: "Naming convention",
      category: "office",
      prompt:
        "Name new documents using the pattern YYYY-MM-DD_Project_Title and save to the correct team folder.",
    },
    {
      title: "No external sharing without approval",
      category: "office",
      prompt:
        "Never share a document outside the workspace domain without Workspace Admin approval.",
    },
    {
      title: "Version before major edits",
      category: "office",
      prompt:
        "Create a version snapshot before making large edits so changes can be rolled back.",
    },
    {
      title: "Template compliance",
      category: "office",
      prompt:
        "Start new documents from the approved company template rather than a blank file.",
    },
    {
      title: "Redact sensitive content",
      category: "office",
      prompt:
        "Detect and redact secrets, card numbers, and personal data before sharing any document.",
    },
    {
      title: "Least-privilege sharing",
      category: "office",
      prompt:
        "Share with view-only access by default and grant edit access only when required.",
    },
    {
      title: "Auto-summarize long docs",
      category: "office",
      prompt:
        "For documents over ten pages, generate an executive summary at the top.",
    },
    {
      title: "Consistent formatting",
      category: "office",
      prompt:
        "Apply consistent headings, fonts, and styles based on the brand style guide.",
    },
    {
      title: "Spreadsheet validation",
      category: "office",
      prompt:
        "Validate formulas and flag broken references or divide-by-zero errors in spreadsheets.",
    },
    {
      title: "Backup on finalize",
      category: "office",
      prompt:
        "When a document is marked final, copy it to the archive folder automatically.",
    },
    {
      title: "Link instead of attach",
      category: "office",
      prompt:
        "Prefer sharing document links over emailing large attachments.",
    },
    {
      title: "Access review",
      category: "office",
      prompt:
        "Periodically review who has access to shared documents and revoke stale permissions.",
    },
    {
      title: "Change summaries",
      category: "office",
      prompt:
        "When editing a shared document, post a short summary of what changed for collaborators.",
    },
    {
      title: "Read-only for published",
      category: "office",
      prompt:
        "Set published or signed documents to read-only to prevent accidental edits.",
    },
    {
      title: "Consistent slide branding",
      category: "office",
      prompt:
        "Ensure presentations use the approved theme, logo, and color palette.",
    },
    {
      title: "Detect duplicate files",
      category: "office",
      prompt:
        "Flag likely duplicate documents and suggest consolidating them.",
    },
    {
      title: "Auto-generate table of contents",
      category: "office",
      prompt:
        "Add or update a table of contents for long structured documents.",
    },
    {
      title: "Currency and units check",
      category: "office",
      prompt:
        "Verify currency symbols and units are consistent across financial spreadsheets.",
    },
    {
      title: "Approval for deletions",
      category: "office",
      prompt:
        "Never permanently delete a shared document without approval and a backup.",
    },
    {
      title: "Expiring share links",
      category: "office",
      prompt:
        "Use expiring links for externally shared documents where supported.",
    },
  ],
  developer: [
    {
      title: "Require review before merge",
      category: "developer",
      prompt:
        "Never merge a pull request without at least one approving human review.",
    },
    {
      title: "Summarize pull requests",
      category: "developer",
      prompt:
        "For every pull request, summarize the change, risks, and affected areas.",
    },
    {
      title: "Flag missing tests",
      category: "developer",
      prompt:
        "Detect code changes that lack corresponding tests and request them before merge.",
    },
    {
      title: "Auto-triage issues",
      category: "developer",
      prompt:
        "Categorize new issues as bug, feature, or question, set priority, and assign an owner.",
    },
    {
      title: "Enforce branch naming",
      category: "developer",
      prompt:
        "Require branch names to follow the team convention (type/short-description).",
    },
    {
      title: "Block secrets in commits",
      category: "developer",
      prompt:
        "Scan diffs for API keys, tokens, and passwords and block commits that contain secrets.",
    },
    {
      title: "Link issues to PRs",
      category: "developer",
      prompt:
        "Ensure each pull request references the issue it resolves before it is merged.",
    },
    {
      title: "Stale PR reminders",
      category: "developer",
      prompt:
        "Remind reviewers about pull requests with no activity for two business days.",
    },
    {
      title: "Conventional commits",
      category: "developer",
      prompt:
        "Encourage conventional commit messages and flag ones that do not follow the format.",
    },
    {
      title: "Protect main branch",
      category: "developer",
      prompt:
        "Never allow direct pushes to the main branch; require pull requests.",
    },
    {
      title: "CI must pass",
      category: "developer",
      prompt:
        "Do not merge until continuous integration checks pass successfully.",
    },
    {
      title: "Dependency risk check",
      category: "developer",
      prompt:
        "Flag pull requests that add unpinned or newly published dependencies for extra review.",
    },
    {
      title: "Auto-label by area",
      category: "developer",
      prompt:
        "Label issues and pull requests by affected module or component.",
    },
    {
      title: "Release notes draft",
      category: "developer",
      prompt:
        "When a release branch is cut, draft release notes from merged pull requests.",
    },
    {
      title: "Escalate P1 bugs",
      category: "developer",
      prompt:
        "Immediately notify the on-call owner when a priority-one bug is filed.",
    },
    {
      title: "Close duplicates",
      category: "developer",
      prompt:
        "Detect duplicate issues and link them to the canonical one rather than working both.",
    },
    {
      title: "Enforce PR size",
      category: "developer",
      prompt:
        "Flag very large pull requests and suggest splitting them for easier review.",
    },
    {
      title: "Track SLA on issues",
      category: "developer",
      prompt:
        "Ensure new customer-reported issues get a first response within the SLA window.",
    },
    {
      title: "No force-push to shared",
      category: "developer",
      prompt:
        "Never force-push to shared branches without explicit approval.",
    },
    {
      title: "Post-merge verification",
      category: "developer",
      prompt:
        "After merge, verify the deploy pipeline succeeded and alert on failure.",
    },
  ],
  crm: [
    {
      title: "Log every stage change",
      category: "crm",
      prompt:
        "When a deal changes stage, log a concise note with the reason and next step.",
    },
    {
      title: "Discount approval",
      category: "crm",
      prompt:
        "Never apply a discount over 10 percent to a deal without Workspace Admin approval.",
    },
    {
      title: "Deduplicate contacts",
      category: "crm",
      prompt:
        "Detect and merge duplicate contacts and companies to keep the CRM clean.",
    },
    {
      title: "Follow-up cadence",
      category: "crm",
      prompt:
        "Create follow-up tasks for open deals with no activity in the last seven days.",
    },
    {
      title: "Lead scoring",
      category: "crm",
      prompt:
        "Score inbound leads by fit and engagement and route high-scoring leads to sales.",
    },
    {
      title: "Required fields",
      category: "crm",
      prompt:
        "Do not mark a deal as won until required fields (amount, close date, owner) are set.",
    },
    {
      title: "Assign by territory",
      category: "crm",
      prompt:
        "Route new leads to the correct owner based on region or account rules.",
    },
    {
      title: "Meeting notes to CRM",
      category: "crm",
      prompt:
        "After a sales meeting, log a summary and next steps to the related record.",
    },
    {
      title: "Stale deal cleanup",
      category: "crm",
      prompt:
        "Flag deals stuck in a stage beyond the expected time for review.",
    },
    {
      title: "Data privacy on export",
      category: "crm",
      prompt:
        "Never export contact lists outside the workspace without approval.",
    },
    {
      title: "Renewal reminders",
      category: "crm",
      prompt:
        "Create renewal tasks ahead of contract end dates for existing customers.",
    },
    {
      title: "Consistent naming",
      category: "crm",
      prompt:
        "Standardize company and contact naming to avoid fragmented records.",
    },
    {
      title: "Enrich new records",
      category: "crm",
      prompt:
        "Enrich new contacts with available public firmographic data where permitted.",
    },
    {
      title: "Pipeline hygiene alerts",
      category: "crm",
      prompt:
        "Alert owners about deals missing close dates or next steps.",
    },
    {
      title: "Win/loss capture",
      category: "crm",
      prompt:
        "Prompt for a win or loss reason when a deal is closed.",
    },
    {
      title: "Escalate large deals",
      category: "crm",
      prompt:
        "Notify sales leadership when a deal exceeds the configured value threshold.",
    },
    {
      title: "No spammy sequences",
      category: "crm",
      prompt:
        "Cap the number of automated outreach steps per lead to avoid over-contacting.",
    },
    {
      title: "Sync activity",
      category: "crm",
      prompt:
        "Log emails and calls against the correct CRM record automatically.",
    },
    {
      title: "Quote accuracy",
      category: "crm",
      prompt:
        "Verify quote line items and totals before sending to the customer.",
    },
    {
      title: "Handoff to success",
      category: "crm",
      prompt:
        "On deal win, create an onboarding handoff task for the customer success team.",
    },
  ],
  support: [
    {
      title: "First-response SLA",
      category: "support",
      prompt:
        "Draft a helpful first response to each new ticket within the SLA window.",
    },
    {
      title: "Escalate angry customers",
      category: "support",
      prompt:
        "If a message shows strong negative sentiment or mentions cancellation, escalate to a human immediately.",
    },
    {
      title: "Auto-tag tickets",
      category: "support",
      prompt:
        "Tag each ticket with the correct topic, product area, and priority.",
    },
    {
      title: "Suggest KB articles",
      category: "support",
      prompt:
        "Attach relevant knowledge-base articles to responses to speed resolution.",
    },
    {
      title: "Never auto-close open issues",
      category: "support",
      prompt:
        "Do not auto-close a ticket while the customer still has an unanswered question.",
    },
    {
      title: "Detect duplicates",
      category: "support",
      prompt:
        "Link duplicate tickets from the same customer instead of handling them separately.",
    },
    {
      title: "VIP priority",
      category: "support",
      prompt:
        "Prioritize tickets from VIP or enterprise accounts and notify their owner.",
    },
    {
      title: "Sentiment tracking",
      category: "support",
      prompt:
        "Track sentiment across the conversation and flag deteriorating threads.",
    },
    {
      title: "Refund approval",
      category: "support",
      prompt:
        "Never approve a refund above the configured amount without a manager's approval.",
    },
    {
      title: "Language matching",
      category: "support",
      prompt:
        "Respond in the customer's language when confident.",
    },
    {
      title: "Follow-up on silence",
      category: "support",
      prompt:
        "If a customer does not reply within the follow-up window, send a gentle check-in.",
    },
    {
      title: "Escalation path",
      category: "support",
      prompt:
        "Route unresolved technical issues to the correct engineering queue with context.",
    },
    {
      title: "Draft KB from resolutions",
      category: "support",
      prompt:
        "When a novel issue is resolved, draft a knowledge-base article from the resolution.",
    },
    {
      title: "CSAT after close",
      category: "support",
      prompt:
        "Send a satisfaction survey after a ticket is resolved and closed.",
    },
    {
      title: "Protect account data",
      category: "support",
      prompt:
        "Verify identity before sharing any account-specific information.",
    },
    {
      title: "Macro consistency",
      category: "support",
      prompt:
        "Use approved response macros for common issues to keep answers consistent.",
    },
    {
      title: "SLA breach alerts",
      category: "support",
      prompt:
        "Alert the team before a ticket is about to breach its SLA.",
    },
    {
      title: "Categorize feedback",
      category: "support",
      prompt:
        "Route feature requests and bug reports to the correct product channel.",
    },
    {
      title: "Merge related threads",
      category: "support",
      prompt:
        "Consolidate multiple channels from one customer into a single conversation.",
    },
    {
      title: "Knowledge freshness",
      category: "support",
      prompt:
        "Flag knowledge-base articles that are outdated based on recent product changes.",
    },
  ],
  erp: [
    {
      title: "Invoice approval threshold",
      category: "erp",
      prompt:
        "Route any invoice or payment above 1000 in value to a Workspace Admin before it is issued or paid.",
    },
    {
      title: "Daily reconciliation",
      category: "erp",
      prompt:
        "At end of day, reconcile new transactions against open invoices and flag mismatches.",
    },
    {
      title: "Duplicate payment guard",
      category: "erp",
      prompt:
        "Detect and block duplicate payments to the same vendor and invoice number.",
    },
    {
      title: "Purchase order matching",
      category: "erp",
      prompt:
        "Require three-way match (PO, receipt, invoice) before approving vendor payments.",
    },
    {
      title: "Tax accuracy",
      category: "erp",
      prompt:
        "Verify tax rates and totals on invoices before they are sent.",
    },
    {
      title: "Late payment reminders",
      category: "erp",
      prompt:
        "Send reminders for overdue customer invoices on a defined schedule.",
    },
    {
      title: "Segregation of duties",
      category: "erp",
      prompt:
        "Never let the same actor both create and approve a payment.",
    },
    {
      title: "Currency consistency",
      category: "erp",
      prompt:
        "Ensure multi-currency transactions use correct and current exchange rates.",
    },
    {
      title: "Expense policy check",
      category: "erp",
      prompt:
        "Flag expense entries that violate the company expense policy for review.",
    },
    {
      title: "Month-end checklist",
      category: "erp",
      prompt:
        "Run the month-end close checklist and flag any incomplete items.",
    },
    {
      title: "Audit trail",
      category: "erp",
      prompt:
        "Log every financial change with actor, timestamp, and reason for the audit trail.",
    },
    {
      title: "Vendor verification",
      category: "erp",
      prompt:
        "Verify new vendor bank details through a second channel before the first payment.",
    },
    {
      title: "Budget threshold alerts",
      category: "erp",
      prompt:
        "Alert owners when spending in a category approaches its budget limit.",
    },
    {
      title: "No backdated entries",
      category: "erp",
      prompt:
        "Prevent backdated financial entries without explicit approval.",
    },
    {
      title: "Refund controls",
      category: "erp",
      prompt:
        "Require approval and a reason for any customer refund above the threshold.",
    },
    {
      title: "Statement matching",
      category: "erp",
      prompt:
        "Match bank statement lines to recorded transactions and flag unmatched items.",
    },
    {
      title: "Recurring billing checks",
      category: "erp",
      prompt:
        "Verify recurring subscription charges before they run each cycle.",
    },
    {
      title: "Credit limit enforcement",
      category: "erp",
      prompt:
        "Do not process orders that exceed a customer's approved credit limit without approval.",
    },
    {
      title: "Sensitive data handling",
      category: "erp",
      prompt:
        "Never expose full bank or card numbers in reports or messages; mask them.",
    },
    {
      title: "Year-end preparation",
      category: "erp",
      prompt:
        "Compile required year-end financial summaries and flag discrepancies early.",
    },
  ],
  collaboration: [
    {
      title: "Daily standup summary",
      category: "collaboration",
      prompt:
        "Each morning, post a concise summary of yesterday's completed work and today's priorities.",
    },
    {
      title: "No broad mentions without approval",
      category: "collaboration",
      prompt:
        "Never use @channel or @everyone without Workspace Admin approval.",
    },
    {
      title: "Meeting notes capture",
      category: "collaboration",
      prompt:
        "After a meeting, post action items with owners and due dates to the team channel.",
    },
    {
      title: "Quiet hours respect",
      category: "collaboration",
      prompt:
        "Do not send non-urgent messages outside a member's configured working hours.",
    },
    {
      title: "Escalate urgent flags",
      category: "collaboration",
      prompt:
        "When a message is flagged urgent, notify the on-call owner promptly.",
    },
    {
      title: "Channel hygiene",
      category: "collaboration",
      prompt:
        "Suggest archiving inactive channels and consolidating duplicated ones.",
    },
    {
      title: "Thread replies",
      category: "collaboration",
      prompt:
        "Reply in threads to keep channels readable rather than posting top-level.",
    },
    {
      title: "No sensitive data in chat",
      category: "collaboration",
      prompt:
        "Never post secrets or personal data in chat; use the secure vault instead.",
    },
    {
      title: "Auto-answer FAQs",
      category: "collaboration",
      prompt:
        "Answer common internal questions from approved documentation with a source link.",
    },
    {
      title: "Meeting scheduling",
      category: "collaboration",
      prompt:
        "Propose meeting times from shared availability instead of booking unilaterally.",
    },
    {
      title: "Recap long threads",
      category: "collaboration",
      prompt:
        "Summarize long or fast-moving threads on request for people catching up.",
    },
    {
      title: "Reaction-based triage",
      category: "collaboration",
      prompt:
        "Treat configured emoji reactions as signals to create tasks or mark resolved.",
    },
    {
      title: "Onboarding welcome",
      category: "collaboration",
      prompt:
        "Send new members a welcome message with key channels and getting-started links.",
    },
    {
      title: "Escalation routing",
      category: "collaboration",
      prompt:
        "Route help requests to the right team channel based on topic.",
    },
    {
      title: "Do-not-disturb aware",
      category: "collaboration",
      prompt:
        "Honor do-not-disturb status and queue non-urgent notifications.",
    },
    {
      title: "Decision logging",
      category: "collaboration",
      prompt:
        "Capture decisions made in chat into a durable decision log.",
    },
    {
      title: "Poll for consensus",
      category: "collaboration",
      prompt:
        "Use quick polls to gather input on low-stakes team decisions.",
    },
    {
      title: "Recording consent",
      category: "collaboration",
      prompt:
        "Confirm consent before recording meetings and share recordings only with attendees.",
    },
    {
      title: "Reminder nudges",
      category: "collaboration",
      prompt:
        "Nudge owners about overdue action items from previous meetings.",
    },
    {
      title: "Cross-post control",
      category: "collaboration",
      prompt:
        "Avoid duplicating the same announcement across many channels without approval.",
    },
  ],
  cloud: [
    {
      title: "Tag every resource",
      category: "cloud",
      prompt:
        "Before creating any cloud resource, apply required cost-center and owner tags.",
    },
    {
      title: "Least privilege",
      category: "cloud",
      prompt:
        "Use least-privilege IAM permissions and avoid wildcard admin grants.",
    },
    {
      title: "Approval for production",
      category: "cloud",
      prompt:
        "Require human approval for any change to production infrastructure.",
    },
    {
      title: "Cost guardrails",
      category: "cloud",
      prompt:
        "Alert owners when projected spend exceeds the configured budget threshold.",
    },
    {
      title: "No public buckets",
      category: "cloud",
      prompt:
        "Never make storage buckets or blobs publicly readable without explicit approval.",
    },
    {
      title: "Encrypt by default",
      category: "cloud",
      prompt:
        "Enable encryption at rest and in transit for all new data stores.",
    },
    {
      title: "Infra changes via code",
      category: "cloud",
      prompt:
        "Make infrastructure changes through reviewed code, not manual console edits.",
    },
    {
      title: "Backup verification",
      category: "cloud",
      prompt:
        "Verify that backups exist and are restorable for critical data stores.",
    },
    {
      title: "Right-size resources",
      category: "cloud",
      prompt:
        "Flag over-provisioned or idle resources and suggest right-sizing.",
    },
    {
      title: "Rotate credentials",
      category: "cloud",
      prompt:
        "Rotate access keys and secrets on the configured schedule.",
    },
    {
      title: "MFA enforcement",
      category: "cloud",
      prompt:
        "Require multi-factor authentication for privileged cloud accounts.",
    },
    {
      title: "Region compliance",
      category: "cloud",
      prompt:
        "Deploy data-storing resources only in approved regions for compliance.",
    },
    {
      title: "Delete guardrails",
      category: "cloud",
      prompt:
        "Require approval and a snapshot before deleting stateful resources.",
    },
    {
      title: "Drift detection",
      category: "cloud",
      prompt:
        "Detect and report configuration drift from the declared infrastructure state.",
    },
    {
      title: "Security group review",
      category: "cloud",
      prompt:
        "Flag overly permissive network security rules such as open 0.0.0.0/0 access.",
    },
    {
      title: "Auto-scaling limits",
      category: "cloud",
      prompt:
        "Set sensible minimum and maximum bounds on auto-scaling groups.",
    },
    {
      title: "Log retention",
      category: "cloud",
      prompt:
        "Ensure audit and access logs are enabled and retained for the required period.",
    },
    {
      title: "Patch cadence",
      category: "cloud",
      prompt:
        "Track and apply security patches to managed compute within the SLA.",
    },
    {
      title: "Terminate on idle",
      category: "cloud",
      prompt:
        "Shut down non-production environments outside business hours to save cost.",
    },
    {
      title: "Incident runbook",
      category: "cloud",
      prompt:
        "On a production alert, open an incident and post the runbook link to the channel.",
    },
  ],
  hr: [
    {
      title: "Interview scheduling",
      category: "hr",
      prompt:
        "When a candidate advances, propose interview times from open calendar slots and send confirmations.",
    },
    {
      title: "Protect candidate data",
      category: "hr",
      prompt:
        "Never share candidate personal data outside the hiring team.",
    },
    {
      title: "Consistent scorecards",
      category: "hr",
      prompt:
        "Require structured interview scorecards before moving a candidate forward.",
    },
    {
      title: "Timely candidate updates",
      category: "hr",
      prompt:
        "Ensure candidates receive a status update within the configured response window.",
    },
    {
      title: "Bias-aware language",
      category: "hr",
      prompt:
        "Review job posts and messages for biased or exclusionary language.",
    },
    {
      title: "Offer approval",
      category: "hr",
      prompt:
        "Require approval before extending any offer or stating compensation.",
    },
    {
      title: "Onboarding checklist",
      category: "hr",
      prompt:
        "On a new hire, create the onboarding checklist and assign owners for each task.",
    },
    {
      title: "Document compliance",
      category: "hr",
      prompt:
        "Verify required onboarding documents are collected before the start date.",
    },
    {
      title: "Equal process",
      category: "hr",
      prompt:
        "Apply the same interview stages and criteria to all candidates for a role.",
    },
    {
      title: "Reference checks",
      category: "hr",
      prompt:
        "Complete reference checks before finalizing an offer where required.",
    },
    {
      title: "PTO handling",
      category: "hr",
      prompt:
        "Route time-off requests to the correct approver and update the calendar on approval.",
    },
    {
      title: "Confidential comp data",
      category: "hr",
      prompt:
        "Never expose salary or compensation data outside authorized roles.",
    },
    {
      title: "Interview load balance",
      category: "hr",
      prompt:
        "Distribute interview assignments fairly across the panel.",
    },
    {
      title: "Candidate feedback capture",
      category: "hr",
      prompt:
        "Collect interviewer feedback promptly after each interview.",
    },
    {
      title: "Rejection courtesy",
      category: "hr",
      prompt:
        "Send respectful, timely rejection messages to candidates not moving forward.",
    },
    {
      title: "Right-to-work checks",
      category: "hr",
      prompt:
        "Confirm required eligibility documentation before start date.",
    },
    {
      title: "Offboarding revocation",
      category: "hr",
      prompt:
        "On departure, trigger access revocation and asset return tasks.",
    },
    {
      title: "Policy acknowledgment",
      category: "hr",
      prompt:
        "Ensure new hires acknowledge required policies during onboarding.",
    },
    {
      title: "Diversity reporting",
      category: "hr",
      prompt:
        "Aggregate hiring funnel metrics without exposing individual personal data.",
    },
    {
      title: "Escalate stalled reqs",
      category: "hr",
      prompt:
        "Flag open requisitions with no pipeline movement for recruiter review.",
    },
  ],
  calendar: [
    {
      title: "Propose, don't book",
      category: "calendar",
      prompt:
        "Propose meeting times from open availability rather than booking unilaterally.",
    },
    {
      title: "Respect working hours",
      category: "calendar",
      prompt:
        "Only schedule meetings within each attendee's configured working hours.",
    },
    {
      title: "Buffer between meetings",
      category: "calendar",
      prompt:
        "Leave a buffer between back-to-back meetings when scheduling.",
    },
    {
      title: "Timezone accuracy",
      category: "calendar",
      prompt:
        "Always confirm and display times in each attendee's timezone.",
    },
    {
      title: "Focus-time protection",
      category: "calendar",
      prompt:
        "Avoid scheduling over blocks marked as focus time without approval.",
    },
    {
      title: "Agenda required",
      category: "calendar",
      prompt:
        "Require a short agenda before confirming any meeting invite.",
    },
    {
      title: "Decline conflicts",
      category: "calendar",
      prompt:
        "Detect double-bookings and propose alternatives instead of overlapping.",
    },
    {
      title: "Auto-add conferencing",
      category: "calendar",
      prompt:
        "Attach a video-conference link to every scheduled meeting.",
    },
    {
      title: "Reminders",
      category: "calendar",
      prompt:
        "Send reminders to attendees ahead of important meetings.",
    },
    {
      title: "Limit meeting length",
      category: "calendar",
      prompt:
        "Default meetings to the shortest reasonable length and flag overly long ones.",
    },
    {
      title: "No-meeting windows",
      category: "calendar",
      prompt:
        "Honor team-wide no-meeting days or windows.",
    },
    {
      title: "Reschedule courtesy",
      category: "calendar",
      prompt:
        "When rescheduling, notify all attendees with the reason and new time.",
    },
    {
      title: "Prep tasks",
      category: "calendar",
      prompt:
        "Create preparation tasks before meetings that require materials.",
    },
    {
      title: "Follow-up capture",
      category: "calendar",
      prompt:
        "After a meeting, capture action items and schedule any needed follow-up.",
    },
    {
      title: "Guest limits",
      category: "calendar",
      prompt:
        "Flag meetings with excessive attendees and suggest trimming the invite list.",
    },
    {
      title: "Recurring cleanup",
      category: "calendar",
      prompt:
        "Review recurring meetings periodically and cancel ones no longer needed.",
    },
    {
      title: "Travel time",
      category: "calendar",
      prompt:
        "Account for travel or transition time when scheduling in-person meetings.",
    },
    {
      title: "Priority holds",
      category: "calendar",
      prompt:
        "Protect high-priority commitments from being overwritten by lower-priority invites.",
    },
    {
      title: "Availability sharing",
      category: "calendar",
      prompt:
        "Share availability via a booking link rather than manual back-and-forth.",
    },
    {
      title: "Consent for external",
      category: "calendar",
      prompt:
        "Confirm before adding external guests to internal recurring meetings.",
    },
  ],
};

/** Flat list of every template across all categories. */
export const ALL_RULE_TEMPLATES: RuleTemplate[] = Object.values(
  RULE_TEMPLATES_BY_CATEGORY,
).flat();
