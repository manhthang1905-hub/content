"""
api_client.py — Reusable Anthropic API HTTP client for CONTENT.

Features:
- Model fallback: sonnet → haiku
- Retry on transient errors (5xx, timeout)
- Configurable timeout per stage
- Full response logging for debugging
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# ── Model tiers ──────────────────────────────────────────────────────────────
MODEL_SONNET = os.environ.get("ANTHROPIC_SONNET_MODEL", "claude-sonnet-4-6")
MODEL_HAIKU  = os.environ.get("ANTHROPIC_HAIKU_MODEL",  "claude-sonnet-4-6")  # same tier until haiku available

# Stage-specific defaults — 3 bước của tool: analyze · write · check
STAGE_MODELS = {
    "analyze": MODEL_SONNET,    # Phân tích đối thủ + dàn ý — sonnet
    "write":   MODEL_SONNET,    # Viết từng phần — sonnet
    "check":   MODEL_SONNET,    # Đánh giá từng phần — sonnet
}

STAGE_TIMEOUTS = {
    "analyze": 300,   # Input lớn (cả transcript đối thủ) → cần thời gian
    "write":   300,   # Viết có thể chậm — giữ 5 phút
    "check":   180,   # Đánh giá 1 phần — nhẹ hơn
}

STAGE_OUTPUT_TOKEN_CAPS = {
    "analyze": int(os.environ.get("CONTENT_API_MAX_OUTPUT_TOKENS_ANALYZE", "32000")),
    "write":   int(os.environ.get("CONTENT_API_MAX_OUTPUT_TOKENS_WRITE", "16000")),
    "check":   int(os.environ.get("CONTENT_API_MAX_OUTPUT_TOKENS_CHECK", "8000")),
}

# ── Config ───────────────────────────────────────────────────────────────────
API_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://routerapi.vovantin.online")
API_KEY      = os.environ.get("ANTHROPIC_API_KEY", "")
MAX_RETRIES  = int(os.environ.get("CONTENT_API_MAX_RETRIES", "999999"))  # transient API/server errors wait/retry for long autonomous runs
RETRY_DELAY  = 5    # base delay (seconds)
RETRY_5XX_CAP = int(os.environ.get("CONTENT_API_RETRY_5XX_CAP", "300"))  # max wait for 503/502/connection errors
RETRY_429_WAITS = [60, 120, 300, 600, 600, 600, 600, 600, 600, 600, 600, 600]  # Progressive backoff for 429
RETRY_QUOTA_WAIT = int(os.environ.get("CONTENT_API_QUOTA_WAIT", "120"))  # wait between retries when account quota/credit is exhausted (polls for top-up)
RETRY_AUTH_WAIT  = int(os.environ.get("CONTENT_API_AUTH_WAIT", "60"))   # wait between retries on 401 (token temporarily revoked) — long gap avoids 400-retry log spam / GUI lag
MIN_GAP_SECONDS = 12  # Minimum gap between API calls to prevent 429 rate limiting
USE_STREAMING = os.environ.get("CONTENT_API_STREAM", "1").strip().lower() not in {"0", "false", "no", "off"}
DEFAULT_MAX_OUTPUT_TOKENS = int(os.environ.get("CONTENT_API_MAX_OUTPUT_TOKENS", "16000"))
PREFLIGHT_TIMEOUT_SECONDS = int(os.environ.get("CONTENT_API_PREFLIGHT_TIMEOUT", "30"))
PREFLIGHT_ATTEMPTS = int(os.environ.get("CONTENT_API_PREFLIGHT_ATTEMPTS", "2"))

# ── Circuit Breaker ──────────────────────────────────────────────────────────
# Protects the entire pipeline from wasting time when API is down.
# When OPEN: calls raise ApiUnavailableError immediately (no retry waste).
# Probe runs every COOLDOWN seconds to detect recovery.

CIRCUIT_FAILURE_THRESHOLD = int(os.environ.get("CONTENT_API_CIRCUIT_FAILURE_THRESHOLD", "3"))
CIRCUIT_COOLDOWN_SECONDS  = 120  # seconds to wait before probing again
PRIMARY_TIMEOUTS_BEFORE_FALLBACK = int(os.environ.get("CONTENT_API_PRIMARY_TIMEOUTS_BEFORE_FALLBACK", "999999"))
FALLBACK_ON_SERVER_DOWN = os.environ.get("CONTENT_API_FALLBACK_ON_SERVER_DOWN", "0").strip().lower() in {"1", "true", "yes", "on"}


class ApiUnavailableError(RuntimeError):
    """Raised when the circuit breaker is OPEN — API is known to be down.
    Callers should pause and wait instead of treating this as a permanent failure."""
    pass


class ApiQuotaError(ApiUnavailableError):
    """Raised when the API is unavailable because quota or balance is exhausted."""
    pass


def _error_message_from_response(resp: httpx.Response) -> str:
    try:
        data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except Exception:
        try:
            data = resp.json()
        except Exception:
            return getattr(resp, "text", "") or "unknown error"
    if isinstance(data, dict):
        return data.get("error", {}).get("message") or data.get("message") or getattr(resp, "text", "") or "unknown error"
    return getattr(resp, "text", "") or "unknown error"


def _is_quota_error(status_code: int, message: str) -> bool:
    """Detect quota/credit/balance exhaustion across gateways (Anthropic 402, new-api 403
    '用户额度不足'/'insufficient quota', etc.). These are RECOVERABLE by topping up, so the
    pipeline should WAIT and retry, never fail permanently."""
    msg = str(message or "").lower()
    quota_markers = (
        "quota", "balance", "spending limit", "daily spending", "available",
        "insufficient", "额度", "余额", "用户额度不足", "credit", "充值", "欠费", "arrears",
        "out of credit", "no credit", "billing",
    )
    if status_code in (402, 403):
        return any(k in msg for k in quota_markers)
    # Some gateways return 200/400 with a quota message in the body.
    return status_code != 200 and any(k in msg for k in ("用户额度不足", "insufficient quota", "额度不足"))


class CircuitBreaker:
    """Simple circuit breaker for API calls.

    States:
        CLOSED    — normal operation, calls go through
        OPEN      — API is down, calls are blocked immediately
        HALF_OPEN — cooldown expired, allow ONE probe call to test recovery
    """

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        cooldown: int = CIRCUIT_COOLDOWN_SECONDS,
        log_fn=None,
    ):
        self._threshold = failure_threshold
        self._cooldown = cooldown
        self._log = log_fn or (lambda msg: None)
        self._failures = 0
        self._state = "CLOSED"          # CLOSED | OPEN | HALF_OPEN
        self._last_failure_time = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "OPEN":
                # Auto-transition to HALF_OPEN after cooldown
                if time.time() - self._last_failure_time >= self._cooldown:
                    self._state = "HALF_OPEN"
                    self._log(f"[CircuitBreaker] OPEN → HALF_OPEN — probing API availability")
            return self._state

    @property
    def cooldown(self) -> int:
        return self._cooldown

    def check(self):
        """Call before making an API request. Raises ApiUnavailableError if circuit is OPEN."""
        s = self.state  # triggers auto-transition check
        if s == "OPEN":
            wait_left = self._cooldown - (time.time() - self._last_failure_time)
            raise ApiUnavailableError(
                f"API unavailable (circuit OPEN). "
                f"Will probe again in {max(0, int(wait_left))}s."
            )
        # HALF_OPEN and CLOSED both allow the call to proceed

    def record_success(self):
        """Call after a successful API response."""
        with self._lock:
            if self._state != "CLOSED":
                self._log(f"[CircuitBreaker] {self._state} → CLOSED — API recovered")
            self._failures = 0
            self._state = "CLOSED"

    def record_failure(self):
        """Call after a failed API response (5xx, timeout, connection error)."""
        with self._lock:
            self._failures += 1
            self._last_failure_time = time.time()
            if self._state == "HALF_OPEN":
                # Probe failed — back to OPEN
                self._state = "OPEN"
                self._log(
                    f"[CircuitBreaker] HALF_OPEN → OPEN — probe failed, "
                    f"waiting {self._cooldown}s before next probe"
                )
            elif self._failures >= self._threshold and self._state == "CLOSED":
                self._state = "OPEN"
                self._log(
                    f"[CircuitBreaker] CLOSED → OPEN — {self._failures} consecutive failures. "
                    f"Blocking API calls for {self._cooldown}s."
                )

    def reset(self):
        """Force reset to CLOSED state."""
        with self._lock:
            self._failures = 0
            self._state = "CLOSED"


def _configured_fallback_endpoints() -> list[dict[str, str]]:
    base_url = os.environ.get("CONTENT_API_FALLBACK_BASE_URL", "").strip()
    api_key = os.environ.get("CONTENT_API_FALLBACK_KEY", "").strip()
    if not base_url or not api_key:
        return []
    return [{"name": os.environ.get("CONTENT_API_FALLBACK_NAME", "fallback"), "base_url": base_url, "key": api_key}]


# ── Fallback endpoints (optional, configured via environment) ────────────────
FALLBACK_ENDPOINTS = _configured_fallback_endpoints()


@dataclass
class ApiResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stage: str = ""
    retries: int = 0
    elapsed: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_json(self) -> dict:
        return {
            "stage": self.stage,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed": round(self.elapsed, 1),
            "retries": self.retries,
        }


class ApiClient:
    """
    Low-level Anthropic API client.

    Usage:
        client = ApiClient()
        resp = client.call(
            stage="intelligence",
            system="You are an analyst...",
            user_message="Analyze this competitor...",
            max_tokens=2000,
        )
        print(resp.text)
    """

    def __init__(
        self,
        base_url: str = API_BASE_URL,
        api_key: str = API_KEY,
        log_fn=None,
        stop_event: threading.Event | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key
        self.log_fn   = log_fn or (lambda msg: None)
        self.stop_event = stop_event
        self._headers = {
            "Authorization":          f"Bearer {self.api_key}",
            "x-api-key":              self.api_key,
            "anthropic-auth-token":   os.environ.get("ANTHROPIC_AUTH_TOKEN", self.api_key),
            "anthropic-version":      "2023-06-01",
            "content-type":           "application/json",
        }
        self._last_call_time: float = 0  # Track last API call for rate limit prevention
        self._failover_active = False    # True when using fallback endpoint
        self.circuit = circuit_breaker or CircuitBreaker(log_fn=self.log_fn)

    def _log(self, msg: str) -> None:
        self.log_fn(msg)

    def _activate_fallback(self, reason: str) -> bool:
        if self._failover_active or not FALLBACK_ENDPOINTS:
            return False
        fb = FALLBACK_ENDPOINTS[0]
        self._log(f"[API] {reason} — switching to fallback: {fb['name']} ({fb['base_url']})")
        self.base_url = fb["base_url"].rstrip("/")
        self._headers["Authorization"] = f"Bearer {fb['key']}"
        self._headers["x-api-key"] = fb["key"]
        self._headers["anthropic-auth-token"] = fb["key"]
        self._failover_active = True
        self.circuit.reset()
        return True

    def preflight(self, stage: str = "intelligence") -> bool:
        """Probe API availability before starting an expensive job."""
        model = STAGE_MODELS.get(stage, MODEL_SONNET)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 16,
            "temperature": 0,
            "system": "Return JSON only.",
            "messages": [{"role": "user", "content": "Return {\"ok\":true}."}],
        }

        endpoints = [{"name": "primary", "base_url": self.base_url, "key": self.api_key}]
        endpoints.extend(FALLBACK_ENDPOINTS)

        seen: set[str] = set()
        for endpoint in endpoints:
            base_url = endpoint["base_url"].rstrip("/")
            if base_url in seen:
                continue
            seen.add(base_url)
            headers = dict(self._headers)
            headers["Authorization"] = f"Bearer {endpoint['key']}"
            headers["x-api-key"] = endpoint["key"]
            headers["anthropic-auth-token"] = endpoint["key"]
            url = f"{base_url}/v1/messages"
            for attempt in range(1, PREFLIGHT_ATTEMPTS + 1):
                if self.stop_event and self.stop_event.is_set():
                    raise RuntimeError("Stopped by user")
                self._log(f"[API] Preflight {endpoint['name']} attempt {attempt}/{PREFLIGHT_ATTEMPTS}")
                t0 = time.time()
                try:
                    with httpx.Client(timeout=PREFLIGHT_TIMEOUT_SECONDS) as client:
                        resp = client.post(url, json=payload, headers=headers)
                    elapsed = time.time() - t0
                    if resp.status_code == 429 or 500 <= resp.status_code < 600:
                        self._log(f"[API] Preflight {endpoint['name']} unavailable ({resp.status_code}, {elapsed:.1f}s)")
                        continue
                    if resp.status_code == 402:
                        msg = _error_message_from_response(resp)
                        self._log(f"[API] Preflight {endpoint['name']} quota unavailable ({elapsed:.1f}s): {msg}")
                        continue
                    if 400 <= resp.status_code < 500:
                        self._log(f"[API] Preflight {endpoint['name']} rejected ({resp.status_code}, {elapsed:.1f}s)")
                        break
                    resp.raise_for_status()
                    self.base_url = base_url
                    self._headers["Authorization"] = f"Bearer {endpoint['key']}"
                    self._headers["x-api-key"] = endpoint["key"]
                    self._headers["anthropic-auth-token"] = endpoint["key"]
                    self._failover_active = endpoint["name"] != "primary"
                    self.circuit.reset()
                    self._log(f"[API] Preflight OK on {endpoint['name']} ({elapsed:.1f}s)")
                    return True
                except Exception as e:
                    elapsed = time.time() - t0
                    self._log(f"[API] Preflight {endpoint['name']} failed ({elapsed:.1f}s): {e}")

        self.circuit.record_failure()
        self._log("[API] Preflight failed on all endpoints; job will wait/retry during normal API calls")
        return False

    def _extract_stream_text(self, resp: httpx.Response) -> tuple[str, dict]:
        parts: list[str] = []
        usage: dict[str, Any] = {}
        current_event = ""
        for raw_line in resp.iter_lines():
            if not raw_line:
                current_event = ""
                continue
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            data_text = line.split(":", 1)[1].strip()
            if not data_text or data_text == "[DONE]":
                continue
            try:
                event = json.loads(data_text)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type") or current_event
            if event_type == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    parts.append(delta["text"])
            elif event_type == "message_delta":
                delta_usage = event.get("usage") or {}
                if delta_usage:
                    usage.update(delta_usage)
            elif event_type == "message_start":
                msg_usage = (event.get("message") or {}).get("usage") or {}
                if msg_usage:
                    usage.update(msg_usage)
        return "".join(parts), usage

    def call(
        self,
        stage: str,
        system: str,
        user_message: str,
        max_tokens: int = 4096,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ApiResponse:
        """
        Make a single API call with retry logic.
        Raises RuntimeError after MAX_RETRIES failures.
        """
        chosen_model   = model or STAGE_MODELS.get(stage, MODEL_SONNET)
        timeout        = STAGE_TIMEOUTS.get(stage, 120)
        url            = f"{self.base_url}/v1/messages"
        # Trần token an toàn của stage. Nếu caller truyền max_tokens cụ thể thì
        # TÔN TRỌNG nó (để ép độ dài từng phần) — chỉ không vượt trần stage.
        cap = STAGE_OUTPUT_TOKEN_CAPS.get(stage, DEFAULT_MAX_OUTPUT_TOKENS)
        if max_tokens and int(max_tokens) > 0:
            effective_max_tokens = min(int(max_tokens), cap)
        else:
            effective_max_tokens = cap

        payload: dict[str, Any] = {
            "model":      chosen_model,
            "max_tokens": effective_max_tokens,
            "temperature": temperature,
            "system":     system,
            "messages":   [{"role": "user", "content": user_message}],
        }
        if USE_STREAMING:
            payload["stream"] = True
        self._log(
            f"[API] {stage} input={len(user_message):,} chars, max_tokens={effective_max_tokens}, "
            f"stream={'on' if USE_STREAMING else 'off'}"
        )

        last_error: Exception | None = None
        all_server_down = True  # Track if ALL failures were server-down (for ApiUnavailableError)
        for attempt in range(1, MAX_RETRIES + 1):
            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("Stopped by user")
            # Circuit breaker: pause instead of failing the job when API is known to be down
            while True:
                try:
                    self.circuit.check()
                    break
                except ApiUnavailableError as e:
                    self._log(f"[API] {stage} paused — {e}")
                    wait_left = self.circuit.cooldown - (time.time() - self.circuit._last_failure_time)
                    wait_time = min(CIRCUIT_COOLDOWN_SECONDS, max(1, int(wait_left)))
                    if self.stop_event:
                        if self.stop_event.wait(wait_time):
                            raise RuntimeError("Stopped by user")
                    else:
                        time.sleep(wait_time)
            # Smart rate limit prevention: enforce minimum gap between calls
            elapsed_since_last = time.time() - self._last_call_time
            if self._last_call_time > 0 and elapsed_since_last < MIN_GAP_SECONDS:
                gap_wait = MIN_GAP_SECONDS - elapsed_since_last
                self._log(f"[API] Rate-limit prevention: waiting {gap_wait:.0f}s before call")
                if self.stop_event and self.stop_event.wait(gap_wait):
                    raise RuntimeError("Stopped by user")

            self._log(f"[API] {stage} → {chosen_model} (attempt {attempt}/{MAX_RETRIES})")
            t0 = time.time()
            self._last_call_time = t0
            try:
                with httpx.Client(timeout=timeout) as client:
                    if USE_STREAMING:
                        with client.stream("POST", url, json=payload, headers=self._headers) as resp:
                            if resp.status_code == 429:
                                data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
                                msg = data.get("error", {}).get("message", "rate limit")
                                last_error = Exception(f"API 429: {msg}")
                                all_server_down = True
                                elapsed = time.time() - t0
                                self._log(f"[API] {stage} attempt {attempt} failed ({elapsed:.1f}s): API 429 rate limit")
                                wait_429 = RETRY_429_WAITS[min(attempt - 1, len(RETRY_429_WAITS) - 1)]
                                self._log(f"[API] Rate limited — waiting {wait_429}s before retry (progressive)...")
                                if self.stop_event and self.stop_event.wait(wait_429):
                                    raise RuntimeError("Stopped by user")
                                continue
                            if resp.status_code == 401:
                                data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
                                msg = data.get("error", {}).get("message", "invalid token")
                                last_error = Exception(f"API 401: {msg}")
                                elapsed = time.time() - t0
                                if not self._failover_active and FALLBACK_ENDPOINTS:
                                    self._activate_fallback("401 on primary")
                                    url = f"{self.base_url}/v1/messages"
                                    continue
                                if attempt <= 3 or attempt % 10 == 0:
                                    self._log(f"[API] {stage} 401 invalid token (attempt {attempt}) — token may be temporarily revoked; waiting {RETRY_AUTH_WAIT}s and retrying. Job will NOT fail.")
                                if attempt < MAX_RETRIES:
                                    if self.stop_event and self.stop_event.wait(RETRY_AUTH_WAIT):
                                        raise RuntimeError("Stopped by user")
                                    if not self.stop_event:
                                        time.sleep(RETRY_AUTH_WAIT)
                                    continue
                                raise RuntimeError(f"API 401: {msg}")
                            if 400 <= resp.status_code < 500:
                                msg = _error_message_from_response(resp)
                                if _is_quota_error(resp.status_code, msg):
                                    last_error = Exception(f"API {resp.status_code}: {msg}")
                                    if not self._failover_active and FALLBACK_ENDPOINTS:
                                        self._activate_fallback("quota exhausted on primary")
                                        url = f"{self.base_url}/v1/messages"
                                        continue
                                    self._log(f"[API] {stage} quota/credit exhausted ({resp.status_code}): {str(msg)[:140]}")
                                    self._log(f"[API] Waiting {RETRY_QUOTA_WAIT}s for account top-up, then retrying — job will NOT fail...")
                                    if self.stop_event and self.stop_event.wait(RETRY_QUOTA_WAIT):
                                        raise RuntimeError("Stopped by user")
                                    if not self.stop_event:
                                        time.sleep(RETRY_QUOTA_WAIT)
                                    continue
                                raise RuntimeError(f"API {resp.status_code}: {msg}")
                            resp.raise_for_status()
                            text, usage = self._extract_stream_text(resp)
                            data = {"content": [{"text": text}], "model": chosen_model, "usage": usage}
                    else:
                        resp = client.post(url, json=payload, headers=self._headers)

                        # 429 = rate limit — retryable with long wait (NOT permanent)
                        if resp.status_code == 429:
                            data = resp.json()
                            msg  = data.get("error", {}).get("message", resp.text)
                            last_error = Exception(f"API 429: {msg}")
                            all_server_down = True
                            elapsed = time.time() - t0
                            self._log(f"[API] {stage} attempt {attempt} failed ({elapsed:.1f}s): API 429 rate limit")
                            wait_429 = RETRY_429_WAITS[min(attempt - 1, len(RETRY_429_WAITS) - 1)]
                            self._log(f"[API] Rate limited — waiting {wait_429}s before retry (progressive)...")
                            if self.stop_event and self.stop_event.wait(wait_429):
                                raise RuntimeError("Stopped by user")
                            continue

                        # 401 = invalid token/router auth issue — fail over, then retry instead of killing the job immediately
                        if resp.status_code == 401:
                            data = resp.json()
                            msg = data.get("error", {}).get("message", resp.text)
                            last_error = Exception(f"API 401: {msg}")
                            elapsed = time.time() - t0

                            if not self._failover_active and FALLBACK_ENDPOINTS:
                                self._activate_fallback("401 on primary")
                                url = f"{self.base_url}/v1/messages"
                                continue

                            if attempt <= 3 or attempt % 10 == 0:
                                self._log(f"[API] {stage} 401 invalid token (attempt {attempt}) — token may be temporarily revoked; waiting {RETRY_AUTH_WAIT}s and retrying. Job will NOT fail.")
                            if attempt < MAX_RETRIES:
                                if self.stop_event and self.stop_event.wait(RETRY_AUTH_WAIT):
                                    raise RuntimeError("Stopped by user")
                                if not self.stop_event:
                                    time.sleep(RETRY_AUTH_WAIT)
                                continue
                            raise RuntimeError(f"API 401: {msg}")

                        # Other 4xx = permanent error UNLESS quota/credit is exhausted,
                        # which is recoverable by topping up — so wait and retry, never fail.
                        if 400 <= resp.status_code < 500:
                            msg = _error_message_from_response(resp)
                            if _is_quota_error(resp.status_code, msg):
                                last_error = Exception(f"API {resp.status_code}: {msg}")
                                if not self._failover_active and FALLBACK_ENDPOINTS:
                                    self._activate_fallback("quota exhausted on primary")
                                    url = f"{self.base_url}/v1/messages"
                                    continue
                                self._log(f"[API] {stage} quota/credit exhausted ({resp.status_code}): {str(msg)[:140]}")
                                self._log(f"[API] Waiting {RETRY_QUOTA_WAIT}s for account top-up, then retrying — job will NOT fail...")
                                if self.stop_event and self.stop_event.wait(RETRY_QUOTA_WAIT):
                                    raise RuntimeError("Stopped by user")
                                if not self.stop_event:
                                    time.sleep(RETRY_QUOTA_WAIT)
                                continue
                            raise RuntimeError(f"API {resp.status_code}: {msg}")

                        resp.raise_for_status()
                        data = resp.json()

                elapsed = time.time() - t0

                text         = data["content"][0]["text"]
                model_used   = data.get("model", chosen_model)
                usage        = data.get("usage", {})
                in_tok        = usage.get("input_tokens", 0)
                out_tok       = usage.get("output_tokens", 0)

                if not str(text or "").strip():
                    raise ValueError("API returned empty text response")

                self._log(
                    f"[API] {stage} OK — {out_tok} out tokens, "
                    f"{len(text):,} chars, {elapsed:.1f}s"
                )
                self.circuit.record_success()

                return ApiResponse(
                    text=text,
                    model=model_used,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    stage=stage,
                    retries=attempt - 1,
                    elapsed=elapsed,
                )

            except ApiQuotaError as e:
                self._log(f"[API] {stage} quota unavailable: {e}")
                raise
            except (RuntimeError, ApiUnavailableError):
                raise  # permanent errors / circuit breaker — don't retry
            except Exception as e:
                elapsed = time.time() - t0
                last_error = e
                err_str = str(e)
                self._log(f"[API] {stage} attempt {attempt} failed ({elapsed:.1f}s): {e}")
                # Cloudflare/server 5xx, connection failures, and timeouts mean the router is overloaded/down.
                is_server_down = (
                    "empty text response" in err_str.lower() or
                    "500" in err_str or "502" in err_str or "503" in err_str or
                    "520" in err_str or "522" in err_str or "524" in err_str or
                    "10060" in err_str or "10061" in err_str or "Connection" in err_str or
                    "timed out" in err_str.lower() or "timeout" in err_str.lower() or
                    "ReadTimeout" in err_str or "ConnectTimeout" in err_str
                )
                if is_server_down:
                    self.circuit.record_failure()
                    if FALLBACK_ON_SERVER_DOWN and not self._failover_active and FALLBACK_ENDPOINTS and attempt >= PRIMARY_TIMEOUTS_BEFORE_FALLBACK:
                        self._activate_fallback("Primary endpoint timeout/down")
                        url = f"{self.base_url}/v1/messages"
                        continue
                else:
                    all_server_down = False
                wait = min(RETRY_DELAY * (2 ** min(attempt - 1, 6)), RETRY_5XX_CAP)
                if is_server_down:
                    self._log(f"[API] Server down/timeout — waiting {wait}s before retry...")
                else:
                    self._log(f"[API] Retrying in {wait}s...")
                if self.stop_event and self.stop_event.wait(wait):
                    raise RuntimeError("Stopped by user")
                if not self.stop_event:
                    time.sleep(wait)

        # Should only be reached if CONTENT_API_MAX_RETRIES is deliberately finite.
        if all_server_down:
            raise ApiUnavailableError(
                f"API {stage} unavailable after {MAX_RETRIES} attempts: {last_error}"
            )
        raise RuntimeError(f"API {stage} failed after {MAX_RETRIES} attempts: {last_error}")

    def call_json(
        self,
        stage: str,
        system: str,
        user_message: str,
        max_tokens: int = 4096,
        model: str | None = None,
        parse_retries: int = 2,
    ) -> dict:
        """
        Like call(), but parses the response as JSON.
        Strips markdown code fences if present and retries malformed JSON.
        """
        parse_feedback = ""
        text = ""

        for parse_attempt in range(1, parse_retries + 2):
            retry_message = user_message if not parse_feedback else f"""{user_message}

PREVIOUS RESPONSE WAS REJECTED BY THE PIPELINE:
{parse_feedback}

Return the complete result again as STRICT valid JSON only. No markdown. No trailing explanation. Escape all quotes inside string values. Do not truncate the JSON."""
            resp = self.call(stage, system, retry_message, max_tokens, model)
            text = self._extract_json_text(resp.text)

            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                parse_feedback = f"Invalid JSON: {e}. Response preview: {text[:300]}"
                self._log(
                    f"[API] {stage} returned malformed JSON "
                    f"({parse_attempt}/{parse_retries + 1}): {e}"
                )
                if parse_attempt <= parse_retries:
                    self._log(f"[API] {stage} retrying for strict JSON output...")
                    continue
                raise ValueError(
                    f"Stage '{stage}' returned non-JSON response after {parse_retries + 1} attempt(s): {e}\n"
                    f"Response preview: {text[:300]}"
                ) from e

        raise RuntimeError(f"Stage '{stage}' JSON parsing failed unexpectedly")

    @staticmethod
    def _extract_json_text(raw_text: str) -> str:
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(
                lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            ).strip()

        if text.startswith("{") or text.startswith("["):
            return text

        first_obj = text.find("{")
        first_arr = text.find("[")
        starts = [pos for pos in (first_obj, first_arr) if pos >= 0]
        if starts:
            text = text[min(starts):].strip()
        return text


# ── CLI backend ───────────────────────────────────────────────────────────────

def _strip_ansi(text: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', text)


# Windows: npm cai 'claude.cmd', ban native cai 'claude.exe' — shutil.which tim ra
# ban nao co tren PATH; subprocess khong tu resolve ten khong duoi tren Windows.
_CLAUDE_CMD = (
    shutil.which("claude") or shutil.which("claude.cmd") or "claude.cmd"
) if platform.system() == "Windows" else "claude"

# Chay an (khong bat cua so console khi goi tu GUI/pythonw)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Moi claude.exe con dang chay — de kill sach khi tat tool (khong de mo coi dot quota)
_ACTIVE_CLI_PROCS: set = set()


def kill_active_cli_procs() -> None:
    """Kill moi claude CLI con dang chay. GUI goi trong on_close; atexit goi khi
    run.py thoat binh thuong."""
    for p in list(_ACTIVE_CLI_PROCS):
        try:
            p.kill()
        except Exception:
            pass
        _ACTIVE_CLI_PROCS.discard(p)


import atexit as _atexit  # noqa: E402
_atexit.register(kill_active_cli_procs)


# ── CLI backup qua gateway (Max het token → TU DONG chuyen, dinh ky thu ve Max) ──
# Cung claude CLI, chi doi env ANTHROPIC_BASE_URL/KEY khi spawn → tinh tien vao
# credit gateway thay vi tai khoan Max. Key dat trong config/.env:
#   CLAUDE_BACKUP_KEYS=sk-xxx,sk-yyy        (nhieu key xoay vong, 401/het → nghi 60')
#   CLAUDE_BACKUP_BASE_URL=...              (mac dinh vip.digishop.work)
CLI_BACKUP_BASE_URL = os.environ.get("CLAUDE_BACKUP_BASE_URL", "https://vip.digishop.work").strip()
CLI_BACKUP_QUOTA_URL = os.environ.get("CLAUDE_BACKUP_QUOTA_URL", "https://token-quota.digishop.work").strip()
CLI_BACKUP_KEYS = [k.strip() for k in os.environ.get("CLAUDE_BACKUP_KEYS", "").split(",") if k.strip()]
CLI_BACKUP_RETRY_MAX_MIN = int(os.environ.get("CONTENT_CLI_RETRY_MAX_MINUTES", "30"))  # chu ky thu ve Max
_BK_COOLDOWN_SECS = 3600
_BK_QUOTA_TTL = 300

_bk_lock = threading.Lock()
_bk_state = {"active": False, "last_max_try": 0.0, "key_i": 0}
_bk_cooldown: dict = {}      # key -> epoch het cooldown
_bk_quota_cache: dict = {}   # key -> (alive, epoch check)


def cli_backup_active() -> bool:
    """GUI health label doc: True = dang chay bang credit backup."""
    return bool(_bk_state["active"] and CLI_BACKUP_KEYS)


def _bk_key_has_quota(key: str) -> bool:
    """Check remaining truoc khi dung (cache 5'). Loi mang → fail-open (True)."""
    now = time.time()
    c = _bk_quota_cache.get(key)
    if c and (now - c[1]) < _BK_QUOTA_TTL:
        return c[0]
    alive = True
    try:
        import urllib.request as _u
        req = _u.Request(CLI_BACKUP_QUOTA_URL, headers={"Authorization": f"Bearer {key}"})
        with _u.urlopen(req, timeout=8) as r:
            q = (json.loads(r.read()) or {}).get("quota", {})
        alive = (not q.get("is_expired")) and int(q.get("remaining", 0) or 0) > 0
    except Exception:
        alive = True
    _bk_quota_cache[key] = (alive, now)
    return alive


def _bk_next_key() -> str:
    """Round-robin qua CLI_BACKUP_KEYS, bo key dang cooldown/het quota.
    Tat ca chet → tra key sap het cooldown nhat (fail-open, van chay duoc)."""
    keys = CLI_BACKUP_KEYS
    if not keys:
        return ""
    if len(keys) == 1:
        return keys[0]
    now = time.time()
    with _bk_lock:
        start = _bk_state["key_i"]
        _bk_state["key_i"] += 1
    for off in range(len(keys)):
        k = keys[(start + off) % len(keys)]
        if _bk_cooldown.get(k, 0) > now:
            continue
        if not _bk_key_has_quota(k):
            continue
        return k
    return min(keys, key=lambda kk: _bk_cooldown.get(kk, 0))


def _bk_mark_cooldown(key: str) -> None:
    if key:
        _bk_cooldown[key] = time.time() + _BK_COOLDOWN_SECS
        _bk_quota_cache.pop(key, None)


class CliApiClient:
    """
    Backend dung claude CLI thay vi HTTP.
    Yeu cau: 'claude' CLI da cai va da dang nhap (claude login).
    Bat bang: api_backend: cli trong config.yaml.
    Model mac dinh: claude-opus-4-8 (Max 20x).
    """

    _MAX_RETRIES = 999999  # retry indefinitely — never break the pipeline
    # 300s tung lam write timeout voi video dai (transcript ~36k chars → Opus viet >5
    # phut), retry vo han moi lan lai dot 5 phut + quota. 30 phut du cho moi do dai.
    _RETRY_DELAY = 10   # seconds, tang dan theo attempt (capped at 60s)
    _TIMEOUT     = int(os.environ.get("CONTENT_CLI_TIMEOUT", "1800"))  # seconds per call

    _QUOTA_MARKERS = ("rate limit", "too many requests", "usage limit", "quota", "overloaded", "529",
                      "session limit")
    _AUTH_MARKERS  = ("401", "invalid api key", "authentication", "unauthorized")  # key backup chet

    def __init__(
        self,
        cli_model: str = "claude-opus-4-8",
        log_fn=None,
        stop_event: threading.Event | None = None,
        fallback: "ApiClient | None" = None,
    ):
        self._cli_model    = cli_model
        self.log_fn        = log_fn or (lambda msg: None)
        self.stop_event    = stop_event
        self._fallback     = fallback      # ApiClient dung khi CLI het quota
        self._quota_hit    = False         # True = da chuyen sang fallback vinh vien

    def _log(self, msg: str) -> None:
        self.log_fn(msg)

    def call(
        self,
        stage: str,
        system: str,
        user_message: str,
        max_tokens: int = 4096,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ApiResponse:
        chosen_model = self._cli_model  # CLI backend luon dung cli_model, bo qua model tu pipeline
        full_prompt  = f"<system>\n{system}\n</system>\n\n{user_message}" if system else user_message

        # Neu da het quota, dung fallback luon
        if self._quota_hit and self._fallback:
            return self._fallback.call(stage, system, user_message, max_tokens, model, temperature)

        self._log(f"[CLI] {stage} input={len(user_message):,} chars, model={chosen_model}")

        last_err: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            if self.stop_event and self.stop_event.is_set():
                raise RuntimeError("Stopped by user")

            # Che do: Max (OAuth) hay backup (gateway)? Dang backup thi cu moi
            # CLI_BACKUP_RETRY_MAX_MIN phut thu lai Max 1 lan — Max hoi la quay ve.
            use_backup = False
            if CLI_BACKUP_KEYS and _bk_state["active"]:
                if time.time() - _bk_state["last_max_try"] > CLI_BACKUP_RETRY_MAX_MIN * 60:
                    with _bk_lock:
                        _bk_state["last_max_try"] = time.time()
                    self._log(f"[CLI] thử lại Max (chu kỳ {CLI_BACKUP_RETRY_MAX_MIN} phút)...")
                else:
                    use_backup = True

            via = "backup" if use_backup else "Max"
            self._log(f"[CLI] {stage} -> {chosen_model} (attempt {attempt}/{self._MAX_RETRIES}, via {via})")
            t0 = time.time()
            backup_key = ""
            try:
                # Loc env: bo ANTHROPIC_* de claude CLI dung OAuth session cua no
                # (khong ke thua ANTHROPIC_BASE_URL cua routerapi)
                clean_env = {k: v for k, v in os.environ.items()
                             if k not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
                                          "ANTHROPIC_AUTH_TOKEN")}
                if use_backup:
                    backup_key = _bk_next_key()
                    clean_env["ANTHROPIC_BASE_URL"] = CLI_BACKUP_BASE_URL
                    clean_env["ANTHROPIC_AUTH_TOKEN"] = backup_key
                    clean_env["ANTHROPIC_API_KEY"] = backup_key
                # Popen (khong phai run) de: (1) chay an CREATE_NO_WINDOW,
                # (2) dang ky vao _ACTIVE_CLI_PROCS cho kill_active_cli_procs()
                proc = subprocess.Popen(
                    [_CLAUDE_CMD, "--print", "--model", chosen_model],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=clean_env,
                    creationflags=_CREATE_NO_WINDOW,
                )
                _ACTIVE_CLI_PROCS.add(proc)
                try:
                    out, err = proc.communicate(input=full_prompt, timeout=self._TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    raise
                finally:
                    _ACTIVE_CLI_PROCS.discard(proc)
                elapsed = time.time() - t0

                if proc.returncode != 0:
                    detail = (err or out or "").strip()[:300]
                    last_err = RuntimeError(f"claude exit {proc.returncode}: {detail}")
                    self._log(f"[CLI] {stage} attempt {attempt} failed ({elapsed:.1f}s): exit {proc.returncode}: {detail}")
                    detail_lower = detail.lower()
                    is_quota = any(m in detail_lower for m in self._QUOTA_MARKERS)
                    is_auth  = any(m in detail_lower for m in self._AUTH_MARKERS)
                    if use_backup and (is_quota or is_auth):
                        # Key backup het token/401 → nghi 60', retry NGAY voi key khac.
                        # HET SACH key → cho RETRY_QUOTA_WAIT roi thu lai (khong duoc
                        # phep continue chay — se lap nong spam gateway 3-5s/lan)
                        _bk_mark_cooldown(backup_key)
                        self._log(f"[CLI] key backup ...{backup_key[-6:]} hết/lỗi — nghỉ 60 phút, đổi key khác")
                        _now = time.time()
                        if all(_bk_cooldown.get(k, 0) > _now for k in CLI_BACKUP_KEYS):
                            self._log(f"[CLI] TẤT CẢ key backup đang nghỉ — chờ {RETRY_QUOTA_WAIT}s rồi thử lại...")
                            if self.stop_event:
                                if self.stop_event.wait(RETRY_QUOTA_WAIT):
                                    raise RuntimeError("Stopped by user")
                            else:
                                time.sleep(RETRY_QUOTA_WAIT)
                        continue
                    if not use_backup and is_quota:
                        if CLI_BACKUP_KEYS:
                            # Max het token → TU DONG chuyen backup, retry ngay (job khong gay)
                            with _bk_lock:
                                _bk_state["active"] = True
                                _bk_state["last_max_try"] = time.time()
                            self._log(f"[CLI] Max hết token — TỰ CHUYỂN backup ({CLI_BACKUP_BASE_URL}), "
                                      f"thử lại Max sau mỗi {CLI_BACKUP_RETRY_MAX_MIN} phút")
                            continue
                        if self._fallback:
                            self._quota_hit = True
                            self._log(f"[CLI] Quota/rate-limit — chuyen sang HTTP fallback vinh vien")
                            return self._fallback.call(stage, system, user_message, max_tokens, model, temperature)
                    # tiep tuc retry
                else:
                    text = _strip_ansi(out).strip()
                    if not text:
                        last_err = ValueError("claude CLI returned empty output")
                        self._log(f"[CLI] {stage} attempt {attempt} empty output ({elapsed:.1f}s)")
                    else:
                        if not use_backup and _bk_state["active"]:
                            # Goi Max thanh cong trong luc backup dang bat → Max da hoi
                            with _bk_lock:
                                _bk_state["active"] = False
                            self._log("[CLI] Max đã hồi — quay về chạy bằng Max")
                        self._log(f"[CLI] {stage} OK — {len(text):,} chars, {elapsed:.1f}s (via {via})")
                        return ApiResponse(text=text, model=chosen_model, stage=stage, elapsed=elapsed)

            except FileNotFoundError:
                raise RuntimeError(
                    f"claude CLI not found ({_CLAUDE_CMD}) — cai tu https://claude.ai/code va chay 'claude login'"
                )
            except subprocess.TimeoutExpired:
                last_err = TimeoutError(f"claude CLI timeout after {self._TIMEOUT}s")
                self._log(f"[CLI] {stage} attempt {attempt} timed out")
            except Exception as e:
                elapsed = time.time() - t0
                last_err = e
                self._log(f"[CLI] {stage} attempt {attempt} failed ({elapsed:.1f}s): {e}")

            if attempt < self._MAX_RETRIES:
                wait = min(self._RETRY_DELAY * attempt, 60)  # cap 60s
                self._log(f"[CLI] Waiting {wait}s before retry...")
                if self.stop_event:
                    if self.stop_event.wait(wait):
                        raise RuntimeError("Stopped by user")
                else:
                    time.sleep(wait)

        raise RuntimeError(f"CLI {stage} failed after {self._MAX_RETRIES} attempts: {last_err}")

    def call_json(
        self,
        stage: str,
        system: str,
        user_message: str,
        max_tokens: int = 4096,
        model: str | None = None,
        parse_retries: int = 2,
    ) -> dict:
        parse_feedback = ""
        for parse_attempt in range(1, parse_retries + 2):
            msg = user_message if not parse_feedback else (
                f"{user_message}\n\nPREVIOUS RESPONSE REJECTED:\n{parse_feedback}\n\n"
                "Return STRICT valid JSON only. No markdown. No explanation."
            )
            resp = self.call(stage, system, msg, max_tokens, model)
            text = ApiClient._extract_json_text(resp.text)
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                parse_feedback = f"Invalid JSON: {e}. Preview: {text[:300]}"
                self._log(f"[CLI] {stage} malformed JSON ({parse_attempt}/{parse_retries + 1}): {e}")
                if parse_attempt > parse_retries:
                    raise ValueError(
                        f"Stage '{stage}' non-JSON after {parse_retries + 1} attempts: {e}"
                    ) from e
        raise RuntimeError(f"Stage '{stage}' JSON parsing failed")


def make_client(
    cfg: dict,
    log_fn=None,
    stop_event: threading.Event | None = None,
) -> "ApiClient | CliApiClient":
    """
    Factory: tra ve ApiClient (HTTP) hoac CliApiClient tuy theo api_backend trong config.
    Dung thay cho ApiClient() truc tiep o gui.py va run.py.
    """
    backend = cfg.get("api_backend", "http")
    if backend == "cli":
        cli_model = cfg.get("cli_model", "claude-opus-4-8")
        http_fallback = ApiClient(log_fn=log_fn, stop_event=stop_event)
        return CliApiClient(cli_model=cli_model, log_fn=log_fn, stop_event=stop_event,
                            fallback=http_fallback)
    return ApiClient(log_fn=log_fn, stop_event=stop_event)
