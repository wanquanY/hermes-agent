from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path


def install_panic_hooks(hermes_home: Path) -> None:
    crash_log = os.path.join(hermes_home, "logs", "tui_gateway_crash.log")

    def _panic_hook(exc_type, exc_value, exc_tb):
        trace = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            os.makedirs(os.path.dirname(crash_log), exist_ok=True)
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write(
                    f"\n=== unhandled exception · {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
                f.write(trace)
        except Exception:
            pass
        first = (
            str(exc_value).strip().splitlines()[0]
            if str(exc_value).strip()
            else exc_type.__name__
        )
        print(
            f"[gateway-crash] {exc_type.__name__}: {first}",
            file=sys.stderr,
            flush=True,
        )
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_panic_hook(args):
        trace = "".join(
            traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback
            )
        )
        try:
            os.makedirs(os.path.dirname(crash_log), exist_ok=True)
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write(
                    f"\n=== thread exception · {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"· thread={args.thread.name} ===\n"
                )
                f.write(trace)
        except Exception:
            pass
        first_line = (
            str(args.exc_value).strip().splitlines()[0]
            if str(args.exc_value).strip()
            else args.exc_type.__name__
        )
        print(
            f"[gateway-crash] thread {args.thread.name} raised {args.exc_type.__name__}: {first_line}",
            file=sys.stderr,
            flush=True,
        )

    sys.excepthook = _panic_hook
    threading.excepthook = _thread_panic_hook
