"""Platform adapter contracts and implementations."""

from .base import BasePlatformAdapter
from .base import MessageEvent
from .base import SendResult

__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "SendResult",
    "YuanbaoAdapter",
]


def __getattr__(name: str):
    if name == "YuanbaoAdapter":
        from .yuanbao import YuanbaoAdapter

        return YuanbaoAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
