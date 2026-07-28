from hermes_gateway.response_normalization import normalize_empty_agent_response


def test_runtime_auth_failure_response_is_user_safe_when_final_response_contains_debug():
    response = normalize_empty_agent_response(
        {"failed": True},
        "⚠️ Non-retryable error (HTTP 401) — trying fallback...\n"
        "❌ Runtime token has expired or was revoked",
    )

    assert response == "⚠️ Dovie runtime 登录凭证已过期，正在刷新本地运行时。请稍后再试一次。"
    assert "HTTP 401" not in response


def test_runtime_auth_failure_response_is_user_safe_when_error_only():
    response = normalize_empty_agent_response(
        {
            "failed": True,
            "error": {
                "status": 401,
                "data": {"error_code": "RUNTIME_TOKEN_ERROR"},
            },
        },
        "",
    )

    assert response == "⚠️ Dovie runtime 登录凭证已过期，正在刷新本地运行时。请稍后再试一次。"
