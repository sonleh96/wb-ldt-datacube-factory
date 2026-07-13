import json
import logging
from pathlib import Path

import pytest
import yaml

from ldt_factory.cli import build_parser
from ldt_factory.config import ConfigError, load_config
from ldt_factory.context import RunContext
from ldt_factory.inspection import build_plan, load_status, run_preflight
from ldt_factory.locks import workspace_lock
from ldt_factory.resources import ResourcePolicy
from ldt_factory.task_state import RunState, TaskKey, TaskStateStore


def _config(tmp_path: Path, pipeline: dict | None = None):
    boundaries = {}
    for level in ("admin0", "admin1", "admin2"):
        path = tmp_path / f"{level}.geojson"
        path.write_text("{}", encoding="utf-8")
        boundaries[level] = str(path)
    data = {
        "country": {"iso3": "ROU", "name": "Romania"},
        "workspace": str(tmp_path / "workspace"),
        "boundaries": {
            **boundaries,
            "admin1_source_field": "NAME_1",
            "admin2_source_field": "NAME_2",
            "admin1_output_name": "County",
            "admin2_output_name": "Municipality",
        },
        "pipeline": pipeline or {},
    }
    path = tmp_path / "country.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.orchestration")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def test_resource_policy_enforces_limits_and_overrides(tmp_path):
    config = _config(
        tmp_path,
        {
            "max_parallel": 3,
            "resource_limits": {"heavy_memory": 1},
            "task_resources": {"domain.flood.process": ["heavy_memory", "disk_io"]},
        },
    )
    policy = ResourcePolicy.from_config(config)
    flood = TaskKey("domain", "flood", "process")
    assert policy.max_parallel == 3
    assert policy.requirements(flood) == {"heavy_memory": 1, "disk_io": 1}
    used = {}
    assert policy.can_reserve(flood, used)
    policy.reserve(flood, used)
    assert not policy.can_reserve(flood, used)
    policy.release(flood, used)
    assert used == {}


def test_invalid_resource_configuration_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="must be at least 1"):
        _config(tmp_path, {"resource_limits": {"heavy_memory": 0}})


def test_task_manifest_resumes_and_revalidates_artifact(tmp_path):
    config = _config(tmp_path)
    config.prepare_directories()
    ctx = RunContext(config, "test-run", resume=True)
    store = TaskStateStore(config)
    task = TaskKey("domain", "flood", "process")
    output = config.dataset_dir / "ROU_flood.csv"
    calls = []

    def operation():
        calls.append(1)
        output.write_text("value\n1\n", encoding="utf-8")

    first = store.execute(ctx, task, _logger(), operation, resume=True)
    second = store.execute(ctx, task, _logger(), operation, resume=True)
    assert first.status == "completed"
    assert second.status == "skipped"
    assert len(calls) == 1
    manifest = json.loads(store.path(task).read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["artifact_count"] == 1
    assert not list(store.path(task).parent.glob("*.tmp"))

    output.unlink()
    third = store.execute(ctx, task, _logger(), operation, resume=True)
    assert third.status == "completed"
    assert len(calls) == 2


def test_task_failure_and_run_summary_are_atomic(tmp_path):
    config = _config(tmp_path)
    config.prepare_directories()
    ctx = RunContext(config, "failed-run")
    store = TaskStateStore(config)
    task = TaskKey("web", "osm")

    with pytest.raises(RuntimeError, match="boom"):
        store.execute(ctx, task, _logger(), lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    task_manifest = json.loads(store.path(task).read_text(encoding="utf-8"))
    assert task_manifest["status"] == "failed"
    assert task_manifest["error_type"] == "RuntimeError"

    run = RunState(config, ctx.run_id)
    run.start(resume=True, force=False, requested_tasks=[task.id])
    run.finish("failed", summary={"failed": 1})
    run_manifest = json.loads(run.path.read_text(encoding="utf-8"))
    assert run_manifest["status"] == "failed"
    assert run_manifest["summary"] == {"failed": 1}


def test_output_producing_task_cannot_complete_without_its_contract(tmp_path):
    config = _config(tmp_path)
    config.prepare_directories()
    ctx = RunContext(config, "missing-output")
    store = TaskStateStore(config)
    task = TaskKey("boundary", "normalized")

    with pytest.raises(RuntimeError, match="returned without required output"):
        store.execute(ctx, task, _logger(), lambda: None)
    manifest = json.loads(store.path(task).read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "admin_2_regions.geojson" in manifest["error_message"]


def test_cli_keeps_existing_commands_and_adds_resume_force_flags():
    parser = build_parser()
    args = parser.parse_args(
        ["run-domain", "--config", "rou.yaml", "--name", "flood", "--phase", "process", "--resume", "--force"]
    )
    assert args.command == "run-domain"
    assert args.resume is True
    assert args.force is True

    existing = parser.parse_args(["run-web", "--config", "rou.yaml", "--source", "osm"])
    assert existing.resume is None
    assert existing.force is False


def test_plan_exposes_stages_resources_and_dependencies(tmp_path):
    config = _config(
        tmp_path,
        {
            "main_domains": ["flood", "tourism"],
            "max_parallel": 3,
        },
    )
    payload = build_plan(config)
    stages = {stage["name"]: stage["tasks"] for stage in payload["stages"]}
    domain_tasks = {task["task_id"]: task for task in stages["domains"]}
    assert payload["max_parallel"] == 3
    assert "domain.accessibility.extract" in domain_tasks
    assert domain_tasks["domain.flood.process"]["resources"]["heavy_memory"] == 1
    assert "domain.flood.extract" in domain_tasks["domain.flood.process"]["depends_on"]
    quality_task = stages["quality"][0]
    assert quality_task["task_id"] == "quality.publication.accessibility"
    assert quality_task["depends_on"] == ["combine.indicators.accessibility"]


def test_accessibility_is_required_even_when_country_config_omits_it(tmp_path):
    config = _config(tmp_path, {"main_domains": ["flood"]})

    payload = build_plan(config)
    stages = {stage["name"]: stage["tasks"] for stage in payload["stages"]}
    task_ids = {task["task_id"] for task in stages["domains"]}

    assert "domain.accessibility.extract" in task_ids
    assert "domain.accessibility.process" in task_ids
    assert stages["combine"][0]["task_id"] == "combine.indicators.accessibility"


def test_preflight_reports_credential_presence_without_value(tmp_path, monkeypatch):
    config = _config(tmp_path, {"main_domains": ["air_pollution"]})
    config.data["sources"] = {
        "openweathermap": {
            "api_key_env": "TEST_OWM_KEY",
            "grid_degrees": 1,
            "requests_per_minute": 60,
        }
    }
    monkeypatch.setenv("TEST_OWM_KEY", "do-not-print-this-secret")
    monkeypatch.setattr("ldt_factory.inspection._air_pollution_estimate", lambda _config: None)
    payload = run_preflight(config)
    serialized = json.dumps(payload)
    assert "TEST_OWM_KEY" in serialized
    assert "do-not-print-this-secret" not in serialized
    assert any(check["name"] == "OpenWeatherMap API key" and check["status"] == "ok" for check in payload["checks"])
    assert any(check["name"] == "Mapbox access token" for check in payload["checks"])


def test_status_loads_latest_run_and_live_task_state(tmp_path):
    config = _config(tmp_path)
    config.prepare_directories()
    run = RunState(config, "status-run")
    task = TaskKey("web", "osm")
    run.start(resume=True, force=False, requested_tasks=[task.id])
    task_path = TaskStateStore(config).path(task)
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        json.dumps({"task_id": task.id, "run_id": "status-run", "status": "running"}),
        encoding="utf-8",
    )

    payload = load_status(config)
    assert payload["run_id"] == "status-run"
    assert payload["status"] == "running"
    assert payload["status_counts"] == {"running": 1}
    assert payload["pending"] == 0


def test_cli_parses_read_only_inspection_commands():
    parser = build_parser()
    assert parser.parse_args(["plan", "--config", "rou.yaml", "--json"]).command == "plan"
    assert parser.parse_args(["preflight", "--config", "rou.yaml"]).command == "preflight"
    status = parser.parse_args(["status", "--config", "rou.yaml", "--run-id", "abc"])
    assert status.command == "status"
    assert status.run_id == "abc"


def test_workspace_lock_is_released_and_cleaned_up(tmp_path):
    path = tmp_path / "pipeline.lock"
    with workspace_lock(path, label="test workspace"):
        assert path.is_file()
    assert not path.exists()
