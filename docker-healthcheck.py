#!/usr/bin/env python3
"""Docker HEALTHCHECK helper.

MODE=api: probe the real /health endpoint (503 during SIGTERM drain ->
container becomes unhealthy).
MODE=cli (default): exit 0 immediately - the agent is a batch process,
not a long-lived service; see Dockerfile for the rationale.
"""

import os
import sys

if os.environ.get("MODE", "cli") != "api":
    sys.exit(0)

import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5)
    sys.exit(0)
except Exception:
    sys.exit(1)
