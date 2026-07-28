from __future__ import annotations

import io
import sys
import threading

from agent.thread_scoped_output import thread_scoped_silence


def test_thread_scoped_silence_does_not_swallow_other_threads():
    stream = io.StringIO()
    original = sys.stdout
    sys.stdout = stream
    entered = threading.Event()
    loud_done = threading.Event()

    def quiet_worker():
        with thread_scoped_silence():
            print("quiet")
            entered.set()
            loud_done.wait(timeout=2)

    def loud_worker():
        entered.wait(timeout=2)
        print("loud")
        loud_done.set()

    try:
        quiet = threading.Thread(target=quiet_worker)
        loud = threading.Thread(target=loud_worker)
        quiet.start()
        loud.start()
        quiet.join(timeout=3)
        loud.join(timeout=3)
    finally:
        sys.stdout = original

    assert "quiet" not in stream.getvalue()
    assert "loud" in stream.getvalue()


def test_nested_silence_restores_current_thread():
    stream = io.StringIO()
    original = sys.stdout
    sys.stdout = stream
    try:
        with thread_scoped_silence():
            with thread_scoped_silence():
                print("inner")
            print("outer")
        print("restored")
    finally:
        sys.stdout = original

    assert "inner" not in stream.getvalue()
    assert "outer" not in stream.getvalue()
    assert "restored" in stream.getvalue()
