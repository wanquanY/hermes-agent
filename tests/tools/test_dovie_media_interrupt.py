import json
import threading

import tools.dovie_media_tools as dovie_media_tools


def test_image_generate_uses_backend_default_when_model_is_omitted(monkeypatch):
    captured = {}

    def fake_proxy_result(env_name, payload):
        captured["env_name"] = env_name
        captured["payload"] = payload
        return "{}"

    monkeypatch.setattr(dovie_media_tools, "_async_media_proxy_result", fake_proxy_result)

    result = dovie_media_tools.dovie_image_generate({"prompt": "test"})

    assert result == "{}"
    assert captured["env_name"] == "DOVIE_IMAGE_GENERATE_PROXY_URL"
    assert "model" not in captured["payload"]
    assert "aspect_ratio" not in captured["payload"]
    assert "generate_num" not in captured["payload"]
    assert "quality" not in captured["payload"]

    properties = dovie_media_tools.DOVIE_IMAGE_GENERATE_SCHEMA["parameters"]["properties"]
    assert "default" not in properties["aspect_ratio"]
    assert "default" not in properties["generate_num"]
    assert "1K" not in properties["quality"]["description"]
    assert "2K" not in properties["quality"]["description"]


def test_image_generate_forwards_explicit_model(monkeypatch):
    captured = {}

    def fake_proxy_result(_env_name, payload):
        captured["payload"] = payload
        return "{}"

    monkeypatch.setattr(dovie_media_tools, "_async_media_proxy_result", fake_proxy_result)

    dovie_media_tools.dovie_image_generate(
        {
            "prompt": "test",
            "model": "gpt-image-2",
            "aspect_ratio": "landscape",
            "generate_num": 2,
            "quality": "high",
        }
    )

    assert captured["payload"]["model"] == "gpt-image-2"
    assert captured["payload"]["aspect_ratio"] == "16:9"
    assert captured["payload"]["generate_num"] == 2
    assert captured["payload"]["quality"] == "high"


def test_image_generate_materializes_outputs_as_workspace_artifacts(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(dovie_media_tools, "_active_workspace_root", lambda: workspace)
    monkeypatch.setattr(
        dovie_media_tools,
        "_async_media_proxy_result",
        lambda *_args, **_kwargs: json.dumps(
            {
                "success": True,
                "status": "completed",
                "image_urls": [
                    "https://cdn.example.com/first.png",
                    "https://cdn.example.com/second.png",
                ],
            }
        ),
    )

    def fake_download(url, destination):
        destination.write_bytes(url.encode("utf-8"))
        return destination.stat().st_size, "image/png"

    monkeypatch.setattr(dovie_media_tools, "_download_generated_image", fake_download)

    result = json.loads(
        dovie_media_tools.dovie_image_generate(
            {
                "prompt": "test",
                "output_directory": "assets/deck",
                "file_stem": "hero",
            }
        )
    )

    expected_paths = [
        workspace / "assets" / "deck" / "hero-01.png",
        workspace / "assets" / "deck" / "hero-02.png",
    ]
    assert result["materialized"] is True
    assert result["local_paths"] == [str(path) for path in expected_paths]
    assert [artifact["source_url"] for artifact in result["artifacts"]] == [
        "https://cdn.example.com/first.png",
        "https://cdn.example.com/second.png",
    ]
    assert all(path.is_file() for path in expected_paths)


def test_image_generate_rejects_materialization_outside_workspace(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(dovie_media_tools, "_active_workspace_root", lambda: workspace)
    monkeypatch.setattr(
        dovie_media_tools,
        "_async_media_proxy_result",
        lambda *_args, **_kwargs: json.dumps(
            {
                "success": True,
                "status": "completed",
                "image_url": "https://cdn.example.com/image.png",
            }
        ),
    )

    result = dovie_media_tools.dovie_image_generate(
        {
            "prompt": "test",
            "output_directory": str(tmp_path / "outside"),
        }
    )

    assert "inside the active workspace" in result


def test_image_generate_uploads_local_workspace_reference_before_generation(
    monkeypatch,
    tmp_path,
):
    image_path = tmp_path / "reference.png"
    image_path.write_bytes(b"png")
    captured = {}
    monkeypatch.setenv(
        "DOVIE_MEDIA_ASSET_PROXY_URL",
        "https://dovie.example/media-assets/reference-image",
    )
    monkeypatch.setenv("DOVIE_LLM_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setattr(dovie_media_tools, "_active_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        dovie_media_tools,
        "_run_proxy_request_interruptibly",
        lambda fn, **_kwargs: fn(),
    )

    def fake_upload(url, path, *, token, timeout):
        captured["upload"] = (url, path, token, timeout)
        return {"url": "https://assets.example/reference.png"}

    def fake_generate(env_name, payload):
        captured["generate"] = (env_name, payload)
        return "{}"

    monkeypatch.setattr(dovie_media_tools, "_post_reference_image", fake_upload)
    monkeypatch.setattr(dovie_media_tools, "_async_media_proxy_result", fake_generate)

    assert dovie_media_tools.dovie_image_generate(
        {"prompt": "test", "reference_image_url": str(image_path)}
    ) == "{}"
    assert captured["upload"][1] == image_path
    assert captured["generate"][1]["reference_image_url"] == (
        "https://assets.example/reference.png"
    )
    assert captured["generate"][1]["generation_type"] == "i2i"


def test_local_reference_rejects_paths_outside_workspace_and_attachment_store(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    monkeypatch.setattr(dovie_media_tools, "_active_workspace_root", lambda: workspace)
    monkeypatch.delenv("DOVIE_ATTACHMENT_ROOT", raising=False)

    result = dovie_media_tools.dovie_image_generate(
        {"prompt": "test", "reference_image_url": str(outside)}
    )

    assert "active workspace or Dovie attachment store" in result


def test_attachment_blob_uses_durable_metadata_for_upload_name(monkeypatch, tmp_path):
    attachment_root = tmp_path / "attachments"
    attachment_dir = attachment_root / "asset"
    attachment_dir.mkdir(parents=True)
    blob = attachment_dir / "blob"
    blob.write_bytes(b"png")
    (attachment_dir / "meta.json").write_text(
        '{"fileName":"reference.png","mimeType":"image/png"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOVIE_ATTACHMENT_ROOT", str(attachment_root))

    assert dovie_media_tools._local_reference_metadata(blob) == (
        "reference.png",
        "image/png",
    )


def test_media_asset_url_and_attachment_root_derive_from_existing_runtime_env(
    monkeypatch,
    tmp_path,
):
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    monkeypatch.delenv("DOVIE_MEDIA_ASSET_PROXY_URL", raising=False)
    monkeypatch.delenv("DOVIE_ATTACHMENT_ROOT", raising=False)
    monkeypatch.setenv(
        "DOVIE_IMAGE_GENERATE_PROXY_URL",
        "https://api.example/api/v1/llm-proxy/v1/image-generate",
    )
    monkeypatch.setenv("DOVIE_RUNTIME_HOME", str(runtime_root))

    assert dovie_media_tools._media_asset_proxy_url() == (
        "https://api.example/api/v1/llm-proxy/v1/media-assets/reference-image"
    )
    assert dovie_media_tools._attachment_root() == tmp_path / "attachments"


def test_media_poll_returns_on_interrupt(monkeypatch):
    poll_started = threading.Event()
    result_holder = {}

    def fake_get_json(url, *, token, timeout):
        poll_started.set()
        return {"status": "running", "task_id": "task-1"}

    monkeypatch.setattr(dovie_media_tools, "_get_json", fake_get_json)
    monkeypatch.setattr(dovie_media_tools, "_env_int", lambda name, default: 5 if name == "DOVIE_MEDIA_PROXY_POLL_INTERVAL" else default)

    def run_poll():
        try:
            dovie_media_tools._poll_proxy_task("https://dovie.example/media", "task-1", token="runtime-token", timeout=30)
        except InterruptedError as exc:
            result_holder["error"] = str(exc)

    worker = threading.Thread(target=run_poll)
    worker.start()
    assert poll_started.wait(timeout=1)

    from tools.interrupt import set_interrupt

    set_interrupt(True, thread_id=worker.ident)
    worker.join(timeout=1)
    set_interrupt(False, thread_id=worker.ident)

    assert not worker.is_alive()
    assert result_holder["error"] == "Dovie media proxy request interrupted"


def test_media_proxy_result_returns_on_interrupt_during_start_request(monkeypatch):
    monkeypatch.setenv("DOVIE_IMAGE_GENERATE_PROXY_URL", "https://dovie.example/media/generate")
    monkeypatch.setenv("DOVIE_LLM_RUNTIME_TOKEN", "runtime-token")
    started = threading.Event()
    release = threading.Event()
    result_holder = {}

    def fake_post_json(url, payload, *, token, timeout):
        started.set()
        release.wait(timeout=5)
        return {"status": "completed", "image_url": "https://cdn.example/image.png"}

    monkeypatch.setattr(dovie_media_tools, "_post_json", fake_post_json)

    def run_tool():
        result_holder["value"] = dovie_media_tools.dovie_image_generate({"prompt": "test"})

    worker = threading.Thread(target=run_tool)
    worker.start()
    assert started.wait(timeout=1)

    from tools.interrupt import set_interrupt

    set_interrupt(True, thread_id=worker.ident)
    worker.join(timeout=1)
    release.set()
    set_interrupt(False, thread_id=worker.ident)

    assert not worker.is_alive()
    assert "interrupted" in result_holder["value"].lower()
