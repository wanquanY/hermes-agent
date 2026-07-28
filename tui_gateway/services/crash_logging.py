from __future__ import annotations

import os
import sys
import threading
import time
import traceback


def install_panic_logger(hermes_home: str) -> str:
    crash_log = os.path.join(hermes_home, "logs", "tui_gateway_crash.log")

    def panic_hook(exc_type, exc_value, exc_tb):
        trace = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            os.makedirs(os.path.dirname(crash_log), exist_ok=True)
            with open(crash_log, "a", encoding="utf-8") as handle:
                handle.write(
                    f"\n=== unhandled exception · {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
                handle.write(trace)
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

    def thread_panic_hook(args):
        trace = "".join(
            traceback.format_exception(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
            )
        )
        try:
            os.makedirs(os.path.dirname(crash_log), exist_ok=True)
            with open(crash_log, "a", encoding="utf-8") as handle:
                handle.write(
                    f"\n=== thread exception · {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"· thread={args.thread.name} ===\n"
                )
                handle.write(trace)
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

    sys.excepthook = panic_hook
    threading.excepthook = thread_panic_hook
    return crash_log
