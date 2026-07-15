"""Yuanbao platform constants."""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

DEFAULT_WS_GATEWAY_URL = "wss://bot-wss.yuanbao.tencent.com/wss/connection"
DEFAULT_API_DOMAIN = "https://bot.yuanbao.tencent.com"

HEARTBEAT_INTERVAL_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 15.0
AUTH_TIMEOUT_SECONDS = 10.0
MAX_RECONNECT_ATTEMPTS = 100
DEFAULT_SEND_TIMEOUT = 30.0  # WS biz request timeout

# Close codes that indicate permanent errors — do NOT reconnect.
NO_RECONNECT_CLOSE_CODES = {4012, 4013, 4014, 4018, 4019, 4021}

# Heartbeat timeout threshold — N consecutive missed pongs trigger reconnect.
HEARTBEAT_TIMEOUT_THRESHOLD = 2

# Auth error code classification
AUTH_FAILED_CODES = {4001, 4002, 4003}      # permanent auth failure, re-sign token
AUTH_RETRYABLE_CODES = {4010, 4011, 4099}   # transient, can retry with same token

# Reply Heartbeat configuration
REPLY_HEARTBEAT_INTERVAL_S = 2.0   # Send RUNNING every 2 seconds
REPLY_HEARTBEAT_TIMEOUT_S = 30.0   # Auto-stop after 30 seconds of inactivity

# Reply-to reference configuration
REPLY_REF_TTL_S = 300.0            # Reference dedup TTL (5 minutes)

# Slow-response hint: push a waiting message when agent produces no data for this duration (seconds)
SLOW_RESPONSE_TIMEOUT_S = 120.0
SLOW_RESPONSE_MESSAGE = "任务有点复杂，正在努力处理中，请耐心等待..."

# Regex matching Yuanbao resource reference anchors in transcript text:
#   [image|ybres:abc123]  [file:report.pdf|ybres:xyz789]  [voice|ybres:...]
_YB_RES_REF_RE = re.compile(
    r"\[(image|voice|video|file(?::[^|\]]*)?)\|ybres:([A-Za-z0-9_\-]+)\]"
)

# Media kinds that can be resolved and injected into the model context
_RESOLVABLE_MEDIA_KINDS = frozenset({"image", "file"})

# Strip page indicators like (1/3) appended by BasePlatformAdapter
_INDICATOR_RE = re.compile(r'\s*\(\d+/\d+\)$')

# Observed-media backfill: how many recent transcript messages to scan
OBSERVED_MEDIA_BACKFILL_LOOKBACK = 50
# Max number of resource references to resolve per inbound turn
OBSERVED_MEDIA_BACKFILL_MAX_RESOLVE_PER_TURN = 12
