from __future__ import annotations

import json
import math
import os
import random
import signal
import threading
import time
from dataclasses import dataclass

import httpx
import psycopg
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from psycopg.rows import dict_row


WORKER_TASKS = Counter("worker_tasks_total", "Worker task outcomes", ["result"])
WORKER_CLAIMS = Counter("worker_claims_total", "Queue claim outcomes", ["result"])
WORKER_DURATION = Histogram(
    "worker_task_duration_seconds",
    "Worker step duration",
    ["step"],
    buckets=(.0005, .001, .002, .005, .01, .02, .05, .1, .2, .5, 1, 2, 5, 10),
)
WORKER_IN_FLIGHT = Gauge("worker_tasks_in_flight", "Tasks currently owned by this worker")


@dataclass(frozen=True)
class Task:
    id: int
    record_id: int
    seed: int
    token: int


class LostOwnership(Exception):
    pass


class Worker:
    def __init__(self) -> None:
        self.worker_id = os.environ["WORKER_ID"]
        self.web_url = os.getenv("WEB_URL", "http://web").rstrip("/")
        self.poll_seconds = float(os.getenv("T_POLL_MS", "100")) / 1000
        self.lease_seconds = int(os.getenv("LEASE_SECONDS", "15"))
        self.fail_rate = float(os.getenv("FAIL_RATE", "0"))
        self.claim_mode = os.getenv("CLAIM_MODE", "locked")
        self.batch_size = int(os.getenv("CLAIM_BATCH_SIZE", "1"))
        self.heartbeat_enabled = os.getenv("HEARTBEAT_ENABLED", "true").lower() == "true"
        self.poison_seed_zero = os.getenv("POISON_SEED_ZERO", "true").lower() == "true"
        self.stop_event = threading.Event()
        self.held_lock = threading.Lock()
        self.held: dict[int, int] = {}
        self.db = psycopg.connect(
            host=os.getenv("DB_HOST", "postgres"),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=os.environ["POSTGRES_DB"],
            autocommit=True,
            row_factory=dict_row,
        )
        self.http = httpx.Client(base_url=self.web_url, timeout=10)

    def log(self, event: str, **values) -> None:
        print(json.dumps({"event": event, "worker_id": self.worker_id, **values}, ensure_ascii=False), flush=True)

    def on_signal(self, signum, _frame) -> None:
        self.log("sigterm", signal=signum)
        self.stop_event.set()

    def reconnect(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass
        self.db = psycopg.connect(
            host=os.getenv("DB_HOST", "postgres"),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            dbname=os.environ["POSTGRES_DB"],
            autocommit=True,
            row_factory=dict_row,
        )
        self.log("db_reconnected")

    def reap_expired(self) -> None:
        with self.db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tasks
                SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
                    worker_id = NULL,
                    lease_until = NULL,
                    finished_at = CASE WHEN attempts >= max_attempts THEN clock_timestamp() ELSE NULL END
                WHERE status = 'running' AND lease_until < clock_timestamp()
                RETURNING id, status, attempts
                """
            )
            for row in cursor.fetchall():
                self.log("lease_expired", **row)

    def claim(self) -> list[Task]:
        started = time.perf_counter()
        try:
            self.reap_expired()
            if self.claim_mode == "naive":
                with self.db.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM tasks WHERE status='queued' ORDER BY created_at, id LIMIT 1"
                    )
                    selected = cursor.fetchone()
                if not selected:
                    rows = []
                else:
                    time.sleep(0.002)
                    with self.db.cursor() as cursor:
                        cursor.execute(
                            """
                            UPDATE tasks SET status='running', worker_id=%s, token=token+1,
                              lease_until=clock_timestamp()+(%s * interval '1 second'),
                              attempts=attempts+1, started_at=clock_timestamp()
                            WHERE id=%s RETURNING id, record_id, seed, token
                            """,
                            (self.worker_id, self.lease_seconds, selected["id"]),
                        )
                        rows = cursor.fetchall()
            else:
                skip = "SKIP LOCKED" if self.claim_mode == "locked" else ""
                query = f"""
                    WITH candidates AS (
                      SELECT id FROM tasks WHERE status='queued'
                      ORDER BY created_at, id LIMIT %s FOR UPDATE {skip}
                    )
                    UPDATE tasks t SET status='running', worker_id=%s, token=t.token+1,
                      lease_until=clock_timestamp()+(%s * interval '1 second'),
                      attempts=t.attempts+1, started_at=clock_timestamp()
                    FROM candidates c WHERE t.id=c.id
                    RETURNING t.id, t.record_id, t.seed, t.token
                """
                with self.db.transaction():
                    with self.db.cursor() as cursor:
                        cursor.execute(query, (self.batch_size, self.worker_id, self.lease_seconds))
                        rows = cursor.fetchall()
            tasks = [Task(**row) for row in sorted(rows, key=lambda item: item["id"])]
            WORKER_CLAIMS.labels("task" if tasks else "empty").inc(max(1, len(tasks)))
            with self.held_lock:
                self.held.update({task.id: task.token for task in tasks})
                WORKER_IN_FLIGHT.set(len(self.held))
            return tasks
        finally:
            WORKER_DURATION.labels("claim").observe(time.perf_counter() - started)

    def heartbeat(self) -> None:
        while not self.stop_event.wait(self.lease_seconds / 3):
            if not self.heartbeat_enabled:
                continue
            with self.held_lock:
                items = list(self.held.items())
            for task_id, token in items:
                try:
                    with self.db.cursor() as cursor:
                        cursor.execute(
                            """
                            UPDATE tasks SET lease_until=clock_timestamp()+(%s * interval '1 second')
                            WHERE id=%s AND token=%s AND status='running'
                            """,
                            (self.lease_seconds, task_id, token),
                        )
                        if cursor.rowcount == 0:
                            self.log("heartbeat_lost", task_id=task_id, token=token, updated_rows=0)
                except Exception as exc:
                    self.log("heartbeat_error", task_id=task_id, error=type(exc).__name__)

    @staticmethod
    def process(seed: int) -> dict:
        if seed == 0 and os.getenv("POISON_SEED_ZERO", "true").lower() == "true":
            raise RuntimeError("poison seed 0")
        size = 256 * 256
        state = seed
        pixels: list[int] = []
        histogram = [0] * 4096
        for _ in range(size):
            state = (1103515245 * state + 12345) % (2**31)
            value = state >> 19
            pixels.append(value)
            histogram[value] += 1
        def quantile_level(q: float) -> int:
            target = math.ceil(q * size)
            seen = 0
            for level, count in enumerate(histogram):
                seen += count
                if seen >= target:
                    return level
            return 4095
        p_lo, p_hi = quantile_level(0.01), quantile_level(0.99)
        kept, removed, total, total_sq = 0, 0, 0, 0
        denominator = max(1, p_hi - p_lo)
        for value in pixels:
            if value < p_lo or value > p_hi:
                removed += 1
                continue
            transformed = (4095 * (value - p_lo) + denominator // 2) // denominator
            kept += 1
            total += transformed
            total_sq += transformed * transformed
        mean = total / kept
        variance = max(0.0, total_sq / kept - mean * mean)
        return {"p_lo": p_lo, "p_hi": p_hi, "removed": removed,
                "mean": mean, "stddev": math.sqrt(variance)}

    def finish(self, task: Task, result: dict) -> bool:
        started = time.perf_counter()
        try:
            with self.db.transaction():
                with self.db.cursor() as cursor:
                    if self.claim_mode == "naive":
                        cursor.execute(
                            "INSERT INTO task_results(task_id,token,worker_id,result) VALUES(%s,%s,%s,%s::jsonb)",
                            (task.id, task.token, self.worker_id, json.dumps(result)),
                        )
                        cursor.execute(
                            "UPDATE tasks SET status='done', finished_at=clock_timestamp() WHERE id=%s",
                            (task.id,),
                        )
                        return True
                    cursor.execute(
                        """
                        UPDATE tasks SET status='done', finished_at=clock_timestamp(), lease_until=NULL
                        WHERE id=%s AND token=%s AND status='running'
                        """,
                        (task.id, task.token),
                    )
                    if cursor.rowcount == 0:
                        raise LostOwnership()
                    cursor.execute(
                        """
                        INSERT INTO task_results(task_id,token,worker_id,result)
                        VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT(task_id) DO NOTHING
                        """,
                        (task.id, task.token, self.worker_id, json.dumps(result)),
                    )
            return True
        except LostOwnership:
            self.log("fencing_rejected", task_id=task.id, token=task.token, updated_rows=0)
            return False
        finally:
            WORKER_DURATION.labels("store").observe(time.perf_counter() - started)

    def release_held(self) -> int:
        released = 0
        with self.held_lock:
            items = list(self.held.items())
        for task_id, token in items:
            with self.db.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE tasks SET status='queued', worker_id=NULL, lease_until=NULL
                    WHERE id=%s AND token=%s AND status='running'
                    """,
                    (task_id, token),
                )
                released += cursor.rowcount
        self.log("graceful_release", returned_to_queue=released)
        return released

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        start_http_server(9000)
        threading.Thread(target=self.heartbeat, daemon=True).start()
        self.log("worker_started", claim_mode=self.claim_mode, batch_size=self.batch_size)
        try:
            while not self.stop_event.is_set():
                try:
                    tasks = self.claim()
                except Exception as exc:
                    self.log("claim_error", error=type(exc).__name__, message=str(exc))
                    time.sleep(self.poll_seconds)
                    try:
                        self.reconnect()
                    except Exception:
                        pass
                    continue
                if not tasks:
                    self.stop_event.wait(self.poll_seconds)
                    continue
                for task in tasks:
                    if self.stop_event.is_set():
                        break
                    try:
                        started = time.perf_counter()
                        response = self.http.get(f"/prescriptions/{task.record_id}")
                        response.raise_for_status()
                        WORKER_DURATION.labels("fetch").observe(time.perf_counter() - started)
                        started = time.perf_counter()
                        result = self.process(task.seed)
                        WORKER_DURATION.labels("process").observe(time.perf_counter() - started)
                        if random.random() < self.fail_rate:
                            self.log("injected_exit", task_id=task.id, token=task.token)
                            os._exit(1)
                        if self.finish(task, result):
                            WORKER_TASKS.labels("done").inc()
                            self.log("task_done", task_id=task.id, token=task.token)
                        else:
                            WORKER_TASKS.labels("lost").inc()
                    except Exception as exc:
                        WORKER_TASKS.labels("failed").inc()
                        self.log("task_error", task_id=task.id, token=task.token,
                                 error=type(exc).__name__, message=str(exc))
                    finally:
                        with self.held_lock:
                            self.held.pop(task.id, None)
                            WORKER_IN_FLIGHT.set(len(self.held))
        finally:
            self.release_held()
            self.http.close()
            self.db.close()
            self.log("worker_stopped", exit_code=0)


if __name__ == "__main__":
    Worker().run()
