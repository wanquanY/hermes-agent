"""Deprecated auto-title compatibility facade.

Product titles now come from the first user message, with user rename as the
only automatic override.  These functions remain import-compatible for older
callers, but they intentionally do not call an LLM, start a worker, or write a
session title.
"""

from typing import Callable, Optional

# Callback signature: (task_name, exception) -> None. Used to surface
# auxiliary failures to the user through AIAgent._emit_auxiliary_failure
# so silent-drops (e.g. OpenRouter 402 exhausting the fallback chain)
# become visible instead of piling up as NULL session titles.
FailureCallback = Callable[[str, BaseException], None]
TitleCallback = Callable[[str], None]


def generate_title(
    user_message: str,
    assistant_response: str,
    timeout: float = 30.0,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
) -> Optional[str]:
    """Return no automatic title.

    The parameters are kept for source compatibility with older callers and
    tests.  No callbacks are invoked because there is no auxiliary operation to
    report.
    """
    return None


def auto_title_session(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
) -> None:
    """No-op compatibility worker for the removed auto-title feature."""
    return None


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: str,
    assistant_response: str,
    conversation_history: list,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
) -> None:
    """Do not start title generation; first-user-message titles are canonical."""
    return None
