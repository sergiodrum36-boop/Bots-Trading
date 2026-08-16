"""Tests unitarios de utils/retry.py: build_retryer() y CircuitBreaker."""

from __future__ import annotations

import pytest

from utils.retry import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    build_retryer,
)


# ----------------------------------------------------------------------
# build_retryer
# ----------------------------------------------------------------------
class TestBuildRetryer:
    def test_rejects_invalid_max_attempts(self):
        with pytest.raises(ValueError):
            build_retryer(description="x", is_retryable=lambda e: True, max_attempts=0)

    def test_rejects_negative_wait_min(self):
        with pytest.raises(ValueError):
            build_retryer(
                description="x", is_retryable=lambda e: True, wait_min_seconds=-1
            )

    def test_rejects_wait_max_below_wait_min(self):
        with pytest.raises(ValueError):
            build_retryer(
                description="x",
                is_retryable=lambda e: True,
                wait_min_seconds=5,
                wait_max_seconds=1,
            )

    def test_succeeds_after_transient_failures_within_budget(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("transitorio")
            return "ok"

        retryer = build_retryer(
            description="flaky",
            is_retryable=lambda exc: isinstance(exc, ConnectionError),
            max_attempts=5,
            wait_min_seconds=0,
            wait_max_seconds=0,
        )

        assert retryer(flaky) == "ok"
        assert attempts["n"] == 3

    def test_reraises_last_exception_after_exhausting_attempts(self):
        def always_fails():
            raise ConnectionError("sigue caido")

        retryer = build_retryer(
            description="always_fails",
            is_retryable=lambda exc: isinstance(exc, ConnectionError),
            max_attempts=3,
            wait_min_seconds=0,
            wait_max_seconds=0,
        )

        with pytest.raises(ConnectionError, match="sigue caido"):
            retryer(always_fails)

    def test_does_not_retry_non_retryable_exception(self):
        attempts = {"n": 0}

        def fails_with_value_error():
            attempts["n"] += 1
            raise ValueError("no es transitorio")

        retryer = build_retryer(
            description="fails_with_value_error",
            is_retryable=lambda exc: isinstance(exc, ConnectionError),
            max_attempts=5,
            wait_min_seconds=0,
            wait_max_seconds=0,
        )

        with pytest.raises(ValueError):
            retryer(fails_with_value_error)
        assert attempts["n"] == 1


# ----------------------------------------------------------------------
# CircuitBreaker
# ----------------------------------------------------------------------
class _FakeClock:
    """Reloj controlable a mano para tests deterministas (sin sleeps reales)."""

    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestCircuitBreakerConstruction:
    def test_rejects_non_positive_failure_threshold(self):
        with pytest.raises(ValueError):
            CircuitBreaker(failure_threshold=0)

    def test_rejects_non_positive_cooldown(self):
        with pytest.raises(ValueError):
            CircuitBreaker(cooldown_seconds=0)

    def test_starts_closed(self):
        breaker = CircuitBreaker()
        assert breaker.state == CircuitState.CLOSED
        assert breaker.consecutive_failures == 0


class TestCircuitBreakerBehaviour:
    def test_success_keeps_circuit_closed(self):
        breaker = CircuitBreaker(failure_threshold=3)
        result = breaker.call(lambda: "ok")
        assert result == "ok"
        assert breaker.state == CircuitState.CLOSED

    def test_opens_after_reaching_failure_threshold(self):
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60, clock=clock)

        def boom():
            raise ConnectionError("caido")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                breaker.call(boom)
        assert breaker.state == CircuitState.CLOSED  # todavia no llega al umbral

        with pytest.raises(ConnectionError):
            breaker.call(boom)
        assert breaker.state == CircuitState.OPEN

    def test_rejects_calls_without_invoking_func_while_open(self):
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60, clock=clock)
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise ConnectionError("caido")

        with pytest.raises(ConnectionError):
            breaker.call(boom)
        assert breaker.state == CircuitState.OPEN
        assert calls["n"] == 1

        with pytest.raises(CircuitBreakerOpenError):
            breaker.call(boom)
        assert calls["n"] == 1  # no se llego a invocar boom() la segunda vez

    def test_transitions_to_half_open_after_cooldown_and_closes_on_success(self):
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60, clock=clock)

        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("caido")))
        assert breaker.state == CircuitState.OPEN

        clock.advance(60)
        assert breaker.state == CircuitState.HALF_OPEN

        result = breaker.call(lambda: "recuperado")
        assert result == "recuperado"
        assert breaker.state == CircuitState.CLOSED
        assert breaker.consecutive_failures == 0

    def test_half_open_trial_failure_reopens_and_resets_cooldown(self):
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60, clock=clock)

        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("caido")))
        clock.advance(60)
        assert breaker.state == CircuitState.HALF_OPEN

        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("sigue caido")))
        assert breaker.state == CircuitState.OPEN

        # El cooldown se reinicio: justo tras el segundo fallo, todavia no
        # ha pasado (aunque ya hubieran pasado los 60s originales).
        assert breaker.state == CircuitState.OPEN
        clock.advance(60)
        assert breaker.state == CircuitState.HALF_OPEN

    def test_success_after_isolated_failures_resets_consecutive_count(self):
        """Un fallo aislado no debe acumularse indefinidamente si luego hay
        exito -- solo fallos CONSECUTIVOS cuentan para el umbral."""
        breaker = CircuitBreaker(failure_threshold=2)

        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("caido")))
        assert breaker.consecutive_failures == 1

        breaker.call(lambda: "ok")
        assert breaker.consecutive_failures == 0
        assert breaker.state == CircuitState.CLOSED

        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("caido")))
        assert breaker.state == CircuitState.CLOSED  # todavia 1, no llega a 2

    def test_manual_reset_forces_closed_state(self):
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60, clock=clock)

        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("caido")))
        assert breaker.state == CircuitState.OPEN

        breaker.reset()
        assert breaker.state == CircuitState.CLOSED
        assert breaker.consecutive_failures == 0

    def test_open_error_message_includes_name_and_failure_count(self):
        clock = _FakeClock()
        breaker = CircuitBreaker(
            failure_threshold=1, cooldown_seconds=60, clock=clock, name="alpaca"
        )
        with pytest.raises(ConnectionError):
            breaker.call(lambda: (_ for _ in ()).throw(ConnectionError("caido")))

        with pytest.raises(CircuitBreakerOpenError, match="alpaca"):
            breaker.call(lambda: "no deberia ejecutarse")
