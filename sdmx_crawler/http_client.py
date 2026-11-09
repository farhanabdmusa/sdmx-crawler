"""Klien HTTP dengan timeout aman, retry + exponential backoff (jitter),
penghormatan header Retry-After, dan throttling antar request."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from .config import Config

log = logging.getLogger(__name__)
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpFetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None,
                 attempts: int = 1, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = retryable


@dataclass
class FetchResult:
    url: str
    status_code: int
    content: bytes
    content_type: str
    attempts: int
    headers: dict = field(default_factory=dict)


class SdmxHttpClient:
    def __init__(self, cfg: Config, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=cfg.connect_timeout, read=cfg.read_timeout,
                                  write=30.0, pool=30.0),
            headers={"User-Agent": cfg.user_agent},
            follow_redirects=True,
            transport=transport,
        )
        self._last_request = 0.0

    def __enter__(self) -> "SdmxHttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        wait = self.cfg.request_delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        delay = min(self.cfg.backoff_max, self.cfg.backoff_base * 2 ** (attempt - 1))
        delay += random.uniform(0, delay * 0.25)
        return max(delay, retry_after or 0.0)

    def get(self, url: str, accept: str) -> FetchResult:
        last_error = "unknown error"
        last_status: int | None = None
        total = self.cfg.max_retries + 1
        for attempt in range(1, total + 1):
            retry_after = None
            self._throttle()
            try:
                resp = self._client.get(url, headers={"Accept": accept})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_status, last_error = None, f"{type(exc).__name__}: {exc}"
            except httpx.HTTPError as exc:  # mis. URL tidak valid -> tidak perlu retry
                raise HttpFetchError(f"{type(exc).__name__}: {exc}", attempts=attempt) from exc
            else:
                if resp.status_code < 400:
                    return FetchResult(
                        url=str(resp.url), status_code=resp.status_code, content=resp.content,
                        content_type=resp.headers.get("content-type", ""), attempts=attempt,
                        headers={k: v for k, v in resp.headers.items()
                                 if k.lower() in {"content-type", "etag", "last-modified", "date"}})
                last_status = resp.status_code
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]!r}"
                if resp.status_code not in RETRYABLE_STATUS:
                    raise HttpFetchError(last_error, resp.status_code, attempt, retryable=False)
                ra = resp.headers.get("retry-after", "")
                retry_after = float(ra) if ra.replace(".", "", 1).isdigit() else None

            if attempt < total:
                delay = self._backoff(attempt, retry_after)
                log.warning("Percobaan %d/%d gagal (%s). Retry dalam %.1fs: %s",
                            attempt, total, last_error[:120], delay, url)
                time.sleep(delay)
        raise HttpFetchError(f"Gagal setelah {total} percobaan: {last_error}",
                             last_status, total, retryable=True)
