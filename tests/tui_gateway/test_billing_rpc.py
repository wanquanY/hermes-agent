from __future__ import annotations

import pytest

from tui_gateway import server
from tui_gateway.methods import billing


@pytest.mark.parametrize(
    ("card", "expected"),
    [
        ("canonical", {"kind": "canonical"}),
        (
            "distinct",
            {
                "kind": "distinct",
                "payment_method_id": "pm_auto",
                "brand": None,
                "last4": None,
            },
        ),
        ("none", {"kind": "none"}),
    ],
)
def test_billing_state_serializes_auto_reload_card_union(monkeypatch, card, expected):
    from agent.billing_view import AutoReload, AutoReloadCard, BillingState

    monkeypatch.setattr(billing, "_usage_payload", lambda state: {"available": False})
    state = BillingState(
        logged_in=True,
        auto_reload=AutoReload(
            enabled=True,
            card=AutoReloadCard(
                kind=card,
                payment_method_id="pm_auto" if card == "distinct" else None,
            ),
        ),
    )

    result = billing._serialize_billing_state(state)

    assert result["auto_reload"]["card"] == expected


def test_billing_state_serializes_server_plan_capability(monkeypatch):
    from agent.billing_view import BillingState

    monkeypatch.setattr(billing, "_usage_payload", lambda state: {"available": False})
    state = BillingState(
        logged_in=True,
        role="MEMBER",
        can_change_plan_raw=True,
    )

    result = billing._serialize_billing_state(state)

    assert result["is_admin"] is False
    assert result["can_change_plan"] is True


class _BillingHeaders:
    def __init__(self, values):
        self._values = values

    def get(self, key):
        return self._values.get(key)


@pytest.mark.parametrize(
    ("status", "error", "retry_after"),
    [
        (503, "stripe_unavailable", 75),
        (429, "upgrade_cap_exceeded", None),
        (429, "rate_limited", None),
    ],
)
def test_billing_error_serialization_preserves_server_code(
    status,
    error,
    retry_after,
):
    import hermes_cli.nous_billing as nb

    headers = _BillingHeaders({"Retry-After": str(retry_after)}) if retry_after else None
    with pytest.raises(nb.BillingTransient) as caught:
        nb._raise_for_error(status, {"error": error}, headers)

    result = server._serialize_billing_error(caught.value)

    assert result["error"] == error
    assert caught.value.error == error
    assert result["retry_after"] == retry_after


def test_billing_rate_limit_without_error_defaults_wire_code():
    import hermes_cli.nous_billing as nb

    exc = nb.BillingRateLimited("slow down", status=429, retry_after=10)

    result = server._serialize_billing_error(exc)

    assert result["error"] == "rate_limited"


def _sub_rpc(method, params):
    return server._methods[method]("1", params)["result"]


def test_session_usage_embeds_canonical_dollar_model(monkeypatch):
    sid = "billing-usage-model"
    model = {
        "ok": True,
        "available": True,
        "status": "healthy",
        "plan_name": "Plus",
        "plan_bar": None,
        "topup_bar": None,
    }
    monkeypatch.setattr(billing, "build_usage_payload", lambda: model)
    with server._sessions_lock:
        server._sessions[sid] = {"session_key": sid, "agent": None}
    try:
        response = server.handle_request(
            {
                "id": "usage-1",
                "method": "session.usage",
                "params": {"session_id": sid},
            }
        )
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)

    assert response["result"]["usage"] == model


def test_subscription_preview_serializes_quote(monkeypatch):
    import hermes_cli.nous_billing as nb

    monkeypatch.setattr(
        nb,
        "post_subscription_preview",
        lambda subscription_type_id: {
            "effect": "charge_now",
            "reason": None,
            "currentTierId": "plus",
            "currentTierName": "Plus",
            "targetTierId": "ultra",
            "targetTierName": "Ultra",
            "monthlyCreditsDelta": "6000",
            "amountDueNowCents": 1234,
            "effectiveAt": None,
        },
    )

    result = _sub_rpc("subscription.preview", {"subscription_type_id": "ultra"})

    assert result["ok"] is True
    assert result["effect"] == "charge_now"
    assert result["amount_due_now_cents"] == 1234
    assert result["target_tier_name"] == "Ultra"
    assert result["monthly_credits_delta"] == "6000"


def test_subscription_preview_requires_tier():
    result = _sub_rpc("subscription.preview", {})

    assert result["ok"] is False
    assert result["error"] == "invalid_request"


def test_subscription_preview_scope_error_maps_to_step_up(monkeypatch):
    import hermes_cli.nous_billing as nb

    def raise_scope_error(subscription_type_id):
        raise nb.BillingScopeRequired("billing:manage required")

    monkeypatch.setattr(nb, "post_subscription_preview", raise_scope_error)

    result = _sub_rpc("subscription.preview", {"subscription_type_id": "ultra"})

    assert result["ok"] is False
    assert result["error"] == "insufficient_scope"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"cancel": True}, {"tier": None, "cancel": True}),
        ({"subscription_type_id": "plus"}, {"tier": "plus", "cancel": False}),
    ],
)
def test_subscription_change_routes_pending_change(monkeypatch, params, expected):
    import hermes_cli.nous_billing as nb

    seen = {}

    def put_change(*, subscription_type_id=None, cancel=False):
        seen["tier"] = subscription_type_id
        seen["cancel"] = cancel
        return {"message": "Scheduled."}

    monkeypatch.setattr(nb, "put_subscription_pending_change", put_change)

    result = _sub_rpc("subscription.change", params)

    assert result["ok"] is True
    assert seen == expected


def test_subscription_change_requires_tier_or_cancel():
    result = _sub_rpc("subscription.change", {})

    assert result["ok"] is False
    assert result["error"] == "invalid_request"


def test_subscription_resume(monkeypatch):
    import hermes_cli.nous_billing as nb

    monkeypatch.setattr(
        nb,
        "delete_subscription_pending_change",
        lambda: {"message": "Resumed."},
    )

    result = _sub_rpc("subscription.resume", {})

    assert result["ok"] is True
    assert result["message"] == "Resumed."


def test_subscription_upgrade_echoes_status_and_idempotency(monkeypatch):
    import hermes_cli.nous_billing as nb

    seen = {}

    def upgrade(*, subscription_type_id, idempotency_key):
        seen["tier"] = subscription_type_id
        seen["key"] = idempotency_key
        return {"status": "upgraded", "targetTierName": "Ultra"}

    monkeypatch.setattr(nb, "post_subscription_upgrade", upgrade)

    result = _sub_rpc(
        "subscription.upgrade",
        {"subscription_type_id": "ultra", "idempotency_key": "k-1"},
    )

    assert result["ok"] is True
    assert result["status"] == "upgraded"
    assert result["target_tier_name"] == "Ultra"
    assert result["idempotency_key"] == "k-1"
    assert seen == {"tier": "ultra", "key": "k-1"}


def test_subscription_upgrade_requires_action_surfaces_recovery(monkeypatch):
    import hermes_cli.nous_billing as nb

    monkeypatch.setattr(
        nb,
        "post_subscription_upgrade",
        lambda *, subscription_type_id, idempotency_key: {
            "status": "requires_action",
            "reason": "authentication_required",
            "recoveryUrl": "https://portal.example/subscription?org_id=o",
        },
    )

    result = _sub_rpc("subscription.upgrade", {"subscription_type_id": "ultra"})

    assert result["ok"] is True
    assert result["status"] == "requires_action"
    assert result["recovery_url"].startswith("https://portal.example")
    assert result["idempotency_key"]
