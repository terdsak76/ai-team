from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Awaitable, Callable


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class MuseTask:
    """A unit of work managed by the Muse queue."""

    task_id: str
    name: str
    dependencies: frozenset[str] = frozenset()
    resources: frozenset[str] = frozenset()


@dataclass
class TaskRecord:
    task: MuseTask
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: BaseException | None = None


@dataclass(frozen=True)
class Reservation:
    task_id: str
    resources: frozenset[str]


@dataclass(frozen=True)
class Conflict:
    task_ids: tuple[str, str]
    resources: frozenset[str]
    reason: str


@dataclass(frozen=True)
class ConflictForecast:
    conflicts: tuple[Conflict, ...] = ()

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


TaskRunner = Callable[[MuseTask], Awaitable[Any]]
