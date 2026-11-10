import asyncio
import os
import secrets
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from prometheus_client import Gauge
from sqlalchemy import text
from sqlalchemy.engine import Engine


TASKS_QUEUED = Gauge("tasks_queued", "Tasks currently waiting in the database queue")
TASKS_RUNNING = Gauge("tasks_running", "Tasks currently owned by workers")
VALID_STATUSES = {"queued", "running", "done", "failed"}


def _task_dict(row) -> dict:
    data = dict(row._mapping)
    for key in ("created_at", "started_at", "finished_at", "lease_until"):
        if isinstance(data.get(key), datetime):
            data[key] = data[key].isoformat()
    return data


def build_task_router(engine: Engine) -> APIRouter:
    router = APIRouter()

    @router.post("/prescriptions/{record_id}/tasks", status_code=202)
    def create_task(
        record_id: int,
        seed: int | None = Query(default=None, ge=0, le=2**31 - 1),
    ):
        if seed is not None and os.getenv("ALLOW_TASK_SEED_OVERRIDE", "false").lower() != "true":
            raise HTTPException(400, "Seed override is disabled")
        generated_seed = seed if seed is not None else secrets.randbelow(2**31)
        with engine.begin() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM prescriptions WHERE id = :record_id"),
                {"record_id": record_id},
            )
            if exists is None:
                raise HTTPException(404, "Prescription not found")
            row = connection.execute(
                text(
                    """
                    INSERT INTO tasks (record_id, seed)
                    VALUES (:record_id, :seed)
                    RETURNING *
                    """
                ),
                {"record_id": record_id, "seed": generated_seed},
            ).one()
        return _task_dict(row)

    @router.get("/tasks/{task_id}")
    def get_task(task_id: int):
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT t.*, r.result, r.worker_id AS result_worker_id,
                           r.token AS result_token, r.created_at AS result_created_at
                    FROM tasks t
                    LEFT JOIN task_results r ON r.task_id = t.id
                    WHERE t.id = :task_id
                    ORDER BY r.created_at
                    LIMIT 1
                    """
                ),
                {"task_id": task_id},
            ).first()
        if row is None:
            raise HTTPException(404, "Task not found")
        return _task_dict(row)

    @router.get("/tasks")
    def list_tasks(
        status: str | None = None,
        limit: int = Query(20, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        if status is not None and status not in VALID_STATUSES:
            raise HTTPException(400, "Unknown task status")
        condition = "WHERE status = :status" if status else ""
        params = {"status": status, "limit": limit, "offset": offset}
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"""
                    SELECT * FROM tasks {condition}
                    ORDER BY created_at, id
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).all()
            total = connection.scalar(
                text(f"SELECT count(*) FROM tasks {condition}"), params
            )
        return {"items": [_task_dict(row) for row in rows], "total": total}

    return router


async def update_task_metrics(engine: Engine, stop_event: asyncio.Event) -> None:
    def refresh() -> None:
        with engine.connect() as connection:
            counts = dict(
                connection.execute(
                    text(
                        "SELECT status, count(*) FROM tasks "
                        "WHERE status IN ('queued', 'running') GROUP BY status"
                    )
                ).all()
            )
        TASKS_QUEUED.set(counts.get("queued", 0))
        TASKS_RUNNING.set(counts.get("running", 0))

    while not stop_event.is_set():
        try:
            await asyncio.to_thread(refresh)
        except Exception:
            TASKS_QUEUED.set(float("nan"))
            TASKS_RUNNING.set(float("nan"))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5)
        except TimeoutError:
            pass
