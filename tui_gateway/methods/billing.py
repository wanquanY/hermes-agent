# ruff: noqa: F401,F403,F405,F821,ARG001
from __future__ import annotations

from tui_gateway.methods._shared import bind_server_globals

_server = bind_server_globals(globals())


@method("credits.view")
def _(rid, params: dict) -> dict:
    """Structured Nous credit view for the TUI /credits command.

    Account-independent (a portal fetch gated on "a Nous account is logged in"),
    so it works with no live agent / on a resumed session — same as the /usage
    credits block. Returns the surface-agnostic CreditsView fields so the TUI can
    render a clickable top-up <Link>. Fail-open: a portal hiccup or logged-out
    account yields {logged_in: false}, never an error the user has to parse.
    """
    try:
        from agent.account_usage import build_credits_view

        view = build_credits_view()
        return _ok(
            rid,
            {
                "logged_in": bool(view.logged_in),
                "balance_lines": [
                    line for line in view.balance_lines if not line.lstrip().startswith("📈")
                ],
                "identity_line": view.identity_line,
                "topup_url": view.topup_url,
                "depleted": bool(view.depleted),
            },
        )
    except Exception:
        # Fail-open: TUI treats this as "not logged in" and shows the prompt.
        return _ok(rid, {"logged_in": False, "balance_lines": [], "identity_line": None, "topup_url": None, "depleted": False})

# Phase 2b terminal billing RPC methods
# ===========================================================================
#
# These return STRUCTURED success envelopes (result.ok / result.error) rather
# than JSON-RPC-level errors, so the TUI's rpc() promise always resolves and the
# Ink side can branch on the typed billing error code (insufficient_scope,
# rate_limited, no_payment_method, …) to render the right affordance instead of
# landing in a generic catch. The data-building lives in the shared core
# (agent/billing_view.py + hermes_cli/nous_billing.py) — same as /topup.


def _serialize_billing_error(exc) -> dict:
    """Map a BillingError into the result.error envelope the TUI branches on."""
    from hermes_cli.nous_billing import (
        BillingRemoteSpendingRevoked,
        BillingScopeRequired,
        BillingSessionRevoked,
        BillingTransient,
    )

    kind = "error"
    if isinstance(exc, BillingRemoteSpendingRevoked):
        kind = "remote_spending_revoked"
    elif isinstance(exc, BillingSessionRevoked):
        kind = "session_revoked"
    elif isinstance(exc, BillingScopeRequired):
        kind = "insufficient_scope"
    elif isinstance(exc, BillingTransient):
        kind = str(exc.error) if getattr(exc, "error", None) else "rate_limited"
    elif getattr(exc, "error", None):
        kind = str(exc.error)
    return {
        "ok": False,
        "error": kind,
        "message": str(exc),
        "portal_url": getattr(exc, "portal_url", None),
        "retry_after": getattr(exc, "retry_after", None),
        "payload": getattr(exc, "payload", {}) or {},
        # Remote-Spending contract extras (threaded so the TUI can render
        # actor-aware copy + route recovery without re-parsing the message).
        "actor": getattr(exc, "actor", None),
        "code": getattr(exc, "code", None),
        "recovery": getattr(exc, "recovery", None),
    }


def _serialize_billing_state(state) -> dict:
    """Serialize a BillingState for the wire (Decimals → strings, money-safe)."""
    from agent.billing_view import format_money

    def _s(value):
        return None if value is None else str(value)

    card = None
    if state.card is not None:
        card = {
            "brand": state.card.brand,
            "last4": state.card.last4,
            "masked": state.card.masked,
            # Post-card-resolver fields (None/False on older NAS payloads):
            # display = "Visa ····4242 — the card on your subscription";
            # resolved_via = the raw resolution rung, for rung-gated surfaces
            # (the /subscription confirm only shows the card when the rung
            # matches what a subscription charge would use).
            "display": state.card.display,
            "resolved_via": state.card.resolved_via,
        }
    monthly_cap = None
    if state.monthly_cap is not None:
        mc = state.monthly_cap
        monthly_cap = {
            "limit_usd": _s(mc.limit_usd),
            "limit_display": format_money(mc.limit_usd),
            "spent_this_month_usd": _s(mc.spent_this_month_usd),
            "spent_display": format_money(mc.spent_this_month_usd),
            "is_default_ceiling": mc.is_default_ceiling,
        }
    auto_reload = None
    if state.auto_reload is not None:
        ar = state.auto_reload
        card_out = None
        if ar.card is not None:
            if ar.card.kind == "distinct":
                card_out = {
                    "kind": "distinct",
                    "payment_method_id": ar.card.payment_method_id,
                    "brand": ar.card.brand,
                    "last4": ar.card.last4,
                }
            else:
                card_out = {"kind": ar.card.kind}
        auto_reload = {
            "enabled": ar.enabled,
            "threshold_usd": _s(ar.threshold_usd),
            "threshold_display": format_money(ar.threshold_usd),
            "reload_to_usd": _s(ar.reload_to_usd),
            "reload_to_display": format_money(ar.reload_to_usd),
            "card": card_out,
        }
    return {
        "ok": True,
        "logged_in": state.logged_in,
        "org_name": state.org_name,
        "org_slug": state.org_slug,
        "role": state.role,
        "is_admin": state.is_admin,
        "can_change_plan": state.can_change_plan,
        "can_charge": state.can_charge,
        "balance_usd": _s(state.balance_usd),
        "balance_display": format_money(state.balance_usd),
        "cli_billing_enabled": state.cli_billing_enabled,
        "charge_presets": [_s(p) for p in state.charge_presets],
        "charge_presets_display": [format_money(p) for p in state.charge_presets],
        "min_usd": _s(state.min_usd),
        "max_usd": _s(state.max_usd),
        "card": card,
        "monthly_cap": monthly_cap,
        "auto_reload": auto_reload,
        "portal_url": state.portal_url,
        "error": state.error,
        # Shared dollar usage model (two-bar view) embedded so /topup renders the
        # same plan + top-up bars as /usage and /subscription from its single
        # fetch. Built from the separate account-info path; fail-open when logged
        # out or the portal is down.
        "usage": _usage_payload(state),
    }


def _usage_payload(state) -> dict:
    """Best-effort shared usage model for the /topup + /subscription overlay bars.

    Only fetched when logged in; fail-open to {available:false} so the overview
    still renders if the account-info path is down.
    """
    if not getattr(state, "logged_in", False):
        return {"available": False}
    return build_usage_payload()


@method("billing.state")
def _(rid, params: dict) -> dict:
    """GET /api/billing/state → serialized BillingState (Screen 1 + 5).

    Fail-open like the other billing RPCs: a logged-out / unreachable portal yields
    {ok:true, logged_in:false}. No scope required for this endpoint.
    """
    try:
        from agent.billing_view import build_billing_state

        state = build_billing_state()
        return _ok(rid, _serialize_billing_state(state))
    except Exception:
        return _ok(rid, {"ok": True, "logged_in": False, "error": "could not load billing state"})


def _serialize_usage_bar(bar) -> Optional[dict]:
    """Serialize a UsageBar (dollar magnitudes → display strings + fractions)."""
    if bar is None:
        return None
    from agent.billing_usage import _fmt_usd

    return {
        "kind": bar.kind,
        "remaining_display": _fmt_usd(bar.remaining_usd),
        "total_display": _fmt_usd(bar.total_usd),
        "spent_display": _fmt_usd(bar.spent_usd),
        "pct_used": bar.pct_used,
        "fill_fraction": bar.fill_fraction,
    }


def _serialize_usage_model(model) -> dict:
    """Serialize a UsageModel for the wire — the shared two-bar dollar view.

    Dollars-only (no 'credits'); fail-open shape mirrors the other billing RPCs
    ({ok, available:false} when logged out / unreachable).
    """
    from agent.billing_usage import _fmt_usd, format_renews

    if model is None or not getattr(model, "available", False):
        return {"ok": True, "available": False}

    return {
        "ok": True,
        "available": True,
        "status": model.status,
        "plan_name": model.plan_name,
        "renews_at": model.renews_at,
        "renews_display": getattr(model, "renews_display", None) or format_renews(model.renews_at),
        "subscription_remaining_display": (
            None if model.subscription_remaining_usd is None else _fmt_usd(model.subscription_remaining_usd)
        ),
        "topup_remaining_display": (
            None if model.topup_remaining_usd is None else _fmt_usd(model.topup_remaining_usd)
        ),
        "total_spendable_display": (
            None if model.total_spendable_usd is None else _fmt_usd(model.total_spendable_usd)
        ),
        "has_topup": model.has_topup,
        "plan_bar": _serialize_usage_bar(model.plan_bar),
        "topup_bar": _serialize_usage_bar(model.topup_bar),
    }


def build_usage_payload() -> dict:
    """Build the canonical fail-open dollar usage wire model."""
    try:
        from agent.billing_usage import build_usage_model

        return _serialize_usage_model(build_usage_model())
    except Exception:
        return {"ok": True, "available": False}


@method("usage.bars")
def _(rid, params: dict) -> dict:
    """Shared dollar usage model (two-bar view) for /usage + /subscription.

    Fail-open: logged-out / unreachable portal → {ok:true, available:false}.
    No scope required (read-only).
    """
    return _ok(rid, build_usage_payload())


def _serialize_subscription_state(state) -> dict:
    """Serialize a SubscriptionState for the wire (Decimals → strings)."""
    from agent.billing_usage import format_renews
    from agent.billing_view import format_money

    def _s(value):
        return None if value is None else str(value)

    current = None
    if state.current is not None:
        c = state.current
        current = {
            "tier_id": c.tier_id,
            "tier_name": c.tier_name,
            "monthly_credits": _s(c.monthly_credits),
            "credits_remaining": _s(c.credits_remaining),
            "cycle_ends_at": c.cycle_ends_at,
            "pending_downgrade_tier_name": c.pending_downgrade_tier_name,
            "pending_downgrade_at": c.pending_downgrade_at,
            "pending_downgrade_display": format_renews(c.pending_downgrade_at),
            "cancel_at_period_end": c.cancel_at_period_end,
            "cancellation_effective_at": c.cancellation_effective_at,
            "cancellation_effective_display": format_renews(c.cancellation_effective_at),
        }
    # Selectable catalog for the in-terminal tier picker; price is pre-formatted
    # ($X / $X.YY) so the TUI renders it directly.
    tiers = [
        {
            "tier_id": t.tier_id,
            "name": t.name,
            "tier_order": t.tier_order,
            "dollars_per_month_display": format_money(t.dollars_per_month),
            "monthly_credits": _s(t.monthly_credits),
            "is_current": t.is_current,
            "is_enabled": t.is_enabled,
        }
        for t in state.tiers
    ]
    return {
        "ok": True,
        "logged_in": state.logged_in,
        "is_admin": state.is_admin,
        "can_change_plan": state.can_change_plan,
        "org_name": state.org_name,
        "org_id": state.org_id,
        "role": state.role,
        "context": state.context,
        "current": current,
        "tiers": tiers,
        "portal_url": state.portal_url,
        "error": state.error,
        # Shared dollar usage model (two-bar view) embedded so /subscription
        # renders the same bars as /usage from its single fetch. Built from the
        # separate account-info path (the only source with top-up dollars);
        # fail-open → {available:false}. Computed lazily so a logged-out state
        # adds no cost.
        "usage": _usage_payload(state),
    }


@method("subscription.state")
def _(rid, params: dict) -> dict:
    """GET /api/billing/subscription → serialized SubscriptionState.

    Fail-open like billing.state: logged-out / unreachable portal →
    {ok:true, logged_in:false}. No scope required (read-only).
    """
    try:
        from agent.subscription_view import build_subscription_state

        state = build_subscription_state()
        return _ok(rid, _serialize_subscription_state(state))
    except Exception:
        return _ok(rid, {"ok": True, "logged_in": False, "error": "could not load subscription state"})


def _serialize_subscription_preview(p) -> dict:
    """Serialize a SubscriptionChangePreview for the wire (Decimal → string)."""
    return {
        "ok": True,
        "effect": p.effect,
        "reason": p.reason,
        "current_tier_id": p.current_tier_id,
        "current_tier_name": p.current_tier_name,
        "target_tier_id": p.target_tier_id,
        "target_tier_name": p.target_tier_name,
        "monthly_credits_delta": (
            None if p.monthly_credits_delta is None else str(p.monthly_credits_delta)
        ),
        "amount_due_now_cents": p.amount_due_now_cents,
        "effective_at": p.effective_at,
    }


@method("subscription.preview")
def _(rid, params: dict) -> dict:
    """POST /api/billing/subscription/preview → serialized quote or typed error.

    params: {subscription_type_id: str}. Chargeless effect quote. Requires
    billing:manage (live Stripe calls + amounts), so a 403 → insufficient_scope
    drives the device step-up exactly like the mutations.
    """
    from agent.subscription_view import subscription_change_preview_from_payload
    from hermes_cli.nous_billing import BillingError, post_subscription_preview

    tier_id = params.get("subscription_type_id")
    if not tier_id:
        return _ok(rid, {"ok": False, "error": "invalid_request", "message": "subscription_type_id is required"})
    try:
        preview = subscription_change_preview_from_payload(
            post_subscription_preview(subscription_type_id=tier_id)
        )
        return _ok(rid, _serialize_subscription_preview(preview))
    except BillingError as exc:
        return _ok(rid, _serialize_billing_error(exc))
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc)})


@method("subscription.change")
def _(rid, params: dict) -> dict:
    """PUT /api/billing/subscription/pending-change → {ok, message} or typed error.

    params: {subscription_type_id?: str, cancel?: bool}. Schedules a downgrade /
    same-price change OR a cancellation at period end (chargeless). Requires
    billing:manage.
    """
    from hermes_cli.nous_billing import BillingError, put_subscription_pending_change

    cancel = bool(params.get("cancel"))
    tier_id = params.get("subscription_type_id")
    if not cancel and not tier_id:
        return _ok(rid, {"ok": False, "error": "invalid_request", "message": "subscription_type_id or cancel is required"})
    try:
        result = put_subscription_pending_change(subscription_type_id=tier_id, cancel=cancel)
        return _ok(rid, {"ok": True, "message": result.get("message"), "payload": result})
    except BillingError as exc:
        return _ok(rid, _serialize_billing_error(exc))
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc)})


@method("subscription.resume")
def _(rid, params: dict) -> dict:
    """DELETE /api/billing/subscription/pending-change → {ok, message} or typed error.

    Clears a scheduled downgrade or cancellation (resume / undo). Chargeless, but it
    re-enables recurring spend → requires billing:manage and honors the kill-switch.
    """
    from hermes_cli.nous_billing import BillingError, delete_subscription_pending_change

    try:
        result = delete_subscription_pending_change()
        return _ok(rid, {"ok": True, "message": result.get("message"), "payload": result})
    except BillingError as exc:
        return _ok(rid, _serialize_billing_error(exc))
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc)})


@method("subscription.upgrade")
def _(rid, params: dict) -> dict:
    """POST /api/billing/subscription/upgrade → {ok, status, ...} or typed error.

    params: {subscription_type_id: str, idempotency_key?: str}. The single money
    route: prorate + charge the card on the subscription + flip the plan. SCA /
    decline come back as status requires_action / payment_failed with a recovery_url
    to finish in the portal. The idempotency key is minted if absent and echoed so
    the TUI reuses it on retry of the SAME upgrade. Requires billing:manage.
    """
    from agent.billing_view import new_idempotency_key
    from hermes_cli.nous_billing import BillingError, post_subscription_upgrade

    tier_id = params.get("subscription_type_id")
    if not tier_id:
        return _ok(rid, {"ok": False, "error": "invalid_request", "message": "subscription_type_id is required"})
    key = params.get("idempotency_key") or new_idempotency_key()
    try:
        result = post_subscription_upgrade(subscription_type_id=tier_id, idempotency_key=key)
        return _ok(
            rid,
            {
                "ok": True,
                "status": result.get("status"),
                "target_tier_name": result.get("targetTierName"),
                "recovery_url": result.get("recoveryUrl"),
                "reason": result.get("reason"),
                "idempotency_key": key,
            },
        )
    except BillingError as exc:
        env = _serialize_billing_error(exc)
        env["idempotency_key"] = key  # so the TUI can reuse on retry
        return _ok(rid, env)
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc), "idempotency_key": key})



@method("billing.charge")
def _(rid, params: dict) -> dict:
    """POST /api/billing/charge → {ok, chargeId} or a typed error envelope.

    params: {amount_usd: str|number, idempotency_key?: str}. If no key is
    supplied, the server-side core mints a fresh one and returns it so the TUI can
    reuse it on retry of the SAME purchase.
    """
    from hermes_cli.nous_billing import BillingError, post_charge
    from agent.billing_view import new_idempotency_key

    amount = params.get("amount_usd")
    if amount is None:
        return _ok(rid, {"ok": False, "error": "invalid_request", "message": "amount_usd is required"})
    key = params.get("idempotency_key") or new_idempotency_key()
    try:
        result = post_charge(amount_usd=amount, idempotency_key=key)
        return _ok(rid, {"ok": True, "charge_id": result.get("chargeId"), "idempotency_key": key})
    except BillingError as exc:
        env = _serialize_billing_error(exc)
        env["idempotency_key"] = key  # so the TUI can reuse on retry
        return _ok(rid, env)
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc), "idempotency_key": key})


@method("billing.charge_status")
def _(rid, params: dict) -> dict:
    """GET /api/billing/charge/{id} → {ok, status, ...} or typed error.

    The poll. Caller drives the 2s/5-min cadence; this is a single status read.
    """
    from hermes_cli.nous_billing import BillingError, get_charge_status

    charge_id = params.get("charge_id")
    if not charge_id:
        return _ok(rid, {"ok": False, "error": "invalid_charge_id", "message": "charge_id is required"})
    try:
        result = get_charge_status(charge_id)
        return _ok(
            rid,
            {
                "ok": True,
                "status": result.get("status"),
                "amount_usd": result.get("amountUsd"),
                "settled_at": result.get("settledAt"),
                "reason": result.get("reason"),
            },
        )
    except BillingError as exc:
        return _ok(rid, _serialize_billing_error(exc))
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc)})


@method("billing.auto_reload")
def _(rid, params: dict) -> dict:
    """PATCH /api/billing/auto-top-up → {ok:true} or typed error (Screen 2).

    params: {enabled: bool, threshold: number, top_up_amount: number}.
    """
    from hermes_cli.nous_billing import BillingError, patch_auto_top_up

    try:
        enabled = bool(params.get("enabled"))
        threshold = params.get("threshold")
        top_up_amount = params.get("top_up_amount")
        if threshold is None or top_up_amount is None:
            return _ok(rid, {"ok": False, "error": "invalid_request", "message": "threshold and top_up_amount are required"})
        patch_auto_top_up(enabled=enabled, threshold=threshold, top_up_amount=top_up_amount)
        return _ok(rid, {"ok": True})
    except BillingError as exc:
        return _ok(rid, _serialize_billing_error(exc))
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc)})


@method("billing.step_up")
def _(rid, params: dict) -> dict:
    """Run the lazy billing:manage step-up device flow → {ok, granted}.

    Triggered by the TUI after a billing call returns error=insufficient_scope.
    Returns granted:false when the server silently downscopes (non-admin / unticked).

    Runs on the thread pool (in _LONG_HANDLERS): the device flow blocks for the
    whole device-code lifetime (minutes), so it must not stall the main stdin loop.
    The verification URL/code reach the TUI via an out-of-band ``billing.step_up.
    verification`` event (a plain print would be dropped by the JSON-RPC stdout
    pipe), and the browser is opened TUI-side via openExternalUrl — never with the
    gateway's headless webbrowser.open (hence open_browser=False).
    """
    sid = params.get("session_id") or ""
    try:
        from hermes_cli.auth import step_up_nous_billing_scope
        from hermes_cli.nous_billing import BillingError

        def _on_verification(url: str, code: str) -> None:
            _emit(
                "billing.step_up.verification",
                sid,
                {"verification_url": url, "user_code": code},
            )

        granted = step_up_nous_billing_scope(
            open_browser=False, on_verification=_on_verification
        )
        return _ok(rid, {"ok": True, "granted": bool(granted)})
    except BillingError as exc:
        # Route typed billing errors (e.g. session_revoked when the token expires
        # mid-device-flow) through the shared spine like the other write handlers,
        # so the TUI maps them to the right copy instead of a generic failure.
        env = _serialize_billing_error(exc)
        env["granted"] = False
        return _ok(rid, env)
    except Exception as exc:
        return _ok(rid, {"ok": False, "error": "error", "message": str(exc), "granted": False})
