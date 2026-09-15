---
title: "Atomic AI: Agents for Humans — Building Trustworthy, Voice-First AI Automation on AWS"
description: "How we built a multi-tenant AI automation platform where autonomous agents do the work, but humans stay in control — using Amazon Bedrock, the Strands Agents SDK, Amazon Nova Sonic, and Amazon SNS."
tags:
  - generative-ai
  - amazon-bedrock
  - strands-agents
  - amazon-nova-sonic
  - accessibility
authors:
  - atomic-ai
date: 2026-09-15
---

# Atomic AI: Agents for Humans — Building Trustworthy, Voice-First AI Automation on AWS

Most "AI agent" demos end at the exciting part: the agent decides to do something. The hard part — the part that decides whether a real team will ever let an agent near their inbox, their CRM, or their production tooling — is everything that happens *around* that decision. Who approves it? What happens when the model is wrong? Can a person who can't stare at a dashboard still stay in control?

We built **Atomic AI** to answer those questions. It's a multi-tenant platform where teams connect their apps, write automation rules in plain language, and deploy autonomous agents to do cross-platform work — but where **nothing high-impact happens without a human saying yes**, and where that human can be fully in the loop by voice and by text message, not just by clicking around a screen.

We call the philosophy **Agents for Humans**: the agent's job is to do the tedious work and propose actions; the human's job is to decide. This post walks through how we built that on AWS — with [Amazon Bedrock](https://aws.amazon.com/bedrock/), the [Strands Agents SDK](https://strandsagents.com/), [Amazon Nova Sonic](https://aws.amazon.com/ai/generative-ai/nova/), and [Amazon SNS](https://aws.amazon.com/sns/) — and the design lessons that made it trustworthy.

## The problem: capable agents, zero trust

Every team already lives across a dozen tools — Gmail, Slack, Jira, HubSpot, Stripe, Google Drive. The "glue" between them is still a human copying context from one tab to another. Agents can do that glue work. But handing an autonomous agent write access to your email and your CRM is genuinely scary: one bad tool call sends the wrong reply to a customer or pushes a change nobody signed off on.

So we set three non-negotiable requirements:

1. **Agents powerful enough** to orchestrate real cross-platform workflows.
2. **A human-in-the-loop approval system** so no high-impact action executes without a person approving it.
3. **Genuine accessibility** — someone who is blind, or simply away from their desk, can review and approve entirely by voice and get notified on their phone.

## Architecture at a glance

Atomic AI runs entirely in Docker Compose: a FastAPI (Python 3.14) backend, a Next.js 16 frontend, PostgreSQL, Redis, and an async worker.

```
Clients (browser, voice, phone/SMS, provider webhooks)
        │
Frontend — Next.js 16 (workspace portal, approvals hub, account, voice widget, admin)
        │  authenticated API + WebSocket
Backend  — FastAPI: auth/RBAC, Voice Gateway, REST/WS API, webhooks
           services: Approval · Agent Engine · Integration Vault · Rules · SNS SMS · Audit
        │  run / notify
Agent Runtime & AWS — ARQ worker · Amazon Bedrock · Amazon Nova Sonic · Amazon SNS
        │  read / write
Data — PostgreSQL 18 · Redis 8 · external app providers (OAuth / MCP)
```

The AWS services share one credential chain and region, which kept the integration surface small: Bedrock powers the agents, Nova Sonic powers voice, and SNS powers SMS — all from the same IAM principal.

## The agent engine: Strands + Amazon Bedrock, with a gate

The agent loop is built on the **Strands Agents SDK**, which defaults to Amazon Bedrock as its model provider. Tools are wired in via the Model Context Protocol (MCP), so each connected app exposes provider-specific capabilities to the agent.

The single most important piece isn't the loop — it's the **interception**. Strands lets you hook `before_tool_call`. We use it to classify whether a proposed tool call is *high-impact* (send an email, push a change, move money) and, if so, we **pause execution and create an approval request instead of running the tool**:

```python
async def before_tool_call(tool_name, arguments):
    if not is_high_impact(tool_name, arguments):
        return ToolCallDecision.allow()          # read-only: proceed

    # High-impact: persist a PENDING approval (scrubbed args) and PAUSE.
    request_id = await persist_pending_approval(tool_name, arguments)
    await notify_reviewers(request_id)           # WebSocket + SMS
    return ToolCallDecision.pause(request_id)
```

The proposed action lands in a **Collaborative Approval Hub** — a real-time queue delivered over WebSockets. An Owner or Admin reviews the exact action, and can approve, edit, regenerate, schedule, or reject it. Only on approval does the tool actually execute. Every step is written to an append-only, secret-scrubbed audit log.

This one design choice is what turns "an agent with my credentials" into "an assistant that drafts, and waits for me."

### Keeping agents affordable by construction

An early version had a "process the inbox" run that kept every email and draft in context and re-sent them each turn — quadratic token growth, and a very unpleasant bill. We fixed it structurally rather than hopefully:

- **Hard per-run caps** passed to Strands: max turns, max total tokens, max output tokens.
- **A sliding context window** so history can't grow unbounded within a run.
- **A durable "already-processed" set** plus a Redis **poll lock** so the same email is never handed to the model twice, and overlapping polls can't double-trigger.

The lesson: with autonomous agents, cost safety belongs in the architecture, not in a prompt.

## Voice-first control with Amazon Nova Sonic

Accessibility was a first-class requirement, and it produced the most interesting engineering. Using **Amazon Nova Sonic** — a bidirectional speech-to-speech model on Bedrock — via the Strands `BidiAgent`, a user can navigate the app, hear their unread emails and pending approvals read aloud, edit a reply's wording, and drive every action button (approve & send, save to draft, schedule with a spoken date/time, reject) **entirely by voice**.

The frontend streams 16 kHz mono PCM audio over a WebSocket and plays back the model's audio. But the key lesson was this:

> **Don't make a probabilistic model responsible for deterministic outcomes.**

Early on, the assistant would *try* to perform an action, its tool-use would be flaky over the streaming session, and it would narrate a false "I'm sorry, there was an issue" over an action the app had *already completed*. Speech models are wonderful at understanding intent and speaking naturally — and unreliable at being the thing that guarantees a click happens.

So we split responsibilities:

- **A deterministic server-side Intent Router** classifies each user utterance and performs navigation and high-impact actions directly, server-side. These tools are *withheld from the model* so it can't race the router.
- **The model** handles natural conversation and reads content aloud, and receives short **grounding notes** after the router completes an action ("the app already did this; acknowledge briefly, don't apologize"), which eliminated the false-failure loops.

Real speech is also messy and multi-turn. Users say *"change the text hi there"* … then *"to hello,"* or *"approve and schedule"* … *"September fifteenth"* … *"seven pm."* We built per-session buffers that accumulate a find-and-replace or a date-plus-time across turns, with spoken-datetime and month-name parsing, so the reply body and the calendar get filled correctly. For precise, programmatic text editing we render reply bodies in a headless TipTap (ProseMirror) editor so voice commands can insert, replace, and set text exactly.

## Meeting people where they are: SMS with Amazon SNS

A blind user — or anyone away from the dashboard — shouldn't have to watch a queue. So when a reply is waiting for approval, Atomic AI texts the reviewer using **Amazon SNS**, reusing the same AWS credentials as Bedrock (no new secret to manage).

Two production details mattered:

- **Cost tracking without AWS billing access.** We compute the billed SMS segment count (GSM-7 vs. UCS-2 encoding rules) and estimate spend per destination country from an in-app pricing table, recording each send. That gives users a live "SMS count and estimated spend by country" view, plus a per-user monthly spend cap to guard against runaway cost.
- **Best-effort by design.** SNS sending runs off the event loop, never blocks approval creation, and records failures instead of raising. That decoupling saved us during rollout: SMS "silently failed" at first — not a code bug, but the IAM user lacked `sns:Publish` and the account was still in the SNS SMS sandbox. Because sends were best-effort with recorded failures, the rest of the platform kept working while we fixed permissions and verified a destination number.

The takeaway: **cloud integrations fail at the edges** — IAM permissions, service sandboxes, per-country SMS rules — so design for graceful, observable failure from day one.

## Multi-tenancy and security, briefly

Because this is a team platform, tenancy and access control run through everything:

- **Workspaces with RBAC** (Owner / Admin / Member / Viewer) gate who can connect integrations, edit shared rules, and resolve approvals.
- **Server-derived tenancy** on every request — the workspace and user come from the authenticated context, never from client or model input.
- **Encrypted credentials at rest** in a shared "integration vault," with connections markable personal or workspace-shared.
- **HttpOnly session cookies, scrubbed audit logs, and no secrets in logs** throughout.

## What we learned

- **Trust is the product.** For autonomous agents, the approval hub, the audit trail, and the hard cost caps mattered as much as the agent's capabilities.
- **Determinism where it counts.** Navigation and high-impact actions belong in code; the model is best at understanding intent and speaking.
- **Design for messy, multi-turn humans** — fragments, corrections, and "change it to…" follow-ups are what make voice feel natural instead of brittle.
- **Build for accessibility first** and the product gets better for everyone.

## What's next

We're broadening the connector catalog across all 12 app categories via MCP, moving toward multi-agent (A2A) swarms under one approval umbrella, taking SNS SMS to production (plus opt-in WhatsApp/Telegram and quiet-hours digests), and adding per-workspace cost dashboards and budgets.

Agents are getting more capable every month. The opportunity isn't to replace the human in the loop — it's to build the loop well. That's what *Agents for Humans* means to us.

---

*Atomic AI is built with FastAPI (Python 3.14), Next.js 16, the Strands Agents SDK, Amazon Bedrock, Amazon Nova Sonic, and Amazon SNS, running on PostgreSQL and Redis via Docker Compose.*
