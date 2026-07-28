"""Gateway method registration modules (spec §12 Phase G).

Each domain exposes ``register(registry, ...)`` for wiring at gateway
startup. Method names are grouped by domain (``session.*``, ``run.*``,
``team_mission.*``, ``system.*``).
"""

from __future__ import annotations

from hermes_agent.gateway.methods import (
    agent_profile_methods,
    handshake_method,
    message_methods,
    run_methods,
    session_methods,
    team_mission_methods,
)

__all__ = [
    "agent_profile_methods",
    "handshake_method",
    "message_methods",
    "run_methods",
    "session_methods",
    "team_mission_methods",
]
