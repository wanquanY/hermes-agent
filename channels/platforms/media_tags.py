"""Canonical parser for model-emitted local MEDIA directives."""

from __future__ import annotations

import re

_DELIVERABLE_EXTENSION_PATTERN = (
    r"png|jpe?g|gif|webp|mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|flac|"
    r"epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|txt|csv|apk|ipa"
)

# Quoted paths may contain arbitrary characters except their own delimiter;
# downstream validation still requires an absolute, safe file. Unquoted paths
# must begin with an absolute-path marker and cannot consume quote/backtick
# delimiters, which also prevents one directive from swallowing the next.
MEDIA_TAG_CLEANUP_RE = re.compile(
    rf'''[`"']?MEDIA:\s*(?P<path>
        `[^`\n]+`
        |"[^"\n]+"
        |'[^'\n]+'
        |(?:~/|/|[A-Za-z]:[\\/])[^`"'\n]*?\.(?:{_DELIVERABLE_EXTENSION_PATTERN})
    )(?=[\s`"',;:)\]}}]|$)[`"']?''',
    re.VERBOSE | re.IGNORECASE,
)
