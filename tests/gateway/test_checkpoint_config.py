"""Gateway filesystem-checkpoint configuration contracts."""

from hermes_cli.config import DEFAULT_CONFIG
from hermes_gateway.checkpoint_config import checkpoint_agent_kwargs


def test_checkpoint_agent_kwargs_maps_full_config():
    kwargs = checkpoint_agent_kwargs(
        {
            "checkpoints": {
                "enabled": True,
                "max_snapshots": 11,
                "max_total_size_mb": 345,
                "max_file_size_mb": 6,
            }
        }
    )
    assert kwargs == {
        "checkpoints_enabled": True,
        "checkpoint_max_snapshots": 11,
        "checkpoint_max_total_size_mb": 345,
        "checkpoint_max_file_size_mb": 6,
    }


def test_checkpoint_agent_kwargs_supports_legacy_boolean_config():
    kwargs = checkpoint_agent_kwargs({"checkpoints": True})
    defaults = DEFAULT_CONFIG["checkpoints"]
    assert kwargs["checkpoints_enabled"] is True
    assert kwargs["checkpoint_max_snapshots"] == defaults["max_snapshots"]
    assert kwargs["checkpoint_max_total_size_mb"] == defaults["max_total_size_mb"]
    assert kwargs["checkpoint_max_file_size_mb"] == defaults["max_file_size_mb"]


def test_checkpoint_agent_kwargs_rejects_invalid_limits():
    kwargs = checkpoint_agent_kwargs(
        {
            "checkpoints": {
                "max_snapshots": -1,
                "max_total_size_mb": "invalid",
                "max_file_size_mb": None,
            }
        }
    )
    defaults = DEFAULT_CONFIG["checkpoints"]
    assert kwargs["checkpoint_max_snapshots"] == defaults["max_snapshots"]
    assert kwargs["checkpoint_max_total_size_mb"] == defaults["max_total_size_mb"]
    assert kwargs["checkpoint_max_file_size_mb"] == defaults["max_file_size_mb"]
