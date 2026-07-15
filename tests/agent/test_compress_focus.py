"""Tests for focus_topic flowing through the compressor.

Verifies that _generate_summary and compress accept and use the focus_topic
parameter correctly.  Inspired by Claude Code's /compact <focus>.
"""

from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor


def _make_compressor():
    """Create a ContextCompressor with minimal state for testing."""
    compressor = ContextCompressor.__new__(ContextCompressor)
    compressor.protect_first_n = 2
    compressor.protect_last_n = 5
    compressor.tail_token_budget = 20000
    compressor.context_length = 200000
    compressor.threshold_percent = 0.80
    compressor.threshold_tokens = 160000
    compressor.max_summary_tokens = 10000
    compressor.quiet_mode = True
    compressor.compression_count = 0
    compressor.last_prompt_tokens = 0
    compressor._previous_summary = None
    compressor._summary_failure_cooldown_until = 0.0
    compressor.summary_model = None
    compressor.model = "test-model"
    compressor.provider = "test"
    compressor.base_url = "http://localhost"
    compressor.api_key = "test-key"
    compressor.api_mode = "chat_completions"
    return compressor


def test_focus_topic_injected_into_summary_prompt():
    """When focus_topic is provided, the LLM prompt includes focus guidance."""
    compressor = _make_compressor()
    turns = [
        {"role": "user", "content": "Tell me about the database schema"},
        {"role": "assistant", "content": "The schema has tables: users, orders, products."},
    ]

    captured_prompt = {}

    def mock_call_llm(**kwargs):
        captured_prompt["messages"] = kwargs["messages"]
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "## Goal\nUnderstand DB schema."
        return resp

    with patch("agent.context_compressor.call_llm", mock_call_llm):
        result = compressor._generate_summary(turns, focus_topic="database schema")

    assert result is not None
    prompt_text = captured_prompt["messages"][0]["content"]
    assert 'FOCUS TOPIC: "database schema"' in prompt_text
    assert "PRIORITISE" in prompt_text
    assert "60-70%" in prompt_text


def test_no_focus_topic_no_injection():
    """Without focus_topic, the prompt doesn't contain focus guidance."""
    compressor = _make_compressor()
    turns = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi"},
    ]

    captured_prompt = {}

    def mock_call_llm(**kwargs):
        captured_prompt["messages"] = kwargs["messages"]
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "## Goal\nGreeting."
        return resp

    with patch("agent.context_compressor.call_llm", mock_call_llm):
        result = compressor._generate_summary(turns)

    prompt_text = captured_prompt["messages"][0]["content"]
    assert "FOCUS TOPIC" not in prompt_text


def test_memory_provider_checkpoint_is_injected_as_runtime_context():
    """Pre-compression memory facts must survive without becoming user speech."""
    compressor = _make_compressor()
    turns = [
        {"role": "user", "content": "Continue the team task"},
        {"role": "assistant", "content": "Working on it."},
    ]
    captured_prompt = {}

    def mock_call_llm(**kwargs):
        captured_prompt["messages"] = kwargs["messages"]
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "## Goal\nContinue the team task."
        return resp

    checkpoint = "participant=member-7; decision=use canonical transcript"
    with patch("agent.context_compressor.call_llm", mock_call_llm):
        result = compressor._generate_summary(
            turns,
            preservation_context=checkpoint,
        )

    assert result is not None
    prompt_text = captured_prompt["messages"][0]["content"]
    assert "PRE-COMPRESSION MEMORY PROVIDER CHECKPOINT" in prompt_text
    assert checkpoint in prompt_text
    assert "not a new user request" in prompt_text
    assert "must not be quoted as user-authored speech" in prompt_text


def test_compress_passes_focus_to_generate_summary():
    """compress() passes focus_topic through to _generate_summary."""
    compressor = _make_compressor()

    # Track what _generate_summary receives
    received_kwargs = {}
    original_generate = compressor._generate_summary

    def tracking_generate(turns, **kwargs):
        received_kwargs.update(kwargs)
        return "## Goal\nTest."

    compressor._generate_summary = tracking_generate

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply2"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "reply3"},
        {"role": "user", "content": "fourth"},
        {"role": "assistant", "content": "reply4"},
    ]

    compressor.compress(messages, current_tokens=100000, focus_topic="authentication flow")

    assert received_kwargs.get("focus_topic") == "authentication flow"


def test_compress_passes_memory_provider_checkpoint_to_generate_summary():
    compressor = _make_compressor()
    received_kwargs = {}

    def tracking_generate(turns, **kwargs):
        received_kwargs.update(kwargs)
        return "## Goal\nTest."

    compressor._generate_summary = tracking_generate
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply2"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "reply3"},
        {"role": "user", "content": "fourth"},
        {"role": "assistant", "content": "reply4"},
    ]

    compressor.compress(
        messages,
        current_tokens=100000,
        preservation_context="participant checkpoint",
    )

    assert received_kwargs.get("preservation_context") == "participant checkpoint"


def test_compress_derives_recent_focus_by_default():
    """Automatic compression derives a stable focus from recent user turns."""
    compressor = _make_compressor()

    received_kwargs = {}

    def tracking_generate(turns, **kwargs):
        received_kwargs.update(kwargs)
        return "## Goal\nTest."

    compressor._generate_summary = tracking_generate

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply2"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": "reply3"},
        {"role": "user", "content": "fourth"},
        {"role": "assistant", "content": "reply4"},
    ]

    compressor.compress(messages, current_tokens=100000)

    assert received_kwargs.get("focus_topic") == (
        "Recent user focus:\n- second\n- third\n- fourth"
    )
