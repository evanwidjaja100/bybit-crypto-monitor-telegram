"""Bybit REST client.

Provides reliable, asynchronous access to the public market endpoints used
by the monitor. Implements bounded retries with exponential backoff and
jitter, pagination for Linear instruments, and robust error handling.

Every request is validated against HTTP and Bybit response contracts
before parsing.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Optional

import httpx

from app.bybit.models import Announcement, Instrument, Ticker
from app.bybit.normalizer import (
    parse_announcement,
    parse_instrument,
    parse_ticker,
)

logger = logging.getLogger("bybit_monitor.bybit.rest")

# HTTP status codes considered transient / retryable.
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
# Bybit error codes considered transient / retryable.
RETRYABLE_RETCODES = {10002, 10006, 33004, 33005}

_INSTRUMENTS_PATH = "/v5/market/instruments-info"
_TICKERS_PATH = "/v5/market/tickers"
_TIME_PATH = "/v5/market/time"
_ANNOUNCEMENTS_PATH = "/v5/announcements/index"


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------
class BybitError(Exception):
    """Base class for Bybit-related failures."""


class BybitRetryableError(BybitError):
    """A transient failure that may succeed after backoff."""


class BybitTimeoutError(BybitRetryableError):
    pass


class BybitConnectError(BybitRetryableError):
    pass


class BybitRateLimitError(BybitRetryableError):
    pass


class BybitServerError(BybitRetryableError):
    pass


class BybitAPIError(BybitError):
    """Bybit returned a non-zero retCode (data error)."""

    def __init__(self, ret_code: int, ret_msg: str) -> None:
        super().__init__(f"Bybit retCode={ret_code} retMsg={ret_msg!r}")
        self.ret_code = ret_code
        self.ret_msg = ret_msg


class BybitHTTPStatusError(BybitError):
    """HTTP error that is not worth retrying (4xx other than 408/425/429)."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------
class BybitRestClient:
    def __init__(
        self,
        base_url: str = "https://api.bybit.com",
        timeout: float = 10.0,
        max_retries: int = 3,
        base_backoff: float = 0.5,
        jitter: float = 0.25,
        max_backoff: float = 10.0,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.jitter = jitter
        self.max_backoff = max_backoff
        self._sleep = sleep_fn
        self._owns_client = False
        if client is not None:
            self._client = client
        else:
            kwargs: dict[str, Any] = {"timeout": timeout}
            if transport is not None:
                kwargs["transport"] = transport
            self._client = httpx.AsyncClient(**kwargs)
            self._owns_client = True

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def get_server_time(self) -> int:
        """Return the Bybit server time as integer epoch seconds."""
        payload = await self._request(_TIME_PATH)
        result = payload.get("result") or {}
        try:
            return int(result.get("timeSecond") or 0)
        except (TypeError, ValueError):
            raise BybitMalformedResponse("server time missing timeSecond") from None

    async def get_spot_instruments(self, limit: int = 1000) -> list[Instrument]:
        return await self._fetch_instruments("spot", status=None, limit=limit)

    async def get_inverse_instruments(self, limit: int = 1000) -> list[Instrument]:
        return await self._fetch_instruments("inverse", status=None, limit=limit)

    async def get_linear_instruments(
        self, status: str = "Trading", limit: int = 1000
    ) -> list[Instrument]:
        return await self._fetch_instruments("linear", status=status, limit=limit)

    async def get_linear_prelaunch_instruments(self, limit: int = 1000) -> list[Instrument]:
        return await self._fetch_instruments("linear", status="PreLaunch", limit=limit)

    async def get_spot_tickers(self) -> list[Ticker]:
        return await self._fetch_tickers("spot")

    async def get_linear_tickers(self) -> list[Ticker]:
        return await self._fetch_tickers("linear")

    async def get_announcements(self, limit: int = 50) -> list[Announcement]:
        params: dict[str, Any] = {"locale": "en-US", "limit": max(1, min(limit, 50))}
        payload = await self._request(_ANNOUNCEMENTS_PATH, params=params)
        result = payload.get("result") or {}
        raw_list = result.get("list") or []
        return [parse_announcement(raw) for raw in raw_list]

    # ------------------------------------------------------------------
    # Endpoint helpers
    # ------------------------------------------------------------------
    async def _fetch_instruments(
        self, category: str, status: Optional[str], limit: int
    ) -> list[Instrument]:
        results: list[Instrument] = []
        cursor: Optional[str] = None
        while True:
            params: dict[str, Any] = {"category": category, "limit": limit}
            if status is not None:
                params["status"] = status
            if cursor is not None:
                params["cursor"] = cursor
            payload = await self._request(_INSTRUMENTS_PATH, params=params)
            result = payload.get("result") or {}
            raw_list = result.get("list") or []
            for raw in raw_list:
                results.append(parse_instrument(category, raw))
            next_cursor = result.get("nextPageCursor")
            if not next_cursor:
                break
            cursor = str(next_cursor)
        return results

    async def _fetch_tickers(self, category: str) -> list[Ticker]:
        payload = await self._request(_TICKERS_PATH, params={"category": category})
        result = payload.get("result") or {}
        raw_list = result.get("list") or []
        return [parse_ticker(category, raw) for raw in raw_list]

    # ------------------------------------------------------------------
    # Core request with retries
    # ------------------------------------------------------------------
    async def _request(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        if self._client is None:
            raise BybitError("client is closed")
        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None
        attempts = 0
        while True:
            attempts += 1
            try:
                return await self._try_request(url, params)
            except BybitRetryableError as exc:
                last_error = exc
                if attempts > self.max_retries:
                    break
                delay = self._backoff_delay(attempts)
                logger.info(
                    "event=request_retry path=%s attempt=%d delay=%.3f error=%s",
                    path,
                    attempts,
                    delay,
                    type(exc).__name__,
                )
                await self._sleep(delay)
            except BybitError:
                raise
        # retries exhausted
        assert last_error is not None
        raise last_error

    async def _try_request(
        self, url: str, params: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        assert self._client is not None
        try:
            response = await self._client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise BybitTimeoutError(f"request to {url} timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise BybitConnectError(f"connection error to {url}: {exc}") from exc

        if response.status_code in RETRYABLE_HTTP_STATUS:
            body = self._safe_body(response)
            if response.status_code == 429:
                raise BybitRateLimitError(f"rate limited: HTTP 429")
            raise BybitServerError(f"retryable HTTP {response.status_code}: {body[:200]}")

        if response.status_code == 200:
            return self._parse_success(response)

        body = self._safe_body(response)
        raise BybitHTTPStatusError(response.status_code, body)

    def _parse_success(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BybitMalformedResponse("invalid JSON in response body") from exc
        if not isinstance(payload, dict):
            raise BybitMalformedResponse("response payload is not a JSON object")
        ret_code = payload.get("retCode")
        if ret_code not in (0, "0"):
            ret_msg = payload.get("retMsg") or payload.get("ret_msg") or "unknown"
            try:
                code_int = int(ret_code)
            except (TypeError, ValueError):
                code_int = -1
            if code_int in RETRYABLE_RETCODES:
                raise BybitRateLimitError(f"Bybit retCode={code_int} retMsg={ret_msg!r}")
            raise BybitAPIError(code_int, str(ret_msg))
        return payload

    def _safe_body(self, response: httpx.Response) -> str:
        try:
            return response.text
        except Exception:
            return ""

    def _backoff_delay(self, attempt: int) -> float:
        base = min(self.base_backoff * (2 ** (attempt - 1)), self.max_backoff)
        if self.jitter > 0:
            base += random.uniform(0, self.jitter)
        return base


__all__ = [
    "BybitRestClient",
    "BybitError",
    "BybitRetryableError",
    "BybitTimeoutError",
    "BybitConnectError",
    "BybitRateLimitError",
    "BybitServerError",
    "BybitAPIError",
    "BybitHTTPStatusError",
    "BybitMalformedResponse",
]
class BybitMalformedResponse(BybitError):
    pass