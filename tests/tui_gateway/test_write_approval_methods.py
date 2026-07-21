import json
import os

import pytest


@pytest.fixture
def approval_home(tmp_path, monkeypatch):
    home = tmp_path / "profile-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _call(method: str, **params):
    from tui_gateway import server

    return server._methods[method]("rid-write-approval", params)


def test_gateway_lists_details_and_rejects_memory_write(approval_home):
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "private fact"},
        summary="add to memory: private fact",
        origin="background_review",
    )

    listed = _call("write_approval.list", subsystem="memory")["result"]
    assert listed["pendingCount"] == 1
    assert listed["pending"] == [
        {
            "id": record["id"],
            "subsystem": "memory",
            "action": "add",
            "summary": "add to memory: private fact",
            "origin": "background_review",
            "createdAt": record["created_at"],
        }
    ]
    assert "payload" not in listed["pending"][0]

    detail = _call(
        "write_approval.detail",
        subsystem="memory",
        id=record["id"],
    )["result"]
    assert detail["payload"]["content"] == "private fact"

    rejected = _call(
        "write_approval.reject",
        subsystem="memory",
        id=record["id"],
    )["result"]
    assert rejected["rejected"] == 1
    assert wa.pending_count(wa.MEMORY) == 0


def test_gateway_approves_memory_against_profile_home(approval_home):
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.MEMORY,
        {"action": "add", "target": "memory", "content": "approved fact"},
        summary="approved fact",
        origin="foreground",
    )
    approved = _call(
        "write_approval.approve",
        subsystem="memory",
        id=record["id"],
    )["result"]

    assert approved == {"applied": 1, "failed": [], "not_found": False, "message": ""}
    assert "approved fact" in (approval_home / "memories" / "MEMORY.md").read_text()
    assert wa.pending_count(wa.MEMORY) == 0


def test_gateway_configures_boolean_gate(approval_home):
    from tools import write_approval as wa

    response = _call(
        "write_approval.configure",
        subsystem="skills",
        enabled=True,
    )["result"]
    assert response == {"subsystem": "skills", "enabled": True, "pendingCount": 0}
    assert wa.write_approval_enabled(wa.SKILLS) is True

    invalid = _call(
        "write_approval.configure",
        subsystem="skills",
        enabled="true",
    )
    assert invalid["error"]["code"] == 4002


def test_pending_ids_cannot_escape_profile_store(approval_home):
    from tools import write_approval as wa

    assert wa.get_pending(wa.MEMORY, "../../config") is None
    assert wa.discard_pending(wa.MEMORY, "../../config") is False


def test_pending_store_is_owner_only(approval_home):
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.SKILLS,
        {"action": "create", "name": "private-skill", "content": "secret"},
        summary="private skill",
        origin="foreground",
    )
    directory = approval_home / "pending" / "skills"
    path = directory / f"{record['id']}.json"
    assert os.stat(directory).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
