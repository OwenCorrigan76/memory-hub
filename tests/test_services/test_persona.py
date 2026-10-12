"""Tests for compiling a user's standing profile from dreaming memories."""

import uuid

from sqlalchemy import select

from memoryhub_core.models.memory import MemoryNode, MemoryRelationship
from memoryhub_core.models.schemas import ContentType, MemoryNodeCreate, MemoryNodeUpdate, MemoryScope
from memoryhub_core.services.memory import (
    create_memory,
    list_memories,
    search_memories,
    update_memory,
)
from memoryhub_core.services.persona import compile_persona


async def _write_fact(
    session,
    embedding_service,
    *,
    content,
    owner="person-1",
    tenant="tenant-1",
    scope=MemoryScope.USER,
    source="dreaming",
):
    return await create_memory(
        MemoryNodeCreate(
            content=content,
            scope=scope,
            owner_id=owner,
            weight=0.7,
            source=source,
        ),
        session,
        embedding_service,
        tenant_id=tenant,
        skip_curation=True,
    )


async def test_compile_with_no_facts_is_a_noop(async_session, embedding_service):
    result = await compile_persona(
        async_session,
        tenant_id="tenant-1",
        owner_id="person-1",
        embedding_service=embedding_service,
    )

    assert result == {
        "compiled": False,
        "reason": "no_facts",
        "memory_id": None,
        "fact_ids": [],
    }


async def test_compile_creates_synopsis_and_provenance_edges(
    async_session,
    embedding_service,
):
    fact, _ = await _write_fact(
        async_session,
        embedding_service,
        content="Prefers Docker over Podman for container work.",
    )

    async def complete(system_prompt, user_prompt):
        assert "## Preferences" in system_prompt
        assert "Docker over Podman" in user_prompt
        return "## Preferences\n- Use Docker for container work."

    result = await compile_persona(
        async_session,
        tenant_id="tenant-1",
        owner_id="person-1",
        embedding_service=embedding_service,
        complete=complete,
    )

    assert result["compiled"] is True
    synopsis = await async_session.get(MemoryNode, uuid.UUID(result["memory_id"]))
    assert synopsis.content_type == ContentType.SYNOPSIS.value
    assert synopsis.scope == MemoryScope.USER.value
    assert synopsis.weight == 1.0
    assert synopsis.source == "compiler"
    assert synopsis.metadata_["compiled_from"] == [str(fact.id)]

    edges = list(
        (
            await async_session.execute(
                select(MemoryRelationship).where(
                    MemoryRelationship.source_id == synopsis.id,
                    MemoryRelationship.relationship_type == "derived_from",
                    MemoryRelationship.valid_until.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert [edge.target_id for edge in edges] == [fact.id]


async def test_compile_uses_only_current_user_dreaming_facts(
    async_session,
    embedding_service,
):
    allowed, _ = await _write_fact(async_session, embedding_service, content="The user likes concise answers.")
    await _write_fact(
        async_session,
        embedding_service,
        content="Project memory must not enter a personal profile.",
        scope=MemoryScope.PROJECT,
    )
    await _write_fact(
        async_session,
        embedding_service,
        content="Agent-authored memory is not a dreaming fact.",
        source="agent",
    )
    await _write_fact(
        async_session,
        embedding_service,
        content="Another user's facts are private.",
        owner="person-2",
    )

    captured = {}

    async def complete(_system_prompt, user_prompt):
        captured["prompt"] = user_prompt
        return "## Communication\n- Keep answers concise."

    result = await compile_persona(
        async_session,
        tenant_id="tenant-1",
        owner_id="person-1",
        embedding_service=embedding_service,
        complete=complete,
    )

    assert result["fact_ids"] == [str(allowed.id)]
    assert "likes concise answers" in captured["prompt"]
    assert "Project memory" not in captured["prompt"]
    assert "Agent-authored" not in captured["prompt"]
    assert "Another user's" not in captured["prompt"]


async def test_user_edited_profile_is_preserved_on_recompile(
    async_session,
    embedding_service,
):
    await _write_fact(async_session, embedding_service, content="Prefers direct communication.")

    async def complete(_system_prompt, _user_prompt):
        return "## Communication\n- Speak directly."

    first = await compile_persona(
        async_session,
        tenant_id="tenant-1",
        owner_id="person-1",
        embedding_service=embedding_service,
        complete=complete,
    )
    edited = await update_memory(
        uuid.UUID(first["memory_id"]),
        MemoryNodeUpdate(
            content="## Preferences\n- Always use my exact wording.",
            metadata={"user_edited": True},
            source="user-declared",
            content_type=ContentType.SYNOPSIS,
        ),
        async_session,
        embedding_service,
    )

    async def compile_again(_system_prompt, user_prompt):
        assert "Always use my exact wording." in user_prompt
        return "## Communication\n- Speak directly."

    second = await compile_persona(
        async_session,
        tenant_id="tenant-1",
        owner_id="person-1",
        embedding_service=embedding_service,
        complete=compile_again,
    )

    current = await async_session.get(MemoryNode, uuid.UUID(second["memory_id"]))
    assert current.content_type == ContentType.SYNOPSIS.value
    assert current.source == "user-declared"
    assert "Always use my exact wording." in current.content
    assert edited.id == current.previous_version_id


async def test_default_memory_list_hides_synopsis_but_explicit_filter_shows_it(
    async_session,
    embedding_service,
):
    await _write_fact(async_session, embedding_service, content="Prefers concise answers.")

    async def complete(_system_prompt, _user_prompt):
        return "## Communication\n- Be concise."

    await compile_persona(
        async_session,
        tenant_id="tenant-1",
        owner_id="person-1",
        embedding_service=embedding_service,
        complete=complete,
    )

    regular, _ = await list_memories(
        async_session,
        tenant_id="tenant-1",
        scope="user",
        owner_id="person-1",
    )
    explicit, _ = await list_memories(
        async_session,
        tenant_id="tenant-1",
        scope="user",
        owner_id="person-1",
        content_type=ContentType.SYNOPSIS.value,
    )
    searched = await search_memories(
        "profile preferences",
        async_session,
        embedding_service,
        tenant_id="tenant-1",
        scope="user",
        owner_id="person-1",
    )
    explicitly_searched = await search_memories(
        "profile preferences",
        async_session,
        embedding_service,
        tenant_id="tenant-1",
        scope="user",
        owner_id="person-1",
        content_type=ContentType.SYNOPSIS.value,
    )

    assert all(item.content_type != ContentType.SYNOPSIS for item in regular)
    assert len(explicit) == 1
    assert explicit[0].content_type == ContentType.SYNOPSIS
    assert all(item.content_type != ContentType.SYNOPSIS for item, _score in searched)
    assert len(explicitly_searched) == 1
    assert explicitly_searched[0][0].content_type == ContentType.SYNOPSIS
