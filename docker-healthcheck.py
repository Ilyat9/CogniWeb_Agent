#!/usr/bin/env python3
"""Docker HEALTHCHECK helper.

MODE=api: probe the real /health endpoint (503 during SIGTERM drain ->
container becomes unhealthy).

MODE=cli (default): previously exited 0 unconditionally, so a hung batch
process (e.g. an undetected browser/step loop) always looked healthy. Now
the agent touches a heartbeat file once per reasoning step (see
Settings.heartbeat_file / AgentOrchestrator._touch_heartbeat); when that
file exists but has not been updated for HEARTBEAT_STALE_SECONDS, the
container is reported unhealthy. No heartbeat file yet (waiting on the
interactive task prompt, or an older build) keeps exit 0 - absence of the
signal is not evidence of a hang.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

if os.environ.get("MODE", "cli") != "api":
    heartbeat = Path(os.environ.get("HEARTBEAT_FILE", "./logs/heartbeat"))
    if not heartbeat.is_file():
        sys.exit(0)
    try:
        stale_after = float(os.environ.get("HEARTBEAT_STALE_SECONDS", "600"))
        age = (datetime.now() - datetime.fromisoformat(heartbeat.read_text())).total_seconds()
    except (ValueError, OSError):
        # Unreadable/corrupt heartbeat: cannot prove liveness, but also
        # cannot prove a hang - stay conservative and healthy.
        sys.exit(0)
    sys.exit(1 if age > stale_after else 0)

import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5)
    sys.exit(0)
except Exception:
    sys.exit(1)
