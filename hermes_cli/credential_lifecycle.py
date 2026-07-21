"""Atomic provider-credential lifecycle across every persisted mirror.

Provider keys may be present in ``.env``, env-seeded credential-pool rows,
``config.yaml`` endpoint mirrors, and the model inventory cache.  UI and CLI
surfaces must mutate these stores as one logical credential rather than leave
stale higher-precedence or re-seedable copies behind.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "purge_env_credential_references",
    "remove_provider_env_credential",
    "save_provider_env_credential",
]


def _providers_for_env_var(env_var: str) -> list[str]:
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
    except Exception:
        return []
    providers: list[str] = []
    for provider_id, config in PROVIDER_REGISTRY.items():
        try:
            if env_var in (config.api_key_env_vars or ()):
                providers.append(provider_id)
        except Exception:
            continue
    return providers


def _prune_env_pool_entries(env_var: str) -> list[str]:
    """Remove only pool rows whose source is exactly ``env:<VAR>``."""
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store

    source = f"env:{env_var}"
    pruned: list[str] = []
    with _auth_store_lock():
        auth_store = _load_auth_store()
        pool = auth_store.get("credential_pool")
        if not isinstance(pool, dict):
            return pruned
        changed = False
        for provider in list(pool):
            entries = pool.get(provider)
            if not isinstance(entries, list):
                continue
            retained = [
                entry
                for entry in entries
                if not (
                    isinstance(entry, dict)
                    and entry.get("source") == source
                )
            ]
            if len(retained) == len(entries):
                continue
            changed = True
            pruned.append(str(provider))
            if retained:
                pool[provider] = retained
            else:
                pool.pop(provider, None)
        if changed:
            _save_auth_store(auth_store)
    return pruned


def _scrub_config_yaml_mirrors(
    old_value: str,
    new_value: str | None,
) -> list[str]:
    """Value-match and reconcile endpoint-key mirrors in raw user config."""
    if not old_value:
        return []
    from hermes_cli.config import atomic_config_write, get_config_path
    from utils import fast_safe_load

    config_path = get_config_path()
    if not config_path.exists():
        return []
    try:
        with open(config_path, encoding="utf-8") as stream:
            user_config = fast_safe_load(stream) or {}
    except Exception:
        return []
    if not isinstance(user_config, dict):
        return []

    touched: list[str] = []

    def reconcile(section: Any, key_path: str) -> None:
        if not isinstance(section, dict):
            return
        for field in ("api_key", "api"):
            if section.get(field) != old_value:
                continue
            if new_value:
                section[field] = new_value
            else:
                section.pop(field, None)
            touched.append(f"{key_path}.{field}")

    reconcile(user_config.get("model"), "model")
    auxiliary = user_config.get("auxiliary")
    if isinstance(auxiliary, dict):
        for task, task_config in auxiliary.items():
            reconcile(task_config, f"auxiliary.{task}")
    custom = user_config.get("custom_providers")
    if isinstance(custom, list):
        for index, provider_config in enumerate(custom):
            reconcile(provider_config, f"custom_providers.{index}")
    elif isinstance(custom, dict):
        for name, provider_config in custom.items():
            reconcile(provider_config, f"custom_providers.{name}")

    if touched:
        atomic_config_write(config_path, user_config, sort_keys=False)
    return touched


def purge_env_credential_references(
    env_var: str,
    *,
    clear_models_cache: bool = True,
) -> dict[str, Any]:
    """Prune re-seedable pool/cache references without touching OAuth rows."""
    pruned = _prune_env_pool_entries(env_var)
    providers = sorted(set(pruned) | set(_providers_for_env_var(env_var)))
    try:
        from hermes_cli.auth import suppress_credential_source

        for provider in providers:
            suppress_credential_source(provider, f"env:{env_var}")
    except Exception:
        pass
    if clear_models_cache:
        try:
            from hermes_cli.models import clear_provider_models_cache

            for provider in providers:
                clear_provider_models_cache(provider)
        except Exception:
            pass
    return {"pool_pruned": pruned, "providers": providers}


def save_provider_env_credential(env_var: str, value: str) -> dict[str, Any]:
    """Save or rotate a provider key and reconcile every known mirror."""
    from hermes_cli.config import load_env, save_env_value

    old_value = load_env().get(env_var)
    save_env_value(env_var, value)
    config_updates: list[str] = []
    if value and old_value and old_value != value:
        config_updates = _scrub_config_yaml_mirrors(old_value, value)
    try:
        from hermes_cli.auth import unsuppress_credential_source

        for provider in _providers_for_env_var(env_var):
            unsuppress_credential_source(provider, f"env:{env_var}")
    except Exception:
        pass
    return {
        "ok": True,
        "key": env_var,
        "config_updates": config_updates,
    }


def remove_provider_env_credential(env_var: str) -> dict[str, Any]:
    """Remove a provider key from env, pool, config mirrors, and cache."""
    from hermes_cli.config import load_env, remove_env_value

    old_value = load_env().get(env_var)
    removed_from_env = remove_env_value(env_var)
    references = purge_env_credential_references(env_var)
    config_scrubbed = (
        _scrub_config_yaml_mirrors(old_value, None)
        if old_value
        else []
    )
    return {
        "ok": True,
        "key": env_var,
        "removed": removed_from_env,
        "pool_pruned": references["pool_pruned"],
        "providers": references["providers"],
        "config_scrubbed": config_scrubbed,
        "found": bool(
            removed_from_env
            or references["pool_pruned"]
            or config_scrubbed
        ),
    }
