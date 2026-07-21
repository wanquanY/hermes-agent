"""Focused regressions for manually absorbed upstream MCP behavior."""

import asyncio
import concurrent.futures
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp_tool import _MCP_LIST_MAX_PAGES, _paginate_full_list


def _item(name: str):
    item = MagicMock()
    item.name = name
    return item


class TestPaginatedDiscovery:
    def test_single_page_without_cursor(self):
        list_method = AsyncMock(
            return_value=SimpleNamespace(tools=[_item("a"), _item("b")])
        )

        items = asyncio.run(_paginate_full_list(list_method, "tools", "srv"))

        assert [item.name for item in items] == ["a", "b"]
        list_method.assert_called_once_with()

    def test_follows_opaque_string_cursors(self):
        pages = {
            None: SimpleNamespace(tools=[_item("one")], nextCursor="page-2"),
            "page-2": SimpleNamespace(tools=[], nextCursor="page-3"),
            "page-3": SimpleNamespace(tools=[_item("three")]),
        }

        async def list_method(cursor=None):
            return pages[cursor]

        items = asyncio.run(_paginate_full_list(list_method, "tools", "srv"))

        assert [item.name for item in items] == ["one", "three"]

    def test_non_string_cursor_ends_pagination(self):
        list_method = AsyncMock(
            return_value=SimpleNamespace(tools=[_item("only")], nextCursor=123)
        )

        items = asyncio.run(_paginate_full_list(list_method, "tools", "srv"))

        assert [item.name for item in items] == ["only"]
        list_method.assert_called_once_with()

    def test_runaway_cursor_is_bounded(self):
        calls = 0

        async def list_method(cursor=None):
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                resources=[_item(str(calls))],
                nextCursor=f"cursor-{calls}",
            )

        items = asyncio.run(
            _paginate_full_list(list_method, "resources", "hostile-server")
        )

        assert calls == _MCP_LIST_MAX_PAGES
        assert len(items) == _MCP_LIST_MAX_PAGES


class TestCompletedFutureTimeout:
    @staticmethod
    def _run_with_future(future):
        import tools.mcp_tool as mcp_tool

        loop = MagicMock()
        loop.is_running.return_value = True

        async def unused_call():
            return "unused"

        def schedule(coro, scheduled_loop, **_kwargs):
            assert scheduled_loop is loop
            coro.close()
            return future

        with patch.object(mcp_tool, "_mcp_loop", loop), patch(
            "agent.async_utils.safe_schedule_threadsafe",
            side_effect=schedule,
        ):
            return mcp_tool._run_on_mcp_loop(unused_call(), timeout=1)

    def test_propagates_timeout_stored_by_completed_future(self):
        inner_error = TimeoutError("inner MCP timeout")
        future = concurrent.futures.Future()
        future.set_exception(inner_error)

        with pytest.raises(TimeoutError, match="inner MCP timeout") as exc_info:
            self._run_with_future(future)

        assert exc_info.value is inner_error

    def test_poll_timeout_racing_success_returns_result(self):
        class PollThenSuccess(concurrent.futures.Future):
            def __init__(self):
                super().__init__()
                self.polls = 0

            def result(self, timeout=None):
                self.polls += 1
                if self.polls == 1:
                    self.set_result("completed")
                    raise concurrent.futures.TimeoutError
                return super().result(timeout=timeout)

        future = PollThenSuccess()

        assert self._run_with_future(future) == "completed"
        assert future.polls == 2
