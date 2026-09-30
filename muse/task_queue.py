import asyncio
from typing import Any

from muse.models import MuseTask, TaskRecord, TaskStatus


class MuseTaskQueue:
    """In-process dependency-aware queue, replaceable by a durable queue later."""

    def __init__(self, tasks: list[MuseTask]):
        self._records = {task.task_id: TaskRecord(task) for task in tasks}
        if len(self._records) != len(tasks):
            raise ValueError("Task IDs must be unique.")
        unknown = {
            dependency
            for task in tasks
            for dependency in task.dependencies
            if dependency not in self._records
        }
        if unknown:
            raise ValueError(f"Tasks have unknown dependencies: {sorted(unknown)}")
        self._lock = asyncio.Lock()

    async def claim_ready(self) -> MuseTask | None:
        async with self._lock:
            for record in self._records.values():
                if record.status is not TaskStatus.PENDING:
                    continue
                dependencies = (self._records[task_id] for task_id in record.task.dependencies)
                if all(dependency.status is TaskStatus.SUCCEEDED for dependency in dependencies):
                    record.status = TaskStatus.RUNNING
                    return record.task
        return None

    async def complete(self, task_id: str, result: Any) -> None:
        async with self._lock:
            record = self._records[task_id]
            if record.status is not TaskStatus.RUNNING:
                raise RuntimeError(f"Task {task_id} is not running.")
            record.status = TaskStatus.SUCCEEDED
            record.result = result

    async def fail(self, task_id: str, error: BaseException) -> None:
        async with self._lock:
            record = self._records[task_id]
            record.status = TaskStatus.FAILED
            record.error = error

    async def has_pending_or_running(self) -> bool:
        async with self._lock:
            return any(
                record.status in {TaskStatus.PENDING, TaskStatus.RUNNING}
                for record in self._records.values()
            )

    async def has_failed(self) -> bool:
        async with self._lock:
            return any(record.status is TaskStatus.FAILED for record in self._records.values())
