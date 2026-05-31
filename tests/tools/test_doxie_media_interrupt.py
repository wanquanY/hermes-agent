import threading

import tools.doxie_media_tools as doxie_media_tools


def test_media_poll_returns_on_interrupt(monkeypatch):
    poll_started = threading.Event()
    result_holder = {}

    def fake_get_json(url, *, token, timeout):
        poll_started.set()
        return {"status": "running", "task_id": "task-1"}

    monkeypatch.setattr(doxie_media_tools, "_get_json", fake_get_json)
    monkeypatch.setattr(doxie_media_tools, "_env_int", lambda name, default: 5 if name == "DOXIE_MEDIA_PROXY_POLL_INTERVAL" else default)

    def run_poll():
        try:
            doxie_media_tools._poll_proxy_task("https://doxie.example/media", "task-1", token="runtime-token", timeout=30)
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
    assert result_holder["error"] == "Doxie media proxy request interrupted"


def test_media_proxy_result_returns_on_interrupt_during_start_request(monkeypatch):
    monkeypatch.setenv("DOXIE_IMAGE_GENERATE_PROXY_URL", "https://doxie.example/media/generate")
    monkeypatch.setenv("DOXIE_LLM_RUNTIME_TOKEN", "runtime-token")
    started = threading.Event()
    release = threading.Event()
    result_holder = {}

    def fake_post_json(url, payload, *, token, timeout):
        started.set()
        release.wait(timeout=5)
        return {"status": "completed", "image_url": "https://cdn.example/image.png"}

    monkeypatch.setattr(doxie_media_tools, "_post_json", fake_post_json)

    def run_tool():
        result_holder["value"] = doxie_media_tools.doxie_image_generate({"prompt": "test"})

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
