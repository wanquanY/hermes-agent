"""Cron provider routing must use the model the job will actually execute."""

from unittest.mock import MagicMock, patch

from cron.scheduler import run_job


def test_primary_resolution_receives_effective_job_model(tmp_path):
    captured = {}

    def resolve(**kwargs):
        captured.update(kwargs)
        return {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
        }

    fake_store = MagicMock()
    with (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("dotenv.load_dotenv"),
        patch("cron.scheduler.open_cli_session_store", return_value=fake_store),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            side_effect=resolve,
        ),
        patch("run_agent.AIAgent") as agent_class,
    ):
        agent_class.return_value.run_conversation.return_value = {
            "final_response": "ok"
        }
        success, *_ = run_job(
            {
                "id": "effective-model-job",
                "name": "effective model",
                "prompt": "hello",
                "model": "my-pinned-model",
                "provider": "openrouter",
            }
        )

    assert success is True
    assert captured["requested"] == "openrouter"
    assert captured["target_model"] == "my-pinned-model"
