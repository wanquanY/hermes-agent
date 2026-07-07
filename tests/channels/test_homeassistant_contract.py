from __future__ import annotations

import os
from unittest.mock import patch

from channels.platforms.homeassistant import HomeAssistantAdapter
from channels.platforms.homeassistant import check_ha_requirements
from channels.config import PlatformConfig


def test_channels_homeassistant_exports_adapter_contract() -> None:
    assert HomeAssistantAdapter.__name__ == "HomeAssistantAdapter"
    assert HomeAssistantAdapter.MAX_MESSAGE_LENGTH == 4096
    assert isinstance(check_ha_requirements(), bool)


def test_channels_homeassistant_adapter_reads_environment_contract() -> None:
    env = {
        "HASS_TOKEN": "token",
        "HASS_URL": "http://ha.local:8123/",
    }
    with patch.dict(os.environ, env):
        adapter = HomeAssistantAdapter(PlatformConfig(enabled=True))

    assert adapter._hass_token == "token"
    assert adapter._hass_url == "http://ha.local:8123"


def test_channels_homeassistant_state_format_contract() -> None:
    old_state = {"state": "off"}
    new_state = {"state": "on", "attributes": {"friendly_name": "Desk Lamp"}}
    result = HomeAssistantAdapter._format_state_change(
        "light.desk_lamp", old_state, new_state
    )

    assert "Desk Lamp" in result
    assert "turned on" in result
