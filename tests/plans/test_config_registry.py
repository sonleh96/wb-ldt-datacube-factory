from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml

from ldt_factory.plans.config import PlanConfigError, load_plan_config
from ldt_factory.plans.registry import load_admin_registry
from ldt_factory.plans.scoring import normalize_status


def _write_config(tmp_path: Path, registry_rows: list[dict[str, str]], **registry_overrides):
    registry_path = tmp_path / "admin2.csv"
    headers = sorted({key for row in registry_rows for key in row})
    with registry_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(registry_rows)
    payload = {
        "schema_version": 1,
        "country": {
            "iso3": "TST",
            "name": "Testland",
            "languages": ["xx"],
            "admin2_types": ["district"],
        },
        "workspace": "plans/TST",
        "admin_registry": {
            "path": registry_path.name,
            "name_field": "name",
            "parent_field": "parent",
            "alias_fields": [],
            **registry_overrides,
        },
        "search": {
            "api_key_env": "EXA_API_KEY",
            "document_terms": ["development plan"],
            "final_terms": ["adopted"],
            "draft_terms": ["draft"],
            "official_domain_suffixes": ["gov.test"],
        },
        "review": {
            "auto_accept_threshold": 0.9,
            "minimum_margin": 0.1,
            "require_formal_adoption_for_auto_accept": True,
        },
        "acquisition": {
            "request_timeout_seconds": 30,
            "download_retries": 2,
            "min_file_bytes": 10,
            "max_file_bytes": 1000000,
        },
        "storage": {"bucket": "test-bucket", "prefix": "plans/districts"},
    }
    path = tmp_path / "plans.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


def test_plan_config_and_registry_deduplicate_panel_rows(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        [
            {"name": "Bačka Palanka", "parent": "South Bačka", "year": "2024"},
            {"name": "Bačka Palanka", "parent": "South Bačka", "year": "2025"},
            {"name": "Ada", "parent": "North Banat", "year": "2025"},
        ],
    )
    config = load_plan_config(config_path, data_root=tmp_path)
    areas = load_admin_registry(config)

    assert config.workspace == (tmp_path / "plans" / "TST").resolve()
    assert [area.name for area in areas] == ["Ada", "Bačka Palanka"]
    assert areas[1].storage_slug == "ba-ka-palanka"
    assert areas[1].admin2_id.startswith("TST-")


def test_registry_uses_explicit_id_and_aliases(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        [{"code": "TST001", "name": "Central", "parent": "North", "aliases": "Centre|Centro"}],
        id_field="code",
        alias_fields=["aliases"],
    )
    area = load_admin_registry(load_plan_config(config_path, data_root=tmp_path))[0]
    assert area.admin2_id == "TST001"
    assert area.aliases == ("Centre", "Centro")


def test_registry_rejects_slug_collisions(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        [
            {"code": "1", "name": "Ča", "parent": "A"},
            {"code": "2", "name": "Ža", "parent": "B"},
        ],
        id_field="code",
    )
    with pytest.raises(PlanConfigError, match="storage slug"):
        load_admin_registry(load_plan_config(config_path, data_root=tmp_path))


def test_plan_config_rejects_relative_paths_without_data_root(tmp_path: Path, monkeypatch):
    config_path = _write_config(tmp_path, [{"name": "A", "parent": "B"}])
    monkeypatch.delenv("LDT_DATA_ROOT", raising=False)
    with pytest.raises(PlanConfigError, match="no data root"):
        load_plan_config(config_path)


def test_status_normalization_uses_country_specific_terms(tmp_path: Path):
    config_path = _write_config(tmp_path, [{"name": "A", "parent": "B"}])
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["search"]["final_terms"] = ["miratuar"]
    payload["search"]["draft_terms"] = ["për konsultim publik"]
    config_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    config = load_plan_config(config_path, data_root=tmp_path)

    assert normalize_status("unknown", "Plani është miratuar", config) == "final"
    assert normalize_status("unknown", "Dokument për konsultim publik", config) == "draft"


def test_committed_albania_profile_contract(tmp_path: Path):
    path = Path(__file__).parents[2] / "config" / "development_plans" / "alb.yaml"
    config = load_plan_config(path, data_root=tmp_path, require_registry=False)

    assert config.iso3 == "ALB"
    assert config.registry["name_field"] == "Municipality"
    assert config.registry["parent_field"] == "County"
    assert config.storage_prefix == "ldt/sources_albania/municipalities"
    assert config.review["auto_accept_enabled"] is False
