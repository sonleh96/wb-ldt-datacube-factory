from pathlib import Path

import pytest
import yaml

from ldt_factory.config import ConfigError, load_config


def _base(tmp_path: Path):
    boundaries = {}
    for level in ("admin0", "admin1", "admin2"):
        path = tmp_path / f"{level}.geojson"
        path.write_text("{}", encoding="utf-8")
        boundaries[level] = str(path)
    return {
        "country": {"iso3": "ROU", "name": "Romania"},
        "workspace": str(tmp_path / "workspace"),
        "boundaries": {
            **boundaries,
            "admin1_source_field": "NAME_1",
            "admin2_source_field": "NAME_2",
            "admin1_output_name": "County",
            "admin2_output_name": "Municipality",
        },
    }


def _write(tmp_path: Path, data) -> Path:
    path = tmp_path / "country.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_load_config_resolves_contract(tmp_path):
    config = load_config(_write(tmp_path, _base(tmp_path)))
    assert config.iso3 == "ROU"
    assert config.admin1 == "County"
    assert config.admin2 == "Municipality"
    assert config.workspace == (tmp_path / "workspace").resolve()


def test_relative_paths_resolve_from_user_data_root(tmp_path):
    data_root = tmp_path / "data root \u6570\u636e"
    workspace = data_root / "countries" / "ROU"
    boundary_dir = workspace / "boundaries"
    boundary_dir.mkdir(parents=True)
    data = _base(tmp_path)
    data["workspace"] = "countries/ROU"
    for level in ("admin0", "admin1", "admin2"):
        path = boundary_dir / f"{level}.geojson"
        path.write_text("{}", encoding="utf-8")
        data["boundaries"][level] = f"boundaries/{level}.geojson"
    data["sources"] = {
        "heatwaves": {"cache_dir": "shared_sources/heatwaves"},
        "internet": {"dataset_root": "shared_sources/ookla"},
    }

    config = load_config(_write(tmp_path, data), data_root=data_root)

    assert config.data_root == data_root.resolve()
    assert config.workspace == workspace.resolve()
    assert config.boundary_path("admin2") == (boundary_dir / "admin2.geojson").resolve()
    assert config.source("heatwaves")["cache_dir"] == str(
        (data_root / "shared_sources" / "heatwaves").resolve()
    )
    assert config.source("internet")["dataset_root"] == str(
        (data_root / "shared_sources" / "ookla").resolve()
    )


def test_environment_data_root_and_explicit_override(tmp_path, monkeypatch):
    env_root = tmp_path / "environment"
    explicit_root = tmp_path / "explicit"
    data = _base(tmp_path)
    data["workspace"] = "${LDT_DATA_ROOT}/countries/ROU"
    for level in ("admin0", "admin1", "admin2"):
        data["boundaries"][level] = str(tmp_path / f"{level}.geojson")
    monkeypatch.setenv("LDT_DATA_ROOT", str(env_root))

    from_environment = load_config(_write(tmp_path, data))
    overridden = load_config(_write(tmp_path, data), data_root=explicit_root)

    assert from_environment.workspace == (env_root / "countries" / "ROU").resolve()
    assert overridden.workspace == (explicit_root / "countries" / "ROU").resolve()


def test_yaml_data_root_is_relative_to_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LDT_DATA_ROOT", raising=False)
    data = _base(tmp_path)
    data["data_root"] = "local-data"
    data["workspace"] = "countries/ROU"

    config = load_config(_write(tmp_path, data))

    assert config.data_root == (tmp_path / "local-data").resolve()
    assert config.workspace == (
        tmp_path / "local-data" / "countries" / "ROU"
    ).resolve()


def test_relative_path_without_data_root_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("LDT_DATA_ROOT", raising=False)
    data = _base(tmp_path)
    data["workspace"] = "countries/ROU"

    with pytest.raises(ConfigError, match="Relative path in workspace requires --data-root"):
        load_config(_write(tmp_path, data))


def test_unresolved_path_variable_names_the_field(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_LDT_ROOT", raising=False)
    data = _base(tmp_path)
    data["workspace"] = "${MISSING_LDT_ROOT}/countries/ROU"

    with pytest.raises(
        ConfigError,
        match=r"Unresolved environment variable\(s\) in workspace: MISSING_LDT_ROOT",
    ):
        load_config(_write(tmp_path, data))


def test_load_config_rejects_same_admin_names(tmp_path):
    data = _base(tmp_path)
    data["boundaries"]["admin2_output_name"] = "County"
    with pytest.raises(ConfigError, match="must differ"):
        load_config(_write(tmp_path, data))


def test_load_config_rejects_non_iso3(tmp_path):
    data = _base(tmp_path)
    data["country"]["iso3"] = "Romania"
    with pytest.raises(ConfigError, match="ISO-3"):
        load_config(_write(tmp_path, data))


def test_load_config_rejects_unknown_land_cover_backend(tmp_path):
    data = _base(tmp_path)
    data["sources"] = {"land_cover": {"backend": "unknown"}}
    with pytest.raises(ConfigError, match="land_cover.backend must be one of"):
        load_config(_write(tmp_path, data))


def test_reduce_regions_backend_requires_earth_engine_asset_and_project(tmp_path):
    data = _base(tmp_path)
    data["sources"] = {"land_cover": {"backend": "gee_reduce_regions"}}
    with pytest.raises(ConfigError, match="admin2_asset_id"):
        load_config(_write(tmp_path, data))


def test_reduce_regions_backend_configuration_is_accepted(tmp_path):
    data = _base(tmp_path)
    data["sources"] = {
        "earth_engine": {
            "project_id": "test-project",
            "admin2_asset_id": "projects/test-project/assets/rou_admin2",
        },
        "land_cover": {
            "backend": "gee_reduce_regions",
            "pixel_size_m": 10,
            "tile_scale": 4,
            "max_pixels_per_region": 1_000_000,
            "poll_seconds": 1,
            "cleanup_intermediate_assets": False,
        },
    }

    config = load_config(_write(tmp_path, data))

    assert config.source("land_cover")["backend"] == "gee_reduce_regions"
