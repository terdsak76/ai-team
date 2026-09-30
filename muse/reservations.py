from muse.models import Conflict, ConflictForecast, MuseTask, Reservation


class ReservationManager:
    """Tracks task resource claims and predicts overlapping work."""

    def forecast(self, tasks: list[MuseTask]) -> ConflictForecast:
        conflicts: list[Conflict] = []
        for index, first in enumerate(tasks):
            for second in tasks[index + 1 :]:
                shared = first.resources & second.resources
                if not shared:
                    continue
                conflicts.append(
                    Conflict(
                        task_ids=(first.task_id, second.task_id),
                        resources=frozenset(shared),
                        reason="Tasks claim the same repository resources.",
                    )
                )
        return ConflictForecast(tuple(conflicts))

    def reserve(self, task: MuseTask, active: dict[str, Reservation]) -> Reservation:
        reservation = Reservation(task_id=task.task_id, resources=task.resources)
        for existing in active.values():
            if existing.resources & reservation.resources:
                raise RuntimeError(
                    f"Task {task.task_id} conflicts with active task {existing.task_id}."
                )
        active[task.task_id] = reservation
        return reservation

    def release(self, task_id: str, active: dict[str, Reservation]) -> None:
        active.pop(task_id, None)
