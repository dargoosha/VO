from __future__ import annotations

import threading
import sys
import types
import unittest
from unittest.mock import patch

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    httpx_stub = types.ModuleType("httpx")
    httpx_stub.Client = object
    sys.modules["httpx"] = httpx_stub

try:
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    psycopg_stub = types.ModuleType("psycopg")
    psycopg_stub.connect = lambda **_kwargs: None
    rows_stub = types.ModuleType("psycopg.rows")
    rows_stub.dict_row = object()
    sys.modules["psycopg"] = psycopg_stub
    sys.modules["psycopg.rows"] = rows_stub

try:
    import prometheus_client  # noqa: F401
except ModuleNotFoundError:
    class MetricStub:
        def labels(self, *_args: object) -> "MetricStub":
            return self

        def inc(self, *_args: object) -> None:
            return None

        def observe(self, *_args: object) -> None:
            return None

        def set(self, *_args: object) -> None:
            return None

    prometheus_stub = types.ModuleType("prometheus_client")
    prometheus_stub.Counter = lambda *_args, **_kwargs: MetricStub()
    prometheus_stub.Gauge = lambda *_args, **_kwargs: MetricStub()
    prometheus_stub.Histogram = lambda *_args, **_kwargs: MetricStub()
    prometheus_stub.start_http_server = lambda *_args, **_kwargs: None
    sys.modules["prometheus_client"] = prometheus_stub

from worker.main import Worker


class FakeCursor:
    rowcount = 1

    def __init__(self, calls: list[tuple[str, tuple[int, int]]]) -> None:
        self.calls = calls

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, values: tuple[int, int]) -> None:
        self.calls.append((query, values))


class FakeConnection:
    def __init__(self, calls: list[tuple[str, tuple[int, int]]]) -> None:
        self.calls = calls

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls)


class ShutdownWatchdogTest(unittest.TestCase):
    def test_timeout_returns_held_task_by_token_and_exits_zero(self) -> None:
        worker = object.__new__(Worker)
        worker.shutdown_complete = threading.Event()
        worker.shutdown_timeout_seconds = 0.001
        worker.held_lock = threading.Lock()
        worker.held = {17: 4}
        events: list[tuple[str, dict[str, object]]] = []
        worker.log = lambda event, **values: events.append((event, values))
        calls: list[tuple[str, tuple[int, int]]] = []

        with (
            patch("worker.main.psycopg.connect", return_value=FakeConnection(calls)),
            patch("worker.main.os._exit") as exit_process,
            patch.dict(
                "worker.main.os.environ",
                {"POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test", "POSTGRES_DB": "test"},
            ),
        ):
            worker.shutdown_watchdog()

        self.assertEqual(calls[0][1], (17, 4))
        self.assertIn("AND token=%s", calls[0][0])
        self.assertEqual(events[-1][0], "graceful_timeout")
        self.assertEqual(events[-1][1]["returned_to_queue"], 1)
        exit_process.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
