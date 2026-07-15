"""
PR-12: 跨仓 E2E 验收 — 三症状原场景复现 + 回归锚测试集

验证 triage 三症状在 PR-1~PR-11 修复后不再复现。

三症状：
  S1: terminal-event-not-persisted — worker/main 混淆导致告警疲劳
  S5: tool_events 双 seq 身份 — canonical seq ≠ tool_events.seq_start
  S8: orphan-recovery main-only — worker 侧噪音扫描

每个测试是"回归锚"——未来改动让症状复现则测试红。
"""
import pytest
import sqlite3
import json
import logging
from unittest.mock import patch, MagicMock


# ── S1: terminal-event 标签拆分 ──────────────────────────────────

class TestS1TerminalEventLabelSplit:
    """S1: terminal-event-not-persisted 一个标签覆盖两种情况。

    修复后（PR-6）：
    - worker 侧 persist=False → DEBUG 'terminal-event-persist-deferred-to-main'
    - main 侧 db=None → ERROR 'terminal-event-dropped-no-db'
    """

    def test_worker_side_uses_debug_label(self):
        """worker 侧 message.complete 发出 DEBUG deferred 标签，不计 error 指标。"""
        from tui_gateway.services import run_control

        with patch('tui_gateway.process_role.is_worker_process', return_value=True):
            with patch.object(run_control, 'logger') as mock_logger:
                # message.complete 触发 terminal_event 路径
                run_control.record_event(
                    params={
                        'type': 'message.complete',
                        'session_id': 'test-session',
                        'run_id': 'test-run',
                        'seq': 1,
                        'payload': {'status': 'completed'},
                    },
                )
                # worker 侧应该用 DEBUG 级别
                debug_calls = [str(c) for c in mock_logger.debug.call_args_list]
                error_calls = [str(c) for c in mock_logger.error.call_args_list]
                all_calls = ' '.join(debug_calls + error_calls)

                assert 'terminal-event-persist-deferred-to-main' in all_calls, \
                    f"worker 侧应该发出 DEBUG 'terminal-event-persist-deferred-to-main'，" \
                    f"实际 debug={debug_calls}, error={error_calls}"

    def test_main_side_uses_error_label(self):
        """main 侧 message.complete + db=None 发出 ERROR dropped 标签。"""
        from tui_gateway.services import run_control

        with patch('tui_gateway.process_role.is_worker_process', return_value=False):
            with patch.object(run_control, 'logger') as mock_logger:
                run_control.record_event(
                    params={
                        'type': 'message.complete',
                        'session_id': 'test-session',
                        'run_id': 'test-run',
                        'seq': 1,
                        'payload': {'status': 'completed'},
                    },
                    db=None,  # main 侧 db=None
                )
                error_calls = [str(c) for c in mock_logger.error.call_args_list]
                combined = ' '.join(error_calls)

                assert 'terminal-event-dropped-no-db' in combined, \
                    f"main 侧应该发出 ERROR 'terminal-event-dropped-no-db'，实际 error={error_calls}"

    def test_old_single_label_not_used(self):
        """旧标签 'terminal-event-not-persisted-no-db-method' 不再使用。"""
        from tui_gateway.services import run_control

        with patch('tui_gateway.process_role.is_worker_process', return_value=False):
            with patch.object(run_control, 'logger') as mock_logger:
                run_control.record_event(
                    params={
                        'type': 'message.complete',
                        'session_id': 'test-session',
                        'run_id': 'test-run',
                        'seq': 1,
                        'payload': {'status': 'completed'},
                    },
                    db=None,
                )
                all_calls = str(mock_logger.error.call_args_list) + \
                    str(mock_logger.debug.call_args_list) + \
                    str(mock_logger.warning.call_args_list)
                assert 'terminal-event-not-persisted-no-db-method' not in all_calls, \
                    "旧标签 terminal-event-not-persisted-no-db-method 不应再出现"


# ── S5: tool_events 双 seq 身份 ──────────────────────────────────

class TestS5ToolEventsDualSeqIdentity:
    """S5: tool_events.seq_start=75 vs canonical run_events.seq=78/79。

    修复后（PR-3）：canonical 事件携带 run_events 真实 seq。
    """

    def _create_db_with_run_events(self, db_path: str) -> sqlite3.Connection:
        """创建带完整 run_events schema 的 DB。"""
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                run_id TEXT,
                turn_id TEXT,
                execution_session_id TEXT,
                runtime_scope_key TEXT,
                participant_id TEXT NOT NULL DEFAULT '',
                activity_id TEXT,
                event_type TEXT NOT NULL,
                seq INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                payload_json TEXT,
                event_json TEXT NOT NULL,
                status TEXT,
                frame_blob BLOB,
                frame_format TEXT,
                retention_class TEXT,
                projected_message_id TEXT,
                projected_tool_event_id TEXT,
                projection_state TEXT,
                runtime_source_seq INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session_id, seq)
            );
        """)
        return conn

    def _insert_run_event(self, conn, seq, session_id, run_id, event_type, payload):
        """插入一条 run_events 记录（带 event_json）。"""
        event_json = json.dumps({
            'type': event_type,
            'seq': seq,
            'run_id': run_id,
            'session_id': session_id,
            'payload': payload,
            'timestamp': float(seq),
        })
        conn.execute(
            """INSERT INTO run_events
               (session_id, run_id, event_type, seq, timestamp, payload_json, event_json, participant_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, '')""",
            (session_id, run_id, event_type, seq, float(seq), json.dumps(payload), event_json)
        )
        conn.commit()

    def test_canonical_seq_not_tool_events_seq_start(self, tmp_path):
        """canonical 事件的 seq 来自 run_events，不等于 tool_events.seq_start。"""
        from hermes_agent.read_models.tool_events import list_tool_events_as_canonical

        db_path = str(tmp_path / "test_s5.db")
        conn = self._create_db_with_run_events(db_path)

        session_id = "test-session-s5"
        run_id = "test-run-s5"

        # 写入 run_events（canonical seq = 78, 79）
        self._insert_run_event(conn, 78, session_id, run_id, 'tool.start',
                               {'tool_id': 'call-1', 'name': 'search_files'})
        self._insert_run_event(conn, 79, session_id, run_id, 'tool.complete',
                               {'tool_id': 'call-1', 'name': 'search_files', 'status': 'completed'})

        # 查询 canonical 事件
        events = list_tool_events_as_canonical(conn, session_id)

        assert len(events) >= 2, f"应该有 2+ canonical 事件，实际: {len(events)}"
        for event in events:
            seq = event.get('seq', 0)
            assert seq in (78, 79), \
                f"canonical seq 应该是 78/79（来自 run_events），实际: {seq}"
            assert seq != 75, \
                "canonical seq 不应该等于 tool_events.seq_start=75"

        conn.close()

    def test_after_seq_cursor_filters_correctly(self, tmp_path):
        """after_seq cursor 在 canonical seq 上过滤。"""
        from hermes_agent.read_models.tool_events import list_tool_events_as_canonical

        db_path = str(tmp_path / "test_s5_cursor.db")
        conn = self._create_db_with_run_events(db_path)

        session_id = "test-session-cursor"
        run_id = "test-run-cursor"

        # 写入 4 个 tool 事件 (seq=100,101,102,103)
        for i in range(4):
            seq = 100 + i
            etype = 'tool.start' if i % 2 == 0 else 'tool.complete'
            self._insert_run_event(conn, seq, session_id, run_id, etype,
                                   {'tool_id': f'call-{i}', 'name': f'tool_{i}'})

        # after_seq=101 应该只返回 seq > 101 的事件（即 102,103）
        events = list_tool_events_as_canonical(conn, session_id, after_seq=101)
        assert all(e['seq'] > 101 for e in events), \
            "after_seq=101 应该只返回 seq > 101 的事件"
        assert len(events) == 2, \
            f"after_seq=101 应该返回 2 个事件，实际: {len(events)}"

        conn.close()


# ── S8: orphan-recovery main-only ──────────────────────────────────

class TestS8OrphanRecoveryMainOnly:
    """S8: orphan-recovery worker 侧 79 次噪音扫描。

    修复后（PR-6）：worker 侧短路 return 0，不扫描 DB。
    """

    def test_worker_skips_scan(self):
        """worker 侧 is_worker_process()=True 时不扫描 DB。"""
        from tui_gateway.services import run_control

        with patch('tui_gateway.process_role.is_worker_process', return_value=True):
            with patch.object(run_control, '_run_method') as mock_run_method:
                result = run_control._recover_orphaned_active_runs()

                assert result == 0, "worker 侧应该返回 0"
                mock_run_method.assert_not_called(), \
                    "worker 侧不应该调用 DB 方法"

    def test_main_runs_scan(self):
        """main 侧 is_worker_process()=False 时正常扫描。"""
        from tui_gateway.services import run_control

        mock_method = MagicMock(return_value=3)
        with patch('tui_gateway.process_role.is_worker_process', return_value=False):
            with patch.object(run_control, '_run_method', return_value=mock_method):
                result = run_control._recover_orphaned_active_runs(object())

                mock_method.assert_called(), \
                    "main 侧应该调用 DB 扫描方法"
                assert result == 3

    def test_worker_returns_zero_with_db(self):
        """worker 侧即使有 DB 也返回 0，不扫描。"""
        from tui_gateway.services import run_control

        mock_method = MagicMock(return_value=999)
        with patch('tui_gateway.process_role.is_worker_process', return_value=True):
            with patch.object(run_control, '_run_method', return_value=mock_method):
                result = run_control._recover_orphaned_active_runs()

                assert result == 0, \
                    "worker 侧即使有 DB 也应该返回 0"
                mock_method.assert_not_called(), \
                    "worker 侧不应该调用 DB 扫描"


# ── 回归锚：respond 三态契约 ────────────────────────────────────

class TestRespondThreeStateContract:
    """respond 三态契约回归锚（PR-5 核心）。

    miss → 4404, hit+resolved → 4409, success → resolved。
    """

    def test_pending_registry_register_and_lookup(self):
        """PendingRegistry register → lookup → mark_resolved 基本流程。"""
        from hermes_agent.orchestration.worker_frame_router import PendingRegistry

        registry = PendingRegistry()
        registry.register(
            request_id='test-req-1',
            kind='approval',
            session_key='session-1',
        )

        entry = registry.lookup('test-req-1')
        assert entry is not None, "register 后 lookup 应该找到"
        assert entry.state == 'pending', f"初始状态应该是 pending，实际: {entry.state}"

        resolved = registry.mark_resolved('test-req-1', 'approve')
        assert resolved, "mark_resolved 应该返回 True"

        entry = registry.lookup('test-req-1')
        assert entry.state == 'resolved', f"resolve 后状态应该是 resolved，实际: {entry.state}"

    def test_pending_registry_miss_returns_none(self):
        """lookup 不存在的 request_id 返回 None（miss 语义）。"""
        from hermes_agent.orchestration.worker_frame_router import PendingRegistry

        registry = PendingRegistry()
        entry = registry.lookup('nonexistent')
        assert entry is None, "lookup 不存在的 request_id 应该返回 None"

    def test_mark_resolved_already_resolved_returns_false(self):
        """mark_resolved 已 resolved 的 entry 返回 False（幂等）。"""
        from hermes_agent.orchestration.worker_frame_router import PendingRegistry

        registry = PendingRegistry()
        registry.register(
            request_id='test-req-2',
            kind='approval',
            session_key='session-2',
        )

        first = registry.mark_resolved('test-req-2', 'approve')
        assert first, "第一次 mark_resolved 应该返回 True"

        second = registry.mark_resolved('test-req-2', 'deny')
        assert not second, "第二次 mark_resolved（已 resolved）应该返回 False"
