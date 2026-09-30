from muse.models import MuseTask


class TaskDAG:
    def __init__(self, tasks: list[MuseTask]):
        self.tasks = tuple(tasks)
        self._validate_acyclic()

    def _validate_acyclic(self) -> None:
        task_map = {task.task_id: task for task in self.tasks}
        if len(task_map) != len(self.tasks):
            raise ValueError("Task IDs must be unique.")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("Task DAG contains a cycle.")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in task_map[task_id].dependencies:
                if dependency not in task_map:
                    raise ValueError(f"Task {task_id} has unknown dependency {dependency}.")
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task in self.tasks:
            visit(task.task_id)
