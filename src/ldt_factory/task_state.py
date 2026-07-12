from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import traceback
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import FactoryConfig
from .context import RunContext


STATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TaskKey:
    kind: str
    name: str
    phase: str | None = None

    @property
    def id(self) -> str:
        parts = (self.kind, self.name, self.phase)
        return ".".join(str(part) for part in parts if part)

    @property
    def filename(self) -> str:
        safe = "".join(character if character.isalnum() or character in "-_." else "_" for character in self.id)
        return f"{safe}.json"


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    status: str
    fingerprint: str
    elapsed_seconds: float
    artifacts: tuple[dict[str, Any], ...] = ()
    error_type: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifacts"] = list(self.artifacts)
        return payload


_DEPENDENCIES: dict[tuple[str, str, str | None], tuple[TaskKey, ...]] = {
    ("prerequisite", "key_assets", None): (TaskKey("web", "osm"),),
    ("prerequisite", "transport", None): (TaskKey("web", "osm"),),
    ("prerequisite", "population", None): (TaskKey("web", "population"),),
    ("domain", "flood", "process"): (
        TaskKey("domain", "flood", "extract"),
        TaskKey("prerequisite", "transport"),
    ),
    ("domain", "heatwaves", "process"): (
        TaskKey("domain", "heatwaves", "extract"),
        TaskKey("prerequisite", "transport"),
    ),
    ("domain", "internet", "process"): (
        TaskKey("domain", "internet", "extract"),
        TaskKey("prerequisite", "key_assets"),
    ),
    ("domain", "tourism", "process"): (
        TaskKey("domain", "tourism", "extract"),
        TaskKey("web", "osm"),
    ),
    ("domain", "emissions", "process"): (
        TaskKey("domain", "emissions", "extract"),
        TaskKey("web", "climate_trace"),
    ),
    ("domain", "accessibility", "extract"): (TaskKey("prerequisite", "key_assets"),),
    ("domain", "accessibility", "process"): (
        TaskKey("domain", "accessibility", "extract"),
        TaskKey("prerequisite", "population"),
    ),
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def redact_sensitive(config: FactoryConfig, value: object) -> str:
    text = str(value)

    def visit(item: Any) -> Iterable[str]:
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, str) and str(key).lower().endswith("_env"):
                    secret = os.environ.get(child)
                    if secret:
                        yield secret
                yield from visit(child)
        elif isinstance(item, list):
            for child in item:
                yield from visit(child)

    for secret in sorted(set(visit(config.data)), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return text


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _file_record(path: Path, root: Path | None = None) -> dict[str, Any]:
    stat = path.stat()
    try:
        name = str(path.relative_to(root)) if root else str(path)
    except ValueError:
        name = str(path)
    return {"path": name.replace("\\", "/"), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _iter_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
    elif path.is_dir():
        yield from (candidate for candidate in path.rglob("*") if candidate.is_file())


def _shapefile_family(path: Path) -> Iterable[Path]:
    if path.suffix.lower() != ".shp":
        yield from _iter_files(path)
        return
    yield from (candidate for candidate in path.parent.glob(f"{path.stem}.*") if candidate.is_file())


def _input_paths(config: FactoryConfig, task: TaskKey) -> list[Path]:
    paths: list[Path] = []
    if task.kind != "web":
        for level in ("admin0", "admin1", "admin2"):
            paths.extend(_shapefile_family(config.boundary_path(level)))

    if task.kind == "prerequisite":
        if task.name in {"key_assets", "transport"}:
            extracted_dir = config.source("osm").get("extracted_dir")
            if extracted_dir:
                paths.extend(_iter_files(config.raw_dir / str(extracted_dir)))
        elif task.name == "population":
            paths.extend(_iter_files(config.raw_dir / "population"))
    elif task.kind == "domain" and task.phase == "process":
        raw_inputs: dict[str, tuple[str, ...]] = {
            "accessibility": ("population", "accessibility"),
            "air_pollution": ("air_pollution",),
            "flood": ("flood",),
            "heatwaves": ("heatwaves",),
            "land_cover": ("land_cover",),
            "luminosity": ("luminosity",),
        }
        for part in raw_inputs.get(task.name, ()):
            candidates = _iter_files(config.raw_dir / part)
            if task.name == "air_pollution":
                candidates = (
                    candidate
                    for candidate in candidates
                    if candidate.name not in {"grid_admin2.csv", "grid_admin2.meta.json"}
                )
            paths.extend(candidates)
        if task.name == "emissions":
            paths.extend(_iter_files(config.raw_dir / f"climate_trace_{config.iso3}_CO2"))
            paths.extend(_iter_files(config.raw_dir / f"climate_trace_{config.iso3}_CH4"))
        if task.name == "tourism":
            extracted_dir = config.source("osm").get("extracted_dir")
            if extracted_dir:
                paths.extend(_iter_files(config.raw_dir / str(extracted_dir)))
        if task.name == "internet":
            dataset_root = config.source("internet").get("dataset_root")
            if dataset_root:
                paths.extend(_iter_files(Path(str(dataset_root)).expanduser().resolve()))
        shape_patterns: dict[str, tuple[str, ...]] = {
            "accessibility": ("assets.*",),
            "flood": ("roads_intersect_*", "rails_intersect_*"),
            "heatwaves": ("roads_intersect_*", "rails_intersect_*"),
            "internet": ("assets.*",),
        }
        for pattern in shape_patterns.get(task.name, ()):
            paths.extend(candidate for candidate in config.shape_dir.glob(pattern) if candidate.is_file())
    elif task.kind == "domain" and task.phase == "extract":
        source = config.source(task.name)
        for key, raw_value in source.items():
            if not isinstance(raw_value, str) or not any(token in key.lower() for token in ("path", "root", "glob", "dir")):
                continue
            if any(character in raw_value for character in "*?["):
                import glob

                paths.extend(Path(match).resolve() for match in glob.glob(raw_value))
            else:
                paths.extend(_iter_files(Path(raw_value).expanduser().resolve()))
        if task.name == "emissions":
            paths.extend(_iter_files(config.raw_dir / f"climate_trace_{config.iso3}_CO2"))
            paths.extend(_iter_files(config.raw_dir / f"climate_trace_{config.iso3}_CH4"))
        elif task.name == "tourism":
            extracted_dir = config.source("osm").get("extracted_dir")
            if extracted_dir:
                paths.extend(_iter_files(config.raw_dir / str(extracted_dir)))
        elif task.name == "accessibility":
            paths.extend(candidate for candidate in config.shape_dir.glob("assets.*") if candidate.is_file())
    elif task.kind == "combine":
        for candidate in _iter_files(config.dataset_dir):
            if not candidate.name.startswith(f"GPBP_LDT_{config.iso3}_"):
                paths.append(candidate)
    return sorted(set(path.resolve() for path in paths if path.is_file()), key=str)


def _domain_process_output(config: FactoryConfig, name: str) -> Path | None:
    names = {
        "accessibility": f"{config.iso3}_accessibility.csv",
        "air_pollution": f"{config.iso3}_air_pollution.csv",
        "emissions": f"{config.iso3}_emissions.csv",
        "flood": f"{config.iso3}_flood.csv",
        "heatwaves": f"{config.iso3}_heatwaves.csv",
        "internet": f"{config.iso3}_internet.csv",
        "land_cover": "lulc.csv",
        "luminosity": "luminosity.csv",
        "tourism": "tourism.csv",
    }
    filename = names.get(name)
    return config.dataset_dir / filename if filename else None


def _required_output_paths(config: FactoryConfig, task: TaskKey) -> list[Path]:
    if task.kind == "boundary":
        return [config.dataset_dir / f"GPBP_LDT_{config.iso3}_admin_2_regions.geojson"]
    if task.kind == "prerequisite" and task.name == "key_assets":
        parquet = config.shape_dir / "assets.parquet"
        return [parquet, parquet.with_suffix(".manifest.json"), config.shape_dir / "assets.geojson"]
    if task.kind == "prerequisite" and task.name == "transport":
        year = int(config.data["years"]["transport"])
        roads = config.shape_dir / f"roads_intersect_{year}.parquet"
        rails = config.shape_dir / f"rails_intersect_{year}.parquet"
        return [
            roads,
            roads.with_suffix(".manifest.json"),
            rails,
            rails.with_suffix(".manifest.json"),
            config.dataset_dir / f"{config.iso3}_infra_length.csv",
        ]
    if task.kind == "prerequisite" and task.name == "population":
        return [config.dataset_dir / f"{config.iso3}_population.csv"]
    if task.kind == "domain" and task.phase == "process":
        output = _domain_process_output(config, task.name)
        return [output] if output else []
    if task.kind == "combine":
        return [
            config.dataset_dir / f"GPBP_LDT_{config.iso3}_admin_2.csv",
            config.dataset_dir / f"GPBP_LDT_{config.iso3}_scores_admin_2.csv",
        ]
    return []


def _output_paths(config: FactoryConfig, task: TaskKey) -> list[Path]:
    paths: list[Path] = list(_required_output_paths(config, task))
    if task.kind == "boundary":
        paths.append(config.dataset_dir / f"GPBP_LDT_{config.iso3}_admin_2_regions.geojson")
    elif task.kind == "web" and task.name == "osm":
        source = config.source("osm")
        paths.append(config.raw_dir / str(source.get("archive_name", "")))
        paths.extend(_iter_files(config.raw_dir / str(source.get("extracted_dir", ""))))
    elif task.kind == "web" and task.name == "climate_trace":
        for gas in ("CO2", "CH4"):
            root = config.raw_dir / f"climate_trace_{config.iso3}_{gas}"
            paths.append(root.with_suffix(".zip"))
            paths.extend(_iter_files(root))
    elif task.kind == "web" and task.name == "population":
        paths.extend(_iter_files(config.raw_dir / "population"))
    elif task.kind == "prerequisite" and task.name == "key_assets":
        paths.extend(candidate for candidate in config.shape_dir.glob("assets.*") if candidate.is_file())
    elif task.kind == "prerequisite" and task.name == "transport":
        paths.extend(candidate for candidate in config.shape_dir.glob("*intersect_*.parquet") if candidate.is_file())
        paths.append(config.dataset_dir / f"{config.iso3}_infra_length.csv")
    elif task.kind == "prerequisite" and task.name == "population":
        paths.append(config.dataset_dir / f"{config.iso3}_population.csv")
    elif task.kind == "domain" and task.phase == "extract":
        extract_roots = {
            "air_pollution": "air_pollution",
            "accessibility": "accessibility",
            "flood": "flood",
            "heatwaves": "heatwaves",
            "land_cover": "land_cover",
            "luminosity": "luminosity",
        }
        root_name = extract_roots.get(task.name)
        if root_name:
            candidates = _iter_files(config.raw_dir / root_name)
            if task.name == "air_pollution":
                candidates = (
                    candidate
                    for candidate in candidates
                    if candidate.name not in {"grid_admin2.csv", "grid_admin2.meta.json"}
                )
            paths.extend(candidates)
    elif task.kind == "domain" and task.phase == "process":
        output = _domain_process_output(config, task.name)
        if output:
            paths.append(output)
    elif task.kind == "combine":
        paths.extend(
            [
                config.dataset_dir / f"GPBP_LDT_{config.iso3}_admin_2.csv",
                config.dataset_dir / f"GPBP_LDT_{config.iso3}_scores_admin_2.csv",
            ]
        )
    return sorted(set(path.resolve() for path in paths if path.is_file()), key=str)


def _input_signature(config: FactoryConfig, task: TaskKey) -> str:
    records = [_file_record(path) for path in _input_paths(config, task)]
    return _canonical_hash(records)


def _code_signature(task: TaskKey) -> str:
    records = []
    package_root = Path(__file__).resolve().parent
    # Every task uses shared I/O, geometry, checkpoint, cache, and logging
    # helpers. A package-wide signature is intentionally conservative.
    for path in sorted(package_root.rglob("*.py"), key=str):
        records.append(
            {
                "path": str(path.relative_to(package_root)).replace("\\", "/"),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return _canonical_hash(records)


class TaskStateStore:
    def __init__(self, config: FactoryConfig):
        self.config = config
        self.root = config.workspace / "state" / "tasks"

    def path(self, task: TaskKey) -> Path:
        return self.root / task.filename

    def read(self, task: TaskKey) -> dict[str, Any] | None:
        return _read_json(self.path(task))

    def dependencies(self, task: TaskKey) -> tuple[TaskKey, ...]:
        dependencies = list(_DEPENDENCIES.get((task.kind, task.name, task.phase), ()))
        if task.kind == "domain" and task.phase == "process" and not any(
            dependency.kind == "domain" and dependency.name == task.name for dependency in dependencies
        ):
            dependencies.insert(0, TaskKey("domain", task.name, "extract"))
        return tuple(dependencies)

    def fingerprint(self, task: TaskKey) -> str:
        upstream = []
        for dependency in self.dependencies(task):
            manifest = self.read(dependency) or {}
            upstream.append(
                {
                    "task_id": dependency.id,
                    "status": manifest.get("status", "missing"),
                    "fingerprint": manifest.get("fingerprint"),
                    "artifact_signature": manifest.get("artifact_signature"),
                }
            )
        return _canonical_hash(
            {
                "state_schema": STATE_SCHEMA_VERSION,
                "task_id": task.id,
                "config": self.config.data,
                "code_signature": _code_signature(task),
                "input_signature": _input_signature(self.config, task),
                "upstream": upstream,
            }
        )

    def can_resume(self, task: TaskKey, fingerprint: str) -> bool:
        manifest = self.read(task)
        if not manifest or manifest.get("status") != "completed" or manifest.get("fingerprint") != fingerprint:
            return False
        for path in _required_output_paths(self.config, task):
            try:
                if not path.is_file() or path.stat().st_size == 0:
                    return False
            except OSError:
                return False
        for artifact in manifest.get("artifacts", []):
            path = Path(str(artifact.get("path", "")))
            if not path.is_file():
                return False
            try:
                stat = path.stat()
                if stat.st_size != int(artifact["size"]) or stat.st_mtime_ns != int(artifact["mtime_ns"]):
                    return False
            except (KeyError, OSError, TypeError, ValueError):
                return False
        return True

    def _workspace_snapshot(self, task: TaskKey) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for path in _output_paths(self.config, task):
            record = _file_record(path.resolve(), self.config.workspace)
            result[str(path.resolve())] = record
        return result

    def execute(
        self,
        ctx: RunContext,
        task: TaskKey,
        logger: Any,
        operation: Callable[[], None],
        *,
        resume: bool = False,
        force: bool = False,
    ) -> TaskResult:
        import time

        fingerprint = self.fingerprint(task)
        previous = self.read(task) or {}
        if resume and not force and self.can_resume(task, fingerprint):
            artifacts = tuple(previous.get("artifacts", []))
            logger.info(
                "task skipped; completed state is current: %s",
                task.id,
                extra={
                    "task_id": task.id,
                    "kind": task.kind,
                    "domain": task.name if task.kind == "domain" else None,
                    "phase": task.phase,
                    "status": "skipped",
                    "fingerprint": fingerprint,
                    "run_id": ctx.run_id,
                    "artifact_count": len(artifacts),
                },
            )
            return TaskResult(task.id, "skipped", fingerprint, 0.0, artifacts)

        attempt = int(previous.get("attempt", 0)) + 1
        started_at = _utc_now()
        before = self._workspace_snapshot(task)
        running = {
            "schema_version": STATE_SCHEMA_VERSION,
            "task_id": task.id,
            "kind": task.kind,
            "name": task.name,
            "phase": task.phase,
            "status": "running",
            "fingerprint": fingerprint,
            "run_id": ctx.run_id,
            "attempt": attempt,
            "started_at": started_at,
            "updated_at": started_at,
            "pid": os.getpid(),
            "host": socket.gethostname(),
        }
        _atomic_write_json(self.path(task), running)
        logger.info(
            "task started: %s",
            task.id,
            extra={
                "task_id": task.id,
                "kind": task.kind,
                "domain": task.name if task.kind == "domain" else None,
                "phase": task.phase,
                "status": "running",
                "fingerprint": fingerprint,
                "run_id": ctx.run_id,
                "attempt": attempt,
            },
        )
        start = time.monotonic()
        try:
            operation()
            missing_outputs = [
                path
                for path in _required_output_paths(self.config, task)
                if not path.is_file() or path.stat().st_size == 0
            ]
            if missing_outputs:
                raise RuntimeError(
                    f"Task {task.id} returned without required output(s): "
                    + ", ".join(str(path) for path in missing_outputs)
                )
        except BaseException as error:
            elapsed = round(time.monotonic() - start, 3)
            interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
            status = "interrupted" if interrupted else "failed"
            error_message = redact_sensitive(self.config, error)
            formatted_traceback = redact_sensitive(
                self.config,
                "".join(traceback.format_exception(error))[-20000:],
            )
            failure = {
                **running,
                "status": status,
                "updated_at": _utc_now(),
                "finished_at": _utc_now(),
                "elapsed_seconds": elapsed,
                "error_type": type(error).__name__,
                "error_message": error_message,
                "traceback": formatted_traceback,
            }
            _atomic_write_json(self.path(task), failure)
            logger.log(
                30 if interrupted else 40,
                f"task {status}: {task.id}",
                exc_info=False,
                extra={
                    "task_id": task.id,
                    "kind": task.kind,
                    "domain": task.name if task.kind == "domain" else None,
                    "phase": task.phase,
                    "status": status,
                    "elapsed_seconds": elapsed,
                    "error_type": type(error).__name__,
                    "error_message": error_message,
                    "run_id": ctx.run_id,
                    "attempt": attempt,
                },
            )
            raise

        elapsed = round(time.monotonic() - start, 3)
        after = self._workspace_snapshot(task)
        artifacts = [{**record, "path": str(Path(path))} for path, record in after.items()]
        changed_artifact_count = sum(before.get(path) != record for path, record in after.items())
        artifact_signature = _canonical_hash(artifacts)
        completed = {
            **running,
            "status": "completed",
            "updated_at": _utc_now(),
            "finished_at": _utc_now(),
            "elapsed_seconds": elapsed,
            "artifacts": artifacts,
            "artifact_count": len(artifacts),
            "changed_artifact_count": changed_artifact_count,
            "artifact_signature": artifact_signature,
        }
        _atomic_write_json(self.path(task), completed)
        logger.info(
            "task completed: %s",
            task.id,
            extra={
                "task_id": task.id,
                "kind": task.kind,
                "domain": task.name if task.kind == "domain" else None,
                "phase": task.phase,
                "status": "completed",
                "elapsed_seconds": elapsed,
                "fingerprint": fingerprint,
                "artifact_count": len(artifacts),
                "run_id": ctx.run_id,
                "attempt": attempt,
            },
        )
        return TaskResult(task.id, "completed", fingerprint, elapsed, tuple(artifacts))

    def mark_abandoned(self, task: TaskKey, *, run_id: str, reason: str) -> None:
        manifest = self.read(task)
        if not manifest or manifest.get("status") != "running" or manifest.get("run_id") != run_id:
            return
        _atomic_write_json(
            self.path(task),
            {
                **manifest,
                "status": "interrupted",
                "updated_at": _utc_now(),
                "finished_at": _utc_now(),
                "error_type": "OrchestratorInterrupted",
                "error_message": reason,
            },
        )


class RunState:
    def __init__(self, config: FactoryConfig, run_id: str):
        self.path = config.workspace / "state" / "runs" / f"{run_id}.json"
        self.run_id = run_id
        self.payload: dict[str, Any] = {}

    def start(self, *, resume: bool, force: bool, requested_tasks: list[str]) -> None:
        previous = _read_json(self.path) or {}
        self.payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": "running",
            "attempt": int(previous.get("attempt", 0)) + 1,
            "started_at": _utc_now(),
            "updated_at": _utc_now(),
            "resume": resume,
            "force": force,
            "requested_tasks": requested_tasks,
            "tasks": {},
        }
        _atomic_write_json(self.path, self.payload)

    def record(self, result: TaskResult) -> None:
        self.payload.setdefault("tasks", {})[result.task_id] = result.as_dict()
        self.payload["updated_at"] = _utc_now()
        _atomic_write_json(self.path, self.payload)

    def finish(self, status: str, *, summary: dict[str, Any] | None = None) -> None:
        self.payload["status"] = status
        self.payload["updated_at"] = _utc_now()
        self.payload["finished_at"] = _utc_now()
        if summary is not None:
            self.payload["summary"] = summary
        _atomic_write_json(self.path, self.payload)
