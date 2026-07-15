"""Composite Gateway Kanban watcher ownership."""

from __future__ import annotations

from hermes_gateway.kanban_artifacts import GatewayKanbanArtifactMixin
from hermes_gateway.kanban_dispatcher import GatewayKanbanDispatcherMixin
from hermes_gateway.kanban_notifier import GatewayKanbanNotifierMixin


class GatewayKanbanWatcherMixin(
    GatewayKanbanNotifierMixin,
    GatewayKanbanArtifactMixin,
    GatewayKanbanDispatcherMixin,
):
    pass
