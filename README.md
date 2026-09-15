# Atomic AI

**Agents for Humans** — a multi-tenant AI automation platform where autonomous agents do the tedious cross-platform work, but humans stay in control. Agents *propose* high-impact actions; a person approves them. Reviewers can stay in the loop by dashboard, by **voice**, or by **SMS**.

Built with FastAPI (Python 3.14), Next.js 16, the [Strands Agents SDK](https://strandsagents.com/) on [Amazon Bedrock](https://aws.amazon.com/bedrock/), [Amazon Nova Sonic](https://aws.amazon.com/ai/generative-ai/nova/) for voice, and [Amazon SNS](https://aws.amazon.com/sns/) for SMS — running end-to-end with a single `docker compose up`.

---

## Highlights

- **Team workspaces with RBAC** — Google/GitHub login, workspaces, and Owner / Admin / Member / Viewer roles that gate integrations, rules, and approvals.
- **Shared integration vault** — connect app accounts via OAuth/MCP, mark them personal or workspace-shared; every credential is encrypted at rest.
- **Plain-language automation rules** — e.g. *"all customer emails must be saved as drafts unless an admin approves."*
- **Collaborative Approval Hub** — a `before_tool_call` interception pauses any high-impact agent action and posts it to a real-time (WebSocket) approval queue; Owners/Admins approve, edit, regenerate, schedule, or reject.
- **Voice control (accessibility-first)** — an Amazon Nova Sonic speech-to-speech assistant navigates the app, reads emails/approvals aloud, edits reply text, and drives every action button by voice.
- **SMS reply-approval alerts** — Amazon SNS texts reviewers when a reply awaits approval, with per-country SMS count/spend tracking and a per-user spend cap.
- **Super Admin control plane** — system-wide user/workspace management, agent session inspection, token/cost analytics, and an emergency kill-switch.
- **Cost safety by construction** — hard per-run token/turn caps, a sliding context window, message de-duplication, and Redis poll locks.

## Architecture

```
Clients (browser · voice · phone/SMS · provider webhooks)
        │  HTTPS / WSS
Frontend — Next.js 16 (workspace portal · approvals hub · account/SMS · voice widget · admin)
        │  authenticated API + WebSocket
Backend  — FastAPI (Python 3.14): auth/RBAC · Voice Gateway · REST/WS API · webhooks
           services: Approval · Agent Engine · Integration Vault · Rules · SNS SMS · Audit
        │  run / notify
Agent Runtime & AWS — ARQ worker · Amazon Bedrock · Amazon Nova Sonic · Amazon SNS
        │  read / write
Data — PostgreSQL 18 · Redis 8 · external app providers (OAuth / MCP)
```

A rendered diagram is in [`docs/atomic-ai-architecture.pdf`](docs/atomic-ai-architecture.pdf).

## Tech stack

| Layer | Technology |
| --- | --- |
| Frontend | Next.js 16 (App Router, TypeScript, Tailwind, shadcn-style UI), TipTap editor |
| Backend | FastAPI + Uvicorn, SQLAlchemy 2.0 async ORM, Alembic |
| Agents | Strands Agents SDK + Model Context Protocol (MCP), Amazon Bedrock |
| Voice | Amazon Nova Sonic (bidirectional speech-to-speech) via Strands BidiAgent |
| Notifications | Amazon SNS (SMS) |
| Data / state | PostgreSQL 18, Redis 8 (cache, ARQ job queue, poll locks) |
| Runtime | Docker & Docker Compose |

## Quick start

**Prerequisites:** Docker + Docker Compose, and (optionally) AWS credentials with Amazon Bedrock, Nova Sonic, and SNS access for the agent, voice, and SMS features.

```bash
# 1. Clone
git clone https://github.com/atomicai-universe/atomic-ai.git
cd atomic-ai

# 2. Configure environment
cp .env.example .env
#   Fill in: ENCRYPTION_KEY, Google/GitHub OAuth client id+secret,
#   and (for agents/voice/SMS) AWS_REGION + AWS credentials.

# 3. Launch everything (Postgres, Redis, backend, worker, frontend)
docker compose up --build
```

- Frontend: <http://localhost:3000>
- Backend API + health: <http://localhost:8000/health>

Database migrations run automatically on backend startup (`alembic upgrade head`).

### Key environment variables

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL`, `REDIS_URL` | Postgres DSN and Redis URL |
| `ENCRYPTION_KEY` | Symmetric key for the credential vault (never stored in the DB) |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Google login |
| `GITHUB_OAUTH_CLIENT_ID` / `_SECRET` | GitHub login |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Shared credentials for Bedrock, Nova Sonic, and SNS |
| `BEDROCK_MODEL_ID` | Bedrock model for the agent loop |
| `VOICE_ENABLED`, `NOVA_SONIC_MODEL_ID` | Voice accessibility feature |
| `SNS_ENABLED`, `SNS_SMS_TYPE`, `SNS_MONTHLY_USER_SPEND_CAP_USD` | SMS notifications |
| `AGENT_MAX_TURNS`, `AGENT_MAX_TOTAL_TOKENS`, `AGENT_CONVERSATION_WINDOW` | Per-run agent cost caps |

See [`.env.example`](.env.example) for the complete, commented list. **Never commit `.env` or OAuth client-secret files** — they are git-ignored by default.

## Project layout

```
backend/            FastAPI app (app/api, app/services, app/agents, app/db), Alembic migrations, tests
frontend/           Next.js 16 app (src/app dashboard + admin, components, lib)
docs/               Architecture diagram + write-ups
docker-compose.yml  Postgres, Redis, backend, ARQ worker, frontend
Dockerfile.backend  Dockerfile.frontend
```

## Testing

```bash
# Backend (in the running container)
docker compose exec fastapi_backend /opt/venv/bin/python -m pytest -q

# Frontend
cd frontend && npm test
```

## Security notes

- Server-derived tenancy on every request (workspace/user come from the authenticated context, never from client or model input).
- HttpOnly session cookies; encrypted integration credentials at rest; scrubbed, append-only audit logs; no secrets in logs.
- High-impact agent actions execute **only** after human approval via the Approval Hub.

> ⚠️ Amazon SNS SMS requires `sns:Publish` on your IAM principal and, for new accounts, moving out of the SNS SMS sandbox (or verifying destination numbers). Bedrock/Nova Sonic require the relevant models enabled in your region.

## License

Released under the [MIT License](LICENSE).
