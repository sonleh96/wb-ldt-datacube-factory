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
