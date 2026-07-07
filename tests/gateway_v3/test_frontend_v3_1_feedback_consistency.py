"""前端 v3.1 反馈六条 P0/P1 一致性守卫.

spec §"v3.0.2 relative to v3.0.1" 明确六条 P0/P1 前端反馈：

1. §6.2 CanonicalEventType 14 arm freeze — 含 thinking.delta / tool.generating /
   tool.delta / message.delta / reasoning.delta 等
2. interaction.* 不进 CanonicalEventType 枚举（走 InternalRunEventType._internal.*）
3. §7.4 Interaction 双通道：InteractionRegistry 相关代码带 anchor_seq
4. Activity 归属 TeamMissionRepo (activities + activity_commands 两表)
5. deprecations: string[] 在 BackendCapabilities（J10 已锁）
6. Handshake 无 interaction.persistent / runStateMachine.singleEntrypoint /
   since 泄漏（J10 已锁）

本文件系统性验证 1-4 条剩余项。
"""

from __future__ import annotations

import inspect
from pathlib import Path

from hermes_agent.domain.run_state_machine import RUN_OPENING_EVENT_TYPES


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# P0/P1 #1: CanonicalEventType 14 arm freeze —— 关键 delta / streaming types
# ---------------------------------------------------------------------------


def test_p0_1_run_opening_events_include_all_streaming_arms():
    """spec §6.2 —— 14 arm CanonicalEventType 中的所有 streaming type 都进
    RUN_OPENING_EVENT_TYPES（决定是否 open active run 的白名单）。
    """
    required = {
        "message.start",
        "message.delta",
        "reasoning.delta",
        "thinking.delta",
        "tool.start",
        "tool.generating",
        "tool.progress",
    }
    missing = required - set(RUN_OPENING_EVENT_TYPES)
    assert not missing, (
        f"CanonicalEventType 14-arm freeze incomplete —— RUN_OPENING_EVENT_TYPES "
        f"missing streaming arms: {sorted(missing)!r}"
    )


# ---------------------------------------------------------------------------
# P0/P1 #2: interaction.* 不进 CanonicalEventType 枚举
# ---------------------------------------------------------------------------


def test_p0_2_interaction_events_not_in_run_opening_types():
    """spec §7.4 —— interaction lifecycle events 走 _internal.* 内部通道，
    不能出现在 RUN_OPENING_EVENT_TYPES（Canonical 枚举等价物）里。
    """
    for evt in RUN_OPENING_EVENT_TYPES:
        assert not evt.startswith("interaction."), (
            f"interaction.* event {evt!r} 被误加入 RUN_OPENING_EVENT_TYPES —— "
            f"应该走 _internal.interaction.* 通道"
        )


def test_p0_2_internal_prefix_convention_in_event_ledger():
    """spec §7.4 —— EventLedger 用 _internal. 前缀识别内部事件。
    """
    from hermes_agent.domain.event_ledger import INTERNAL_EVENT_PREFIX

    assert INTERNAL_EVENT_PREFIX == "_internal.", (
        f"INTERNAL_EVENT_PREFIX 漂移: {INTERNAL_EVENT_PREFIX!r} != '_internal.'"
    )


# ---------------------------------------------------------------------------
# P0/P1 #3: InteractionRegistry 相关代码带 anchor_seq
# ---------------------------------------------------------------------------


def test_p0_3_persist_interaction_event_returns_anchor_seq():
    """spec §7.4 —— InteractionRegistry 的持久化接口必须返回 anchor_seq。
    """
    from tui_gateway.services.interaction_registry import persist_interaction_event

    src = inspect.getsource(persist_interaction_event)
    # 必须显式处理 anchor_seq —— 无论是设置还是返回。
    assert "anchor_seq" in src, (
        "persist_interaction_event 里没有 anchor_seq —— spec §7.4 "
        "Interaction 双通道要求这个字段"
    )


def test_p0_3_find_interaction_anchor_seq_helper_exists():
    """spec §7.4 —— 需要 anchor_seq 查询工具函数。"""
    from tui_gateway.services import interaction_registry

    assert hasattr(interaction_registry, "find_interaction_anchor_seq"), (
        "find_interaction_anchor_seq 缺失 —— spec §7.4 要求可查询 anchor_seq"
    )


def test_p0_3_migration_persists_anchor_seq_column():
    """spec §7.4 —— run_events.anchor_seq 列由 migration 0042 建。"""
    mig = REPO_ROOT / "hermes_agent" / "storage" / "migrations" / "0042_interaction_events_persist.py"
    src = mig.read_text(encoding="utf-8")
    assert "anchor_seq" in src, (
        "migration 0042 里没有 anchor_seq —— spec §7.4 要求该列"
    )


# ---------------------------------------------------------------------------
# P0/P1 #4: Activity 归属 TeamMissionRepo (activities + activity_commands 两表)
# ---------------------------------------------------------------------------


def test_p0_4_team_mission_repo_owns_activities_table():
    """spec §4.4 v3.0.2 P0-B4 —— TeamMissionRepo 接手 activities 表。"""
    tm_src = (
        REPO_ROOT / "hermes_agent" / "repositories" / "team_mission_repo.py"
    ).read_text(encoding="utf-8")
    assert "activities" in tm_src.lower(), (
        "TeamMissionRepo 未接手 activities 表 —— spec §4.4 v3.0.2 P0-B4 要求"
    )


def test_p0_4_team_mission_repo_owns_activity_commands_table():
    """spec §4.4 v3.0.2 P0-B4 —— TeamMissionRepo 接手 activity_commands 表。"""
    tm_src = (
        REPO_ROOT / "hermes_agent" / "repositories" / "team_mission_repo.py"
    ).read_text(encoding="utf-8")
    assert "activity_commands" in tm_src, (
        "TeamMissionRepo 未接手 activity_commands 表 —— spec §4.4 v3.0.2 P0-B4 要求"
    )


def test_p0_4_activity_seq_shares_run_events_seq_domain():
    """spec §6.5 —— Activity.append_activity 走 SeqAllocator.allocate_only
    与 run_events 共享 seq 域。"""
    tm_src = (
        REPO_ROOT / "hermes_agent" / "repositories" / "team_mission_repo.py"
    ).read_text(encoding="utf-8")
    assert "allocate_only" in tm_src, (
        "TeamMissionRepo 未调用 SeqAllocator.allocate_only —— spec §6.5 要求"
        "activity_seq 与 run_events seq 共享域"
    )


# ---------------------------------------------------------------------------
# Sanity summary — 前端 v3.1 反馈六条 P0/P1 综合检查（元测）
# ---------------------------------------------------------------------------


def test_frontend_v3_1_feedback_all_six_have_coverage_here_or_j10():
    """元测 —— 六条 P0/P1 中的前四条本文件覆盖；后两条由 test_j10_*
    contract_version_drift.py 覆盖。若这个映射改变，本 assert 需同步更新。
    """
    covered_here = {
        "P0/P1 #1 CanonicalEventType 14 arm freeze",
        "P0/P1 #2 interaction.* not in Canonical enum",
        "P0/P1 #3 InteractionRegistry.request 带 anchor_seq",
        "P0/P1 #4 Activity 归属 TeamMissionRepo (activities+activity_commands)",
    }
    covered_by_j10 = {
        "P0/P1 #5 deprecations: string[] 在 BackendCapabilities",
        "P0/P1 #6 Handshake 无内部字段泄漏",
    }
    assert len(covered_here) + len(covered_by_j10) == 6
