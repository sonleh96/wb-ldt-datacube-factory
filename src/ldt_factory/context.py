from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .config import FactoryConfig


@dataclass(frozen=True)
class RunContext:
    config: FactoryConfig
    run_id: str
    resume: bool = False
    force: bool = False

    @property
    def log_dir(self) -> Path:
        return self.config.workspace / "logs" / self.run_id

    @property
    def state_dir(self) -> Path:
        path = self.config.workspace / "state"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def require_env(self, variable: str) -> str:
        value = os.environ.get(variable)
        if not value:
            raise RuntimeError(f"Required environment variable is not set: {variable}")
        return value

    def output(self, *parts: str) -> Path:
        path = self.config.dataset_dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def raw(self, *parts: str) -> Path:
        path = self.config.raw_dir.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
