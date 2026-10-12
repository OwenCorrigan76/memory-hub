"""Compile a standing profile from current dreaming facts.

Dreaming stays the write path. This module reads those facts, asks the
extraction model for a short profile, and stores one synopsis node per user.
The session hook loads that node before search.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memoryhub_core.config import AppSettings
from memoryhub_core.models.memory import MemoryNode, MemoryRelationship
from memoryhub_core.models.schemas import (
    ContentType,
    MemoryNodeCreate,
    MemoryNodeUpdate,
    MemoryScope,
    RelationshipCreate,
)
from memoryhub_core.services.embeddings import EmbeddingService
from memoryhub_core.services.graph import create_relationship, invalidate_relationship
from memoryhub_core.services.memory import create_memory, update_memory

logger = logging.getLogger(__name__)

_FACT_LIMIT = 50
_FACT_CHARS = 1000
_COMPILER_ACTOR = "persona-compiler"

# (system_prompt, user_prompt) -> profile markdown
SynopsisComplete = Callable[[str, str], Awaitable[str]]

_prompt_cache: str | None = None


def load_synopsis_prompt() -> str:
    """Return the compiler system prompt. Cached after the first read."""
    global _prompt_cache
    if _prompt_cache is not None:
        return _prompt_cache

    prompt_path = Path(__file__).resolve().parents[3] / "prompts" / "persona_synopsis.yaml"
    data = yaml.safe_load(prompt_path.read_text())
    _prompt_cache = data["system_prompt"]
    return _prompt_cache


def _parse_profile(raw: str) -> str:
    """Pull the profile string out of a JSON object, ignoring code fences."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        fence = cleaned.rfind("```")
        if fence != -1:
            cleaned = cleaned[:fence]
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Synopsis model did not return a JSON object")
    profile = parsed.get("profile") or ""
    if not isinstance(profile, str):
        raise ValueError("Synopsis profile must be a string")
    return profile.strip()


async def _call_synopsis_llm(
    system_prompt: str,
    user_prompt: str,
    *,
    client: httpx.AsyncClient,
    model: str,
    url: str,
) -> str:
    """Call the same chat-completions endpoint dreaming uses."""
    response = await client.post(
        f"{url.rstrip('/')}/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
        },
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"]
    return _parse_profile(raw)


def _format_facts(facts: list[MemoryNode]) -> str:
    lines = [
        "Facts, newest first. When two facts disagree, the newer updated_at wins.",
        "",
    ]
    for fact in facts:
        stamp = fact.updated_at.astimezone(UTC).isoformat() if fact.updated_at else "unknown"
        text = (fact.content or "").replace("\n", " ").strip()
        if len(text) > _FACT_CHARS:
            text = text[:_FACT_CHARS].rstrip() + "..."
        lines.append(f"- [{stamp}] {text}")
    return "\n".join(lines)


def _user_prompt(facts: list[MemoryNode], pinned: str | None) -> str:
    body = _format_facts(facts)
    if pinned:
        body += (
            "\n\nUSER DECLARED (keep this text verbatim; do not contradict or delete it):\n"
            f"{pinned.strip()}\n"
        )
    return body


def _ensure_pinned(profile: str, pinned: str | None) -> str:
    """Keep user-written text even when the model drops it."""
    if not pinned or not pinned.strip():
        return profile
    if pinned.strip() in profile:
        return profile
    return profile.rstrip() + "\n\n## User declared\n\n" + pinned.strip() + "\n"


async def _load_facts(
    session: AsyncSession,
    *,
    tenant_id: str,
    owner_id: str,
) -> list[MemoryNode]:
    stmt = (
        select(MemoryNode)
        .where(
            MemoryNode.tenant_id == tenant_id,
            MemoryNode.owner_id == owner_id,
            MemoryNode.scope == "user",
            MemoryNode.source == "dreaming",
            MemoryNode.is_current.is_(True),
            MemoryNode.deleted_at.is_(None),
            MemoryNode.status == "active",
        )
        .order_by(MemoryNode.updated_at.desc())
        .limit(_FACT_LIMIT)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _load_synopsis(
    session: AsyncSession,
    *,
    tenant_id: str,
    owner_id: str,
) -> MemoryNode | None:
    stmt = (
        select(MemoryNode)
        .where(
            MemoryNode.tenant_id == tenant_id,
            MemoryNode.owner_id == owner_id,
            MemoryNode.scope == "user",
            MemoryNode.content_type == ContentType.SYNOPSIS.value,
            MemoryNode.is_current.is_(True),
            MemoryNode.deleted_at.is_(None),
            MemoryNode.status == "active",
        )
        .order_by(MemoryNode.updated_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _sync_provenance(
    session: AsyncSession,
    *,
    synopsis_id: uuid.UUID,
    fact_ids: list[uuid.UUID],
) -> None:
    """Point derived_from edges at the facts this compile used.

    Edges that already point at a desired fact stay. Edges for facts that
    dropped out are invalidated. New facts get a new edge. Rebuilding every
    edge would collide with the unique active-edge index on SQLite.
    """
    stmt = select(MemoryRelationship).where(
        MemoryRelationship.source_id == synopsis_id,
        MemoryRelationship.relationship_type == "derived_from",
        MemoryRelationship.valid_until.is_(None),
    )
    existing = list((await session.execute(stmt)).scalars().all())
    desired = set(fact_ids)
    existing_targets = {rel.target_id for rel in existing}

    invalidated = False
    for rel in existing:
        if rel.target_id not in desired:
            await invalidate_relationship(rel.id, session)
            invalidated = True

    created = False
    for fact_id in fact_ids:
        if fact_id in existing_targets:
            continue
        await create_relationship(
            RelationshipCreate(
                source_id=synopsis_id,
                target_id=fact_id,
                relationship_type="derived_from",
                created_by=_COMPILER_ACTOR,
            ),
            session,
        )
        created = True

    if invalidated and not created:
        await session.commit()


def _result(
    *,
    compiled: bool,
    reason: str,
    memory_id: uuid.UUID | None = None,
    fact_ids: list[uuid.UUID] | None = None,
) -> dict:
    return {
        "compiled": compiled,
        "reason": reason,
        "memory_id": str(memory_id) if memory_id else None,
        "fact_ids": [str(fact_id) for fact_id in (fact_ids or [])],
    }


async def compile_persona(
    session: AsyncSession,
    *,
    tenant_id: str,
    owner_id: str,
    embedding_service: EmbeddingService,
    model: str | None = None,
    url: str | None = None,
    complete: SynopsisComplete | None = None,
) -> dict:
    """Rebuild the user's standing profile from current dreaming facts.

    ``complete`` injects the model call in tests. Production uses the
    dreaming extraction model settings.

    No facts, or an empty model reply, leaves any existing card unchanged.
    """
    facts = await _load_facts(session, tenant_id=tenant_id, owner_id=owner_id)
    existing = await _load_synopsis(session, tenant_id=tenant_id, owner_id=owner_id)
    if not facts:
        return _result(
            compiled=False,
            reason="no_facts",
            memory_id=existing.id if existing else None,
        )

    pinned = None
    user_edited = False
    if existing is not None:
        meta = existing.metadata_ or {}
        user_edited = bool(meta.get("user_edited"))
        if user_edited:
            pinned = existing.content

    system_prompt = load_synopsis_prompt()
    user_prompt = _user_prompt(facts, pinned)

    if complete is not None:
        profile = (await complete(system_prompt, user_prompt)).strip()
    else:
        settings = AppSettings()
        model_name = model or settings.conv_extraction_model
        model_url = url or settings.conv_extraction_model_url
        if not model_name or not model_url:
            raise ValueError(
                "Extraction model and URL must be configured. "
                "Set MEMORYHUB_CONV_EXTRACTION_MODEL and "
                "MEMORYHUB_CONV_EXTRACTION_MODEL_URL."
            )
        headers = {}
        if settings.conv_extraction_api_key:
            headers["Authorization"] = f"Bearer {settings.conv_extraction_api_key}"
        async with httpx.AsyncClient(
            timeout=settings.conv_extraction_timeout,
            verify=False,
            headers=headers,
        ) as client:
            profile = await _call_synopsis_llm(
                system_prompt,
                user_prompt,
                client=client,
                model=model_name,
                url=model_url,
            )

    if not profile:
        return _result(
            compiled=False,
            reason="empty_profile",
            memory_id=existing.id if existing else None,
            fact_ids=[fact.id for fact in facts],
        )

    profile = _ensure_pinned(profile, pinned)
    source = "user-declared" if user_edited else "compiler"
    metadata = {
        "user_edited": user_edited,
        "compiled_from": [str(fact.id) for fact in facts],
        "compiled_at": datetime.now(UTC).isoformat(),
    }
    fact_ids = [fact.id for fact in facts]

    if existing is None:
        created, _curation = await create_memory(
            MemoryNodeCreate(
                content=profile,
                scope=MemoryScope.USER,
                weight=1.0,
                owner_id=owner_id,
                content_type=ContentType.SYNOPSIS,
                source=source,
                metadata=metadata,
            ),
            session,
            embedding_service,
            tenant_id=tenant_id,
            skip_curation=True,
        )
        if created is None:
            return _result(compiled=False, reason="create_blocked", fact_ids=fact_ids)
        synopsis_id = created.id
    else:
        updated = await update_memory(
            existing.id,
            MemoryNodeUpdate(
                content=profile,
                weight=1.0,
                metadata=metadata,
                content_type=ContentType.SYNOPSIS,
                source=source,
            ),
            session,
            embedding_service,
            actor_id=_COMPILER_ACTOR,
        )
        synopsis_id = updated.id

    await _sync_provenance(session, synopsis_id=synopsis_id, fact_ids=fact_ids)
    return _result(
        compiled=True,
        reason="compiled",
        memory_id=synopsis_id,
        fact_ids=fact_ids,
    )
