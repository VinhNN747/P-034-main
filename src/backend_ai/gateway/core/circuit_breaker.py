"""Per-model circuit breaker shared by every request in the process.

Without it, every request rediscovers a dead provider by waiting out the
full timeout before falling back, and keeps hammering a provider that is
rate-limiting or down. With it, after ``failure_threshold`` failures within
``window_seconds`` a model is skipped outright (OPEN) until a cooldown
passes; then one probe request is let through (HALF_OPEN) and its outcome
closes the circuit or re-opens it with a longer cooldown.

Only transient failures count (timeouts, connection errors, 429, 5xx). A 400
is our request's fault and a 401/403 is configuration; opening the circuit
for those would hide the real problem behind "model unavailable".

State is in-process. With several gateway replicas each one learns
independently, which is slower to react but still correct; move the state to
Redis if that matters.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_threshold: int = 5
    window_seconds: float = 60.0
    open_seconds: float = 30.0
    max_open_seconds: float = 300.0
    half_open_max_probes: int = 1


@dataclass
class _Circuit:
    state: CircuitState = CircuitState.CLOSED
    failures: deque[float] = field(default_factory=deque)
    opened_at: float = 0.0
    open_for: float = 0.0
    consecutive_opens: int = 0
    probes_in_flight: int = 0
    last_error: str | None = None


class CircuitBreaker:
    def __init__(self, config: CircuitBreakerConfig | None = None, *, clock: Callable[[], float] = time.monotonic):
        self.config = config or CircuitBreakerConfig()
        self._clock = clock
        self._circuits: dict[str, _Circuit] = {}

    @staticmethod
    def key(model: str, provider: str | None = None) -> str:
        return f"{provider}:{model}" if provider else model

    def _circuit(self, key: str) -> _Circuit:
        return self._circuits.setdefault(key, _Circuit())

    def state(self, key: str) -> CircuitState:
        circuit = self._circuits.get(key)
        if circuit is None:
            return CircuitState.CLOSED
        if circuit.state == CircuitState.OPEN and self._clock() - circuit.opened_at >= circuit.open_for:
            return CircuitState.HALF_OPEN
        return circuit.state

    def allow(self, key: str) -> bool:
        """True if a call may go to ``key`` now. In HALF_OPEN this reserves
        one of the probe slots, so callers must report the outcome."""
        circuit = self._circuits.get(key)
        if circuit is None or circuit.state == CircuitState.CLOSED:
            return True
        if circuit.state == CircuitState.OPEN:
            if self._clock() - circuit.opened_at < circuit.open_for:
                return False
            circuit.state = CircuitState.HALF_OPEN
            circuit.probes_in_flight = 0
        if circuit.probes_in_flight >= self.config.half_open_max_probes:
            return False
        circuit.probes_in_flight += 1
        return True

    def record_success(self, key: str) -> None:
        circuit = self._circuits.get(key)
        if circuit is None:
            return
        self._circuits[key] = _Circuit()

    def record_failure(self, key: str, *, error: str | None = None, retry_after: float | None = None) -> None:
        now = self._clock()
        circuit = self._circuit(key)
        circuit.last_error = error
        if circuit.state == CircuitState.HALF_OPEN:
            self._open(circuit, now, retry_after)
            return
        if circuit.state == CircuitState.OPEN:
            return
        circuit.failures.append(now)
        while circuit.failures and now - circuit.failures[0] > self.config.window_seconds:
            circuit.failures.popleft()
        # A provider telling us exactly when to come back (429 Retry-After)
        # is a stronger signal than our failure count.
        if retry_after is not None or len(circuit.failures) >= self.config.failure_threshold:
            self._open(circuit, now, retry_after)

    def release_probe(self, key: str) -> None:
        """Give back a HALF_OPEN probe slot that was reserved but never used
        (e.g. the call was abandoned before reaching the provider)."""
        circuit = self._circuits.get(key)
        if circuit is not None and circuit.probes_in_flight > 0:
            circuit.probes_in_flight -= 1

    def _open(self, circuit: _Circuit, now: float, retry_after: float | None) -> None:
        backoff = self.config.open_seconds * (2**circuit.consecutive_opens)
        circuit.open_for = min(max(backoff, retry_after or 0.0), self.config.max_open_seconds)
        circuit.state = CircuitState.OPEN
        circuit.opened_at = now
        circuit.consecutive_opens += 1
        circuit.probes_in_flight = 0
        circuit.failures.clear()

    def snapshot(self) -> list[dict[str, object]]:
        now = self._clock()
        rows = []
        for key, circuit in sorted(self._circuits.items()):
            state = self.state(key)
            rows.append(
                {
                    "key": key,
                    "state": state.value,
                    "recent_failures": len(circuit.failures),
                    "retry_in_seconds": (
                        round(max(0.0, circuit.open_for - (now - circuit.opened_at)), 3)
                        if state == CircuitState.OPEN
                        else 0.0
                    ),
                    "consecutive_opens": circuit.consecutive_opens,
                    "last_error": circuit.last_error,
                }
            )
        return rows

    def reset(self) -> None:
        self._circuits.clear()


def retry_after_seconds(error: Exception) -> float | None:
    value = getattr(error, "retry_after", None)
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def is_transient_error(error: Exception) -> bool:
    """Transient by status (408/429/5xx) or by kind (timeout, connection).

    For SDKs outside the provider layer (google-genai uses ``code``,
    TypeSafe/httpx use ``status_code`` or typed timeout/connection errors);
    provider errors are classified by ``classify_error`` instead.
    """
    import asyncio

    if isinstance(error, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    status = getattr(error, "code", None) or getattr(error, "status_code", None)
    if isinstance(status, int):
        return status in (408, 429) or status >= 500
    name = type(error).__name__.lower()
    return "timeout" in name or "connect" in name
