"""Idempotent database seed process (Req 19.4).

Provisions a default :class:`~app.db.models.User` Super_Admin account and a
sample :class:`~app.db.models.Workspace` (with the Super_Admin as sole Owner)
so a freshly-initialized deployment is immediately usable.

Design constraints:

- **Runs after migrations.** The container entrypoint invokes ``python -m
  app.db.seed`` only after ``alembic upgrade head`` (see ``entrypoint.sh``), so
  the schema is guaranteed present.
- **Idempotent.** The seed is safe to run on every container start. Each entity
  is created only when an existence check confirms it is absent, so restarts
  never duplicate rows. The Super_Admin is looked up by email; the Workspace by
  its deterministic unique slug; the owner membership by ``(workspace, user)``.
- **Transactional.** All work runs inside a single :func:`session_scope`
  transaction that commits on success and rolls back on any error.

Seed values are read from :class:`app.config.Settings` (``SEED_*``), all of
which have sensible defaults so the seed works out of the box while remaining
configurable per-deployment.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    AuthProvider,
    MemberRole,
    User,
    Workspace,
    WorkspaceMember,
)
from app.db.session import session_scope

logger = logging.getLogger("app.db.seed")

# Deterministic slug for the sample workspace. Using a fixed, unique slug makes
# the existence check stable across runs so re-seeding never creates a second
# sample workspace (the ``slug`` column is UNIQUE).
SAMPLE_WORKSPACE_SLUG = "sample-workspace"


def _resolve_auth_provider(raw: str) -> AuthProvider:
    """Coerce the configured provider string to a valid :class:`AuthProvider`.

    ``auth_provider`` must be one of ``google``/``github``; an unrecognized
    configuration value falls back to ``google`` (the default) rather than
    persisting an invalid enum label.
    """
    try:
        return AuthProvider(raw.strip().lower())
    except ValueError:
        logger.warning(
            "Unknown SEED_SUPERADMIN_AUTH_PROVIDER %r; defaulting to 'google'", raw
        )
        return AuthProvider.GOOGLE


async def _get_or_create_superadmin(session: AsyncSession) -> User:
    """Return the existing Super_Admin (looked up by email) or create it."""
    settings = get_settings()
    email = settings.SEED_SUPERADMIN_EMAIL

    existing = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("Seed: Super_Admin %s already present; skipping.", email)
        return existing

    user = User(
        email=email,
        name=settings.SEED_SUPERADMIN_NAME,
        auth_provider=_resolve_auth_provider(settings.SEED_SUPERADMIN_AUTH_PROVIDER),
        is_superadmin=True,
    )
    session.add(user)
    # Flush so the client-side UUID default is materialized and available for
    # the workspace's ``created_by_user_id`` / membership below.
    await session.flush()
    logger.info("Seed: created Super_Admin %s.", email)
    return user


async def _get_or_create_sample_workspace(
    session: AsyncSession, owner: User
) -> Workspace:
    """Return the existing sample Workspace (by slug) or create it."""
    settings = get_settings()

    existing = (
        await session.execute(
            select(Workspace).where(Workspace.slug == SAMPLE_WORKSPACE_SLUG)
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "Seed: sample Workspace %r already present; skipping.",
            SAMPLE_WORKSPACE_SLUG,
        )
        return existing

    workspace = Workspace(
        name=settings.SEED_WORKSPACE_NAME,
        slug=SAMPLE_WORKSPACE_SLUG,
        created_by_user_id=owner.id,
    )
    session.add(workspace)
    await session.flush()
    logger.info("Seed: created sample Workspace %r.", SAMPLE_WORKSPACE_SLUG)
    return workspace


async def _ensure_owner_membership(
    session: AsyncSession, workspace: Workspace, owner: User
) -> WorkspaceMember:
    """Ensure the Super_Admin holds the Owner membership on the workspace."""
    existing = (
        await session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == owner.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "Seed: owner membership already present for workspace %r; skipping.",
            SAMPLE_WORKSPACE_SLUG,
        )
        return existing

    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=owner.id,
        role=MemberRole.OWNER,
    )
    session.add(member)
    await session.flush()
    logger.info("Seed: created Owner membership for %s.", owner.email)
    return member


async def seed() -> None:
    """Provision the default Super_Admin and sample Workspace (idempotent).

    Wraps all existence checks and inserts in a single transaction so a partial
    run cannot leave the database half-seeded.
    """
    async with session_scope() as session:
        superadmin = await _get_or_create_superadmin(session)
        workspace = await _get_or_create_sample_workspace(session, superadmin)
        await _ensure_owner_membership(session, workspace, superadmin)
    logger.info("Seed: complete.")


def main() -> None:
    """Console entrypoint for ``python -m app.db.seed``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(seed())


if __name__ == "__main__":
    main()
