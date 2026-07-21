from hermes_cli import model_display
from hermes_cli.model_switch import format_model_for_display


def test_opaque_resource_id_is_shortened_for_display_only():
    resource_id = (
        "ri.language-model-service..language-model."
        "anthropic-claude-4-7-opus"
    )

    assert format_model_for_display(resource_id) == "anthropic-claude-4-7-opus"
    assert format_model_for_display("meta-llama/Llama-3.3-70B-Instruct") == (
        "meta-llama/Llama-3.3-70B-Instruct"
    )
    assert format_model_for_display("ri.language-model-service..language-model.")


def test_display_model_name_prefers_shortest_configured_alias(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "model_aliases": {
                "palantir-claude": {"model": "opaque-model"},
                "opus": {"model": "opaque-model"},
            },
            "model": {"aliases": {"op": "provider/opaque-model"}},
        },
    )
    model_display.reset_model_display_cache()

    assert model_display.display_model_name("opaque-model") == "op"


def test_display_model_name_falls_back_to_leaf_and_opaque_format(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    model_display.reset_model_display_cache()

    assert model_display.display_model_name("vendor/plain-model") == "plain-model"
    assert model_display.display_model_name(
        "ri.language-model-service..language-model.claude"
    ) == "claude"
