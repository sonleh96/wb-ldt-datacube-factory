from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ConfigError, FactoryConfig
from .task_state import TaskKey


DEFAULT_LIMITS = {
    "heavy_memory": 2,
    "disk_io": 2,
    "earth_engine": 1,
    "rate_limited_api": 1,
}

DEFAULT_TASK_RESOURCES: dict[str, dict[str, int]] = {
    "web.osm": {"disk_io": 1},
    "web.climate_trace": {"disk_io": 1},
    "web.population": {"disk_io": 1},
    "prerequisite.key_assets": {"disk_io": 1},
    "prerequisite.transport": {"heavy_memory": 1, "disk_io": 1},
    "prerequisite.population": {"disk_io": 1},
    "domain.flood.extract": {"earth_engine": 1, "disk_io": 1},
    "domain.land_cover.extract": {"earth_engine": 1, "disk_io": 1},
    "domain.luminosity.extract": {"earth_engine": 1, "disk_io": 1},
    "domain.air_pollution.extract": {"rate_limited_api": 1, "disk_io": 1},
    "domain.heatwaves.extract": {"heavy_memory": 1, "disk_io": 1},
    "domain.flood.process": {"heavy_memory": 1, "disk_io": 1},
    "domain.land_cover.process": {"heavy_memory": 1, "disk_io": 1},
    "domain.luminosity.process": {"disk_io": 1},
    "domain.air_pollution.process": {"heavy_memory": 1, "disk_io": 1},
    "domain.emissions.process": {"disk_io": 1},
    "domain.heatwaves.process": {"heavy_memory": 1, "disk_io": 1},
    "domain.internet.process": {"heavy_memory": 1, "disk_io": 1},
    "domain.tourism.process": {"heavy_memory": 1, "disk_io": 1},
    "domain.accessibility.extract": {"rate_limited_api": 1, "disk_io": 1},
    "domain.accessibility.process": {"heavy_memory": 1, "disk_io": 1},
    "combine.indicators": {"disk_io": 1},
}


def _normalize_requirements(value: Any, *, task_id: str) -> dict[str, int]:
    if value is None:
        return {}
    if isinstance(value, str):
        return {value: 1}
    if isinstance(value, list):
        result: dict[str, int] = {}
        for item in value:
            name = str(item)
            result[name] = result.get(name, 0) + 1
        return result
    if isinstance(value, dict):
        result = {}
        for name, amount in value.items():
            try:
                parsed = int(amount)
            except (TypeError, ValueError) as error:
                raise ConfigError(f"pipeline.task_resources.{task_id}.{name} must be an integer") from error
            if parsed < 1:
                raise ConfigError(f"pipeline.task_resources.{task_id}.{name} must be at least 1")
            result[str(name)] = parsed
        return result
    raise ConfigError(f"pipeline.task_resources.{task_id} must be a string, list, or mapping")


@dataclass(frozen=True)
class ResourcePolicy:
    max_parallel: int
    worker_threads: int
    limits: dict[str, int]
    task_resources: dict[str, dict[str, int]]

    @classmethod
    def from_config(cls, config: FactoryConfig) -> "ResourcePolicy":
        pipeline = config.data.get("pipeline", {})
        try:
            max_parallel = int(pipeline.get("max_parallel", 4))
        except (TypeError, ValueError) as error:
            raise ConfigError("pipeline.max_parallel must be an integer") from error
        if max_parallel < 1:
            raise ConfigError("pipeline.max_parallel must be at least 1")
        try:
            worker_threads = int(pipeline.get("worker_threads", 1))
        except (TypeError, ValueError) as error:
            raise ConfigError("pipeline.worker_threads must be an integer") from error
        if worker_threads < 1:
            raise ConfigError("pipeline.worker_threads must be at least 1")

        limits = dict(DEFAULT_LIMITS)
        configured_limits = pipeline.get("resource_limits", {})
        if not isinstance(configured_limits, dict):
            raise ConfigError("pipeline.resource_limits must be a mapping")
        for name, value in configured_limits.items():
            try:
                amount = int(value)
            except (TypeError, ValueError) as error:
                raise ConfigError(f"pipeline.resource_limits.{name} must be an integer") from error
            if amount < 1:
                raise ConfigError(f"pipeline.resource_limits.{name} must be at least 1")
            limits[str(name)] = amount

        task_resources = {name: dict(values) for name, values in DEFAULT_TASK_RESOURCES.items()}
        configured_tasks = pipeline.get("task_resources", {})
        if not isinstance(configured_tasks, dict):
            raise ConfigError("pipeline.task_resources must be a mapping")
        for task_id, requirements in configured_tasks.items():
            task_resources[str(task_id)] = _normalize_requirements(requirements, task_id=str(task_id))

        for task_id, requirements in task_resources.items():
            for resource, amount in requirements.items():
                if resource not in limits:
                    raise ConfigError(
                        f"pipeline task {task_id!r} requires undefined resource {resource!r}; "
                        "add it to pipeline.resource_limits"
                    )
                if amount > limits[resource]:
                    raise ConfigError(
                        f"pipeline task {task_id!r} requires {amount} {resource!r} slots, "
                        f"but the configured limit is {limits[resource]}"
                    )
        return cls(max_parallel, worker_threads, limits, task_resources)

    def requirements(self, task: TaskKey) -> dict[str, int]:
        exact = self.task_resources.get(task.id)
        if exact is not None:
            return dict(exact)
        return dict(self.task_resources.get(f"{task.kind}.{task.name}", {}))

    def can_reserve(self, task: TaskKey, used: dict[str, int]) -> bool:
        return all(used.get(resource, 0) + amount <= self.limits[resource] for resource, amount in self.requirements(task).items())

    def reserve(self, task: TaskKey, used: dict[str, int]) -> None:
        for resource, amount in self.requirements(task).items():
            used[resource] = used.get(resource, 0) + amount

    def release(self, task: TaskKey, used: dict[str, int]) -> None:
        for resource, amount in self.requirements(task).items():
            remaining = used.get(resource, 0) - amount
            if remaining:
                used[resource] = remaining
            else:
                used.pop(resource, None)
