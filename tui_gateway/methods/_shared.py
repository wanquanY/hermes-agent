from __future__ import annotations

from typing import Any


class ServerProxy:
    def __init__(self, name: str):
        self._name = name

    def _target(self):
        from tui_gateway import server

        return getattr(server, self._name)

    def __call__(self, *args, **kwargs):
        return self._target()(*args, **kwargs)

    def __getattr__(self, item: str):
        return getattr(self._target(), item)

    def __bool__(self):
        return bool(self._target())

    def __len__(self):
        return len(self._target())

    def __iter__(self):
        return iter(self._target())

    def __contains__(self, item):
        return item in self._target()

    def __getitem__(self, item):
        return self._target()[item]

    def __setitem__(self, item, value):
        self._target()[item] = value

    def __delitem__(self, item):
        del self._target()[item]


def bind_server_globals(target: dict[str, Any]):
    """Bind legacy gateway globals into a method module.

    The gateway is being decomposed by responsibility while preserving the
    existing JSON-RPC behavior. Method modules are imported after
    ``tui_gateway.server`` has initialized its shared process state, so this
    adapter gives extracted handlers access to the same state objects and
    helper functions without moving everything in one risky step.
    """
    from tui_gateway import server

    dynamic_names = {"_sessions", "_pending", "_answers", "_methods", "_db", "_db_error"}
    target.update(
        {
            name: ServerProxy(name) if callable(value) or name in dynamic_names else value
            for name, value in vars(server).items()
            if not name.startswith("__")
        }
    )
    return server
