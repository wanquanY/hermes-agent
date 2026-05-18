from run_agent import AIAgent


def test_model_descriptor_vision_override_allows_image_tool_results():
    agent = object.__new__(AIAgent)
    agent.model_descriptor = {"id": "custom-vision-model", "vision_enabled": True}
    agent.provider = "custom"
    agent.model = "custom-vision-model"

    assert AIAgent._model_supports_vision(agent) is True


def test_model_descriptor_vision_override_can_disable_image_tool_results():
    agent = object.__new__(AIAgent)
    agent.model_descriptor = {"id": "known-model", "vision_enabled": False}
    agent.provider = "openai"
    agent.model = "gpt-5.5"

    assert AIAgent._model_supports_vision(agent) is False
