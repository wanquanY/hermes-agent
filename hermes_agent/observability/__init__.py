"""Observability layer — error taxonomy + drop/degrade/fallback tagging (spec §10.2)."""

from __future__ import annotations

from hermes_agent.observability.error_taxonomy import (
    DEGRADE_TAG,
    DROP_TAG,
    FALLBACK_TAG,
    FatalError,
    ObservabilityEvent,
    RecoverableError,
    log_degrade,
    log_drop,
    log_fallback,
)
from hermes_agent.observability.mutation_harness import (
    MutationHarnessError,
    MutationResult,
    MutationSpec,
    apply_mutation,
)
from hermes_agent.observability.module_liveness_audit import (
    LivenessAudit,
    ModuleLivenessReport,
    audit_liveness,
)

__all__ = [
    "DEGRADE_TAG",
    "DROP_TAG",
    "FALLBACK_TAG",
    "FatalError",
    "LivenessAudit",
    "ModuleLivenessReport",
    "MutationHarnessError",
    "MutationResult",
    "MutationSpec",
    "ObservabilityEvent",
    "RecoverableError",
    "apply_mutation",
    "audit_liveness",
    "log_degrade",
    "log_drop",
    "log_fallback",
]
