"""Gateway update/restart lifecycle composition."""

from __future__ import annotations

from hermes_gateway.update_lifecycle import GatewayUpdateLifecycleMixin


class GatewayUpdateRestartMixin(
    GatewayUpdateLifecycleMixin,
):
    pass
