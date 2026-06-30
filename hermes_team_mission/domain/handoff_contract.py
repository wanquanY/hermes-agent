from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind


HANDOFF_DELIVERY_CHANNELS = frozenset({"handoff", "internal_handoff"})
HANDOFF_EFFECTIVE_SOURCES = frozenset({"authoritative", "derived_degraded", "legacy_imported"})
HANDOFF_EXEMPT_NODE_KINDS = frozenset({"root", "approval_gate"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy_contract_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return _text(value).lower() in {"1", "true", "yes", "on"}


def output_contract_requires_handoff(output_contract: Mapping[str, Any] | None) -> bool:
    contract = output_contract if isinstance(output_contract, Mapping) else {}
    if _truthy_contract_flag(contract.get("requires_explicit_handoff")):
        return True
    if _truthy_contract_flag(contract.get("requiresExplicitHandoff")):
        return True
    if _truthy_contract_flag(contract.get("requires_deliverable")):
        return True
    if _truthy_contract_flag(contract.get("requiresDeliverable")):
        return True
    delivery_channel = _text(contract.get("delivery_channel") or contract.get("deliveryChannel")).lower()
    return delivery_channel in HANDOFF_DELIVERY_CHANNELS


def node_requires_authoritative_handoff(node: Mapping[str, Any] | None) -> bool:
    if not isinstance(node, Mapping):
        return False
    if normalize_team_mission_node_kind(node.get("kind")) in HANDOFF_EXEMPT_NODE_KINDS:
        return False
    output_contract = node.get("output_contract") if isinstance(node.get("output_contract"), Mapping) else {}
    return output_contract_requires_handoff(output_contract)


def deliverable_is_effective_handoff(deliverable: Mapping[str, Any] | None) -> bool:
    if not isinstance(deliverable, Mapping) or not deliverable:
        return False
    return _text(deliverable.get("source")).lower() in HANDOFF_EFFECTIVE_SOURCES
