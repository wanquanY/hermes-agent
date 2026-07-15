"""L3 Orchestration layer (spec §3, §8).

Single-replica in-flight state (WorkerPool) + sharded lock (ShardedRpcLock)
are the spec §12 Phase F primitives that the higher-level RunOrchestrator
and TeamMissionOrchestrator will build on.
"""

from __future__ import annotations

from hermes_agent.orchestration.rpc_lock import (
    DEFAULT_LEASE_TTL,
    DEFAULT_TIMEOUT,
    LONG_OP_TIMEOUT,
    LockLease,
    RpcBusy,
    ShardedRpcLock,
)
from hermes_agent.orchestration.run_orchestrator import (
    RunLaunchResult,
    RunLaunchSpec,
    RunOrchestrator,
)
from hermes_agent.orchestration.team_mission_orchestrator import (
    MissionAdvanceOutcome,
    TeamMissionOrchestrator,
)
from hermes_agent.orchestration.worker_pool import InflightRun, WorkerPool

__all__ = [
    "DEFAULT_LEASE_TTL",
    "DEFAULT_TIMEOUT",
    "InflightRun",
    "LONG_OP_TIMEOUT",
    "LockLease",
    "MissionAdvanceOutcome",
    "RpcBusy",
    "RunLaunchResult",
    "RunLaunchSpec",
    "RunOrchestrator",
    "ShardedRpcLock",
    "TeamMissionOrchestrator",
    "WorkerPool",
]
