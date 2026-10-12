"""Tests for the persona profile CLI commands."""

import json
from unittest.mock import AsyncMock, patch

from memoryhub.models import Memory
from typer.testing import CliRunner

from memoryhub_cli.main import app

runner = CliRunner()


def _client(**methods):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    for name, method in methods.items():
        setattr(client, name, method)
    return client


def test_persona_compile_json():
    result_data = {
        "compiled": True,
        "reason": "compiled",
        "memory_id": "persona-1",
        "fact_ids": [],
    }
    client = _client(persona_compile=AsyncMock(return_value=result_data))

    with patch("memoryhub_cli.main._get_client", return_value=client):
        result = runner.invoke(app, ["persona", "compile", "--output", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == result_data


def test_persona_show_compact():
    client = _client(
        list=AsyncMock(
            return_value={
                "results": [{"id": "persona-1", "content": "## Preferences\n- Use Docker."}],
            }
        )
    )

    with patch("memoryhub_cli.main._get_client", return_value=client):
        result = runner.invoke(app, ["persona", "show", "--output", "compact"])

    assert result.exit_code == 0, result.output
    assert result.output.index("<memoryhub-persona>") < result.output.index("Use Docker")
    assert "</memoryhub-persona>" in result.output
    client.list.assert_awaited_once_with(
        scope="user",
        content_type="synopsis",
        max_results=1,
    )


def test_persona_edit_marks_profile_user_declared():
    client = _client(
        list=AsyncMock(
            return_value={
                "results": [
                    {
                        "id": "persona-1",
                        "content": "old",
                    }
                ]
            }
        ),
        read=AsyncMock(
            return_value=Memory(
                id="persona-1",
                content="old",
                scope="user",
                owner_id="person-1",
                metadata={"compiled_from": ["fact-1"]},
                content_type="synopsis",
            )
        ),
        update=AsyncMock(
            return_value=Memory(
                id="persona-2",
                content="my wording",
                scope="user",
                owner_id="person-1",
                weight=1.0,
                version=2,
                content_type="synopsis",
                source="user-declared",
            )
        ),
    )

    with patch("memoryhub_cli.main._get_client", return_value=client):
        result = runner.invoke(app, ["persona", "edit", "my wording"])

    assert result.exit_code == 0, result.output
    client.update.assert_awaited_once_with(
        "persona-1",
        content="my wording",
        metadata={"compiled_from": ["fact-1"], "user_edited": True},
        source="user-declared",
        content_type="synopsis",
    )
