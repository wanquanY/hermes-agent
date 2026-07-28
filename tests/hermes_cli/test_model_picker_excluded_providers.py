from __future__ import annotations

from unittest.mock import patch

import yaml


def _write_config(home, *, excluded: list[str] | None = None) -> None:
    cfg: dict = {"model": "old-model", "custom_providers": []}
    if excluded is not None:
        cfg["model_catalog"] = {"excluded_providers": excluded}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (home / ".env").write_text("", encoding="utf-8")


def _capture_provider_labels(home, monkeypatch) -> list[str]:
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "HERMES_MODEL",
        "LLM_MODEL",
        "HERMES_INFERENCE_PROVIDER",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    from hermes_cli import config as config_mod

    config_mod._cached_config = None
    from hermes_cli.main import select_provider_and_model

    captured: list[str] = []

    def capture_and_cancel(labels, **_kwargs):
        captured.extend(labels)
        return None

    with patch(
        "hermes_cli.main._prompt_provider_choice", side_effect=capture_and_cancel
    ), patch("builtins.print"):
        select_provider_and_model()
    return captured


def test_cli_picker_hides_excluded_provider(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    _write_config(home, excluded=["openrouter"])

    labels = _capture_provider_labels(home, monkeypatch)

    assert labels
    assert not any("OpenRouter" in label for label in labels)


def test_cli_picker_hides_canonical_provider_by_alias(tmp_path, monkeypatch):
    from hermes_cli.models import (
        CANONICAL_PROVIDERS,
        _PROVIDER_ALIASES,
        _PROVIDER_LABELS,
    )

    canonical_slugs = {provider.slug for provider in CANONICAL_PROVIDERS}
    alias, canonical = next(
        (alias, canonical)
        for alias, canonical in _PROVIDER_ALIASES.items()
        if canonical in canonical_slugs
    )
    label = _PROVIDER_LABELS.get(canonical, canonical)
    home = tmp_path / "hermes"
    home.mkdir()
    _write_config(home, excluded=[alias])

    labels = _capture_provider_labels(home, monkeypatch)

    assert not any(label in candidate for candidate in labels)


def test_cli_picker_empty_exclusion_is_noop(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    _write_config(home, excluded=[])
    with_empty = _capture_provider_labels(home, monkeypatch)
    _write_config(home)
    baseline = _capture_provider_labels(home, monkeypatch)

    assert with_empty == baseline
