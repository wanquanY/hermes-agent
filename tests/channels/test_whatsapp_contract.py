from __future__ import annotations

from channels.platforms.whatsapp import WhatsAppAdapter
from channels.platforms.whatsapp import check_whatsapp_requirements


def test_channels_whatsapp_exports_adapter_contract() -> None:
    assert WhatsAppAdapter.__name__ == "WhatsAppAdapter"
    assert WhatsAppAdapter.MAX_MESSAGE_LENGTH == 4096
    assert isinstance(check_whatsapp_requirements(), bool)


def test_channels_whatsapp_formatting_contract() -> None:
    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)

    assert adapter.format_message("**hello**") == "*hello*"
