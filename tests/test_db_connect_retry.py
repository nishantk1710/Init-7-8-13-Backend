"""Opening a database connection is retried when it fails transiently -- and
nothing else is.

The error that prompted this: ``('08001', '[08001] [Microsoft][ODBC Driver 18
for SQL Server]TCP Provider: Error code 0x68 (104) (SQLDriverConnect)')``, a
connection reset while *opening* a connection, which killed a staging run on
13 Oct 2026. Only the connect is wrapped -- no SQL has run, no transaction
exists -- so a retry cannot repeat a write. No database is needed: the connect
is a fake that fails on demand.
"""

from __future__ import annotations

import pytest

from app.core.db import connect_with_retry, is_transient_connect_error

TCP_RESET = ("08001", "[08001] [Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: Error code 0x68 (104) (SQLDriverConnect)")


class DriverError(Exception):
    """Stands in for pyodbc.OperationalError: the SQLSTATE is args[0]."""


class FlakyConnect:
    def __init__(self, failures: list[Exception], result: str = "connection") -> None:
        self.failures = list(failures)
        self.result = result
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.result


class Clock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def test_a_connection_that_works_is_opened_once_without_waiting() -> None:
    connect, clock = FlakyConnect([]), Clock()
    assert connect_with_retry(connect, retries=2, sleep=clock) == "connection"
    assert (connect.calls, clock.sleeps) == (1, [])


def test_the_reported_tcp_reset_is_retried_and_then_succeeds() -> None:
    connect, clock = FlakyConnect([DriverError(*TCP_RESET)]), Clock()
    assert connect_with_retry(connect, retries=2, sleep=clock) == "connection"
    assert (connect.calls, clock.sleeps) == (2, [2.0])


def test_backoff_grows_and_the_retries_are_bounded() -> None:
    errors = [DriverError(*TCP_RESET) for _ in range(3)]
    connect, clock = FlakyConnect(errors), Clock()
    with pytest.raises(DriverError) as raised:
        connect_with_retry(connect, retries=2, sleep=clock)
    assert connect.calls == 3  # the first try plus two retries, then it gives up
    assert clock.sleeps == [2.0, 4.0]
    assert raised.value is errors[-1]  # the original exception, not a wrapper


@pytest.mark.parametrize("state", ["08001", "08S01", "HYT00"])
def test_each_transient_sqlstate_is_retried(state) -> None:
    connect = FlakyConnect([DriverError(state, "x")])
    assert connect_with_retry(connect, retries=1, sleep=Clock()) == "connection"
    assert connect.calls == 2


@pytest.mark.parametrize(
    "error",
    [
        DriverError("28000", "Login failed for user"),       # wrong credentials
        DriverError("42000", "Cannot open database"),          # permanent
        DriverError("42S02", "Invalid object name"),           # a programming error
        ValueError("not a driver error at all"),
        DriverError(),                                         # no SQLSTATE
    ],
)
def test_permanent_errors_fail_at_once_without_a_retry(error) -> None:
    connect, clock = FlakyConnect([error]), Clock()
    with pytest.raises(type(error)) as raised:
        connect_with_retry(connect, retries=2, sleep=clock)
    assert (connect.calls, clock.sleeps) == (1, [])
    assert raised.value is error


def test_retries_zero_turns_it_off() -> None:
    connect, clock = FlakyConnect([DriverError(*TCP_RESET)]), Clock()
    with pytest.raises(DriverError):
        connect_with_retry(connect, retries=0, sleep=clock)
    assert (connect.calls, clock.sleeps) == (1, [])


def test_a_transient_error_then_a_permanent_one_stops_on_the_permanent_one() -> None:
    permanent = DriverError("28000", "Login failed")
    connect = FlakyConnect([DriverError(*TCP_RESET), permanent])
    with pytest.raises(DriverError) as raised:
        connect_with_retry(connect, retries=3, sleep=Clock())
    assert raised.value is permanent and connect.calls == 2


def test_classification() -> None:
    assert is_transient_connect_error(DriverError(*TCP_RESET))
    assert not is_transient_connect_error(DriverError("28000", "x"))
    assert not is_transient_connect_error(RuntimeError())


def test_the_setting_defaults_on_with_two_retries() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None).database_connect_retries == 2
