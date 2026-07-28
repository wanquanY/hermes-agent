"""Version-anchored LSP diagnostic freshness regressions."""

import os
import sys
from pathlib import Path

import pytest

from agent.lsp.client import LSPClient

MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")


def _client(workspace: Path, script: str, **env_extra: str) -> LSPClient:
    return LSPClient(
        server_id=f"mock-{script}",
        workspace_root=str(workspace),
        command=[sys.executable, MOCK_SERVER],
        env={"MOCK_LSP_SCRIPT": script, **env_extra},
        cwd=str(workspace),
    )


@pytest.mark.asyncio
async def test_stale_push_does_not_satisfy_wait(tmp_path: Path):
    file_path = tmp_path / "x.py"
    file_path.write_text("bad code\n")
    client = _client(tmp_path, "stale")
    await client.start()
    try:
        version = await client.open_file(str(file_path), language_id="python")
        assert await client.wait_for_diagnostics(
            str(file_path), version, mode="document", timeout=2.0
        )
        assert len(client.diagnostics_for(str(file_path))) == 1

        file_path.write_text("good code\n")
        version = await client.open_file(str(file_path), language_id="python")
        assert not await client.wait_for_diagnostics(
            str(file_path), version, mode="document", timeout=0.5
        )
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_fresh_only_excludes_stale_push(tmp_path: Path):
    file_path = tmp_path / "x.py"
    file_path.write_text("bad code\n")
    client = _client(tmp_path, "stale")
    await client.start()
    try:
        version = await client.open_file(str(file_path), language_id="python")
        await client.wait_for_diagnostics(
            str(file_path), version, mode="document", timeout=2.0
        )
        file_path.write_text("good code\n")
        await client.open_file(str(file_path), language_id="python")
        assert len(client.diagnostics_for(str(file_path))) == 1
        assert client.diagnostics_for(str(file_path), fresh_only=True) == []
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_slow_fresh_push_is_waited_for(tmp_path: Path):
    file_path = tmp_path / "x.py"
    file_path.write_text("bad code\n")
    client = _client(tmp_path, "slow_push", MOCK_LSP_PUSH_DELAY="0.4")
    await client.start()
    try:
        version = await client.open_file(str(file_path), language_id="python")
        assert await client.wait_for_diagnostics(
            str(file_path), version, mode="document", timeout=2.0
        )
        file_path.write_text("good code\n")
        version = await client.open_file(str(file_path), language_id="python")
        assert await client.wait_for_diagnostics(
            str(file_path), version, mode="document", timeout=3.0
        )
        assert client.diagnostics_for(str(file_path), fresh_only=True) == []
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_version_bump_invalidates_pull_store(tmp_path: Path):
    file_path = tmp_path / "x.py"
    file_path.write_text("bad code\n")
    client = _client(tmp_path, "clean")
    await client.start()
    try:
        version = await client.open_file(str(file_path), language_id="python")
        await client.wait_for_diagnostics(
            str(file_path), version, mode="document", timeout=2.0
        )
        document = client._docs[os.path.abspath(str(file_path))]
        assert document.fresh_pull()
        file_path.write_text("good code\n")
        await client.open_file(str(file_path), language_id="python")
        assert not document.fresh_pull()
        assert client.diagnostics_for(str(file_path), fresh_only=True) == []
    finally:
        await client.shutdown()
