"""Rate-limited EDGAR HTTP client.

SEC requires a User-Agent header with a real contact (Name email). Without
it, requests are blocked. SEC asks for <= 10 req/sec; we throttle at 8.
"""
from __future__ import annotations

import os
import threading
import time

import requests

DEFAULT_UA = "Aarnav Chitari aarnav@utexas.edu"
USER_AGENT = os.environ.get("SEC_USER_AGENT", DEFAULT_UA)

# Conservative throttle: 8 req/sec leaves headroom under the 10/s cap.
_MIN_INTERVAL = 1.0 / 8.0
_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def get(url: str, *, accept: str = "*/*", timeout: float = 30.0) -> requests.Response:
    _throttle()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Encoding": "gzip, deflate",
        "Host": _host(url),
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


def _host(url: str) -> str:
    # crude but enough for sec.gov / data.sec.gov / www.sec.gov
    return url.split("://", 1)[1].split("/", 1)[0]
